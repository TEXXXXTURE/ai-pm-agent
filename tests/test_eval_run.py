# [C 2026-09-12 by MA] 构建期跑评测节点（eval_run）自测
"""eval_run 零 API 测试：patch 掉 nodes.eval_run.interrupt 与 subprocess.run，
不发起任何真实模型调用、不跑真 Promptfoo。

覆盖：
1. finalize_eval_config 纯函数：prompts 替换为 file://system_prompt.txt、
   provider 归一为 {id, config:{temperature:0,max_tokens:500,showThinking:false}}、
   tests 原样保留、非法 YAML 返回空串；
2. parse_promptfoo_results 纯函数：successes/failures/errors 取数、errors 计入失败、
   逐题字段提取、缺字段安全降级、token/cost/duration；
3. judge_eval_report 纯函数：双及格线达标/单项不达标/双不达标、关键题为空不设门槛、
   total=0 时 overall=0、匹配不到的关键题视为失败；
4. 节点协议（mock interrupt + subprocess）：
   - ai_core=False 直通返回 {}；
   - await_prompt：缺 system_prompt.txt -> interrupt(status=await_prompt) 含 prompt_path；
     恢复后文件存在 -> 继续执行；
   - tool_error：subprocess 返回码 1 -> interrupt(status=tool_error) 含 reason；
     恢复后返回码 0 -> 继续；
   - eval_failed：返回码 100 + judge=False -> interrupt(status=eval_failed) 含 report；
     恢复后重跑，返回码 0 且 judge=True -> 放行；
   - 达标路径：返回码 0 + judge=True -> eval_report.passed=True、eval_run_count=1、
     eval_artifacts 三路径；
   - eval_run_count 每次执行 +1；await_prompt 阶段不 +1；
5. 路由：route_after_eval_run 两态（passed -> launch_plan；否则 -> eval_run）；
   route_after_issue_confirm：确认且 ai_core=True -> eval_run；确认且普通轨 -> artifact_persist；
   重拆/回炉分支逐字不变；
6. 图编译：17 节点齐（新增 eval_run；第 6 段再增 bake_off）；普通轨 issue_confirm 确认分支不经 eval_run；
7. QUESTION 文案：run_prd_workflow._build_question 对 eval_run 三态输出对应提示。
"""
from __future__ import annotations

import importlib.util
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

import yaml  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager, sanitize_name  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.eval_design import render_promptfoo_yaml  # noqa: E402
from nodes.eval_run import (  # noqa: E402
    EXIT_CONTENT_FAIL,
    EXIT_OK,
    EXIT_TOOL_ERROR,
    finalize_eval_config,
    judge_eval_report,
    make_eval_run,
    parse_promptfoo_results,
    route_after_eval_run,
)
from nodes.issues import route_after_issue_confirm  # noqa: E402

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"


# ────────────────────────── 测试替身与夹具 ──────────────────────────


class FakeLLM:
    def __init__(self, json_queue=None):
        self.json_queue = list(json_queue or [])
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        return self.json_queue.pop(0) if self.json_queue else {}


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(
        runner=runner,
        registry=registry,
        artifacts=artifacts,
        kb=None,
        eval_tool={"promptfoo_dir": str(tmp_dir / "pf")},
    )


def _valid_eval_system() -> dict:
    """构造合法评测体系（典型3/边界3/对抗2/replay0，含 critical 标记）。"""
    return {
        "purpose": "测试用",
        "exam_sets": [
            {
                "layer": "typical",
                "exams": [
                    {"id": "T1", "layer": "typical", "description": "T1 核心题",
                     "prompt_hint": "in1", "scorer": "assertion",
                     "assertion": "contains: x", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": True},
                    {"id": "T2", "layer": "typical", "description": "T2 非关键",
                     "prompt_hint": "in2", "scorer": "assertion",
                     "assertion": "contains: y", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                    {"id": "T3", "layer": "typical", "description": "T3 非关键",
                     "prompt_hint": "in3", "scorer": "assertion",
                     "assertion": "contains: z", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                ],
                "placeholder_note": "",
            },
            {
                "layer": "boundary",
                "exams": [
                    {"id": "B1", "layer": "boundary", "description": "B1",
                     "prompt_hint": "b1", "scorer": "assertion",
                     "assertion": "contains: b", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                    {"id": "B2", "layer": "boundary", "description": "B2",
                     "prompt_hint": "b2", "scorer": "assertion",
                     "assertion": "contains: c", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                    {"id": "B3", "layer": "boundary", "description": "B3",
                     "prompt_hint": "b3", "scorer": "assertion",
                     "assertion": "contains: d", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                ],
                "placeholder_note": "",
            },
            {
                "layer": "adversarial",
                "exams": [
                    {"id": "A1", "layer": "adversarial", "description": "A1 注入",
                     "prompt_hint": "a1", "scorer": "assertion",
                     "assertion": "contains: 拒", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                    {"id": "A2", "layer": "adversarial", "description": "A2 泄露",
                     "prompt_hint": "a2", "scorer": "assertion",
                     "assertion": "contains: 不能", "judge_rubric": None,
                     "manual_review_ratio": 0.0, "critical": False},
                ],
                "placeholder_note": "",
            },
            {
                "layer": "replay",
                "exams": [],
                "placeholder_note": "二期填充",
            },
        ],
        "pass_lines": {"overall_pass_rate": 0.8, "critical_pass_rate": 0.9,
                       "note": "测试"},
    }


def _results_json(successes: int, failures: int, errors: int = 0,
                  per_exam_success: list[bool] | None = None,
                  descriptions: list[str] | None = None) -> dict:
    """构造 Promptfoo results.json 结构。descriptions 与 per_exam_success 一一对应。"""
    total = successes + failures
    per_exam = []
    if per_exam_success is None:
        per_exam_success = [True] * successes + [False] * failures
    for i, ok in enumerate(per_exam_success):
        desc = descriptions[i] if descriptions else f"考题 {i+1}"
        per_exam.append({
            "testIdx": i,
            "description": desc,
            "success": ok,
            "score": 1.0 if ok else 0.0,
            "latencyMs": 100,
            "response": {"output": f"回答{i+1}"},
            "gradingResult": {"pass": ok},
        })
    return {
        "stats": {
            "successes": successes,
            "failures": failures,
            "errors": errors,
            "tokenUsage": {"prompt": 100, "completion": 50, "total": 150},
            "totalCost": 0.01,
            "durationMs": 2000,
        },
        "results": per_exam,
    }


def run_eval_node(state: dict, deps: NodeDeps, answers: list,
                  returncodes: list[int] | None = None,
                  results_json_list: list[dict] | None = None,
                  prompt_exists_after: int = 0):
    """运行 eval_run 节点，mock interrupt 按 answers 回放、subprocess 按 returncodes 回放。

    - answers: interrupt 的 resume 值序列（任意值，eval_run 不读 resume 内容）；
    - returncodes: subprocess.run 返回码序列，None 时默认 [0]；
    - results_json_list: 每次执行写入 results.json 的内容序列，None 时默认全过。
    - prompt_exists_after: 第几次 interrupt 后创建 system_prompt.txt（0=一开始就存在，
      即不经 await_prompt；1=第一次 interrupt 恢复后创建，依此类推）。
    """
    payloads: list[dict] = []
    ans_queue = list(answers)
    rc_queue = list(returncodes if returncodes is not None else [EXIT_OK])
    rj_queue = list(results_json_list if results_json_list is not None
                    else [_results_json(8, 0)])
    interrupt_count = [0]
    eval_dir_ref: list[Path] = []

    def _find_eval_dir() -> Path | None:
        """在 artifacts 输出目录下搜索刚落盘的 eval_config.yaml，取其父目录。"""
        root = Path(deps.artifacts.output_root)
        found = list(root.rglob("*eval_config*.yaml"))
        return found[0].parent if found else None

    def fake_interrupt(value):
        payloads.append(value)
        interrupt_count[0] += 1
        # 第 prompt_exists_after 次 interrupt 恢复后，把 system_prompt.txt 放进评测目录
        if interrupt_count[0] >= prompt_exists_after:
            d = _find_eval_dir()
            if d is not None:
                (d / "system_prompt.txt").write_text(
                    "你是被测系统", encoding="utf-8"
                )
        return ans_queue.pop(0) if ans_queue else None

    def fake_run(command, **kwargs):
        rc = rc_queue.pop(0)
        proc = MagicMock()
        proc.returncode = rc
        proc.stderr = ""
        proc.stdout = ""
        # 写 results.json 到 cwd（cwd 是评测目录）
        if rc in (EXIT_OK, EXIT_CONTENT_FAIL):
            cwd = kwargs.get("cwd", ".")
            eval_dir_ref.append(Path(cwd))
            rj = rj_queue.pop(0) if rj_queue else _results_json(8, 0)
            (Path(cwd) / "results.json").write_text(
                json.dumps(rj, ensure_ascii=False), encoding="utf-8"
            )
        else:
            eval_dir_ref.append(Path(kwargs.get("cwd", ".")))
        return proc

    # prompt_exists_after=0：预先在评测目录放好 system_prompt.txt，跳过 await_prompt
    if prompt_exists_after == 0:
        safe = sanitize_name(state.get("requirement_name") or "未命名需求")
        eval_dir = Path(deps.artifacts.output_root) / safe / "评测"
        eval_dir.mkdir(parents=True, exist_ok=True)
        (eval_dir / "system_prompt.txt").write_text("你是被测系统", encoding="utf-8")

    node = make_eval_run(deps)
    with patch("nodes.eval_run.interrupt", side_effect=fake_interrupt), \
         patch("nodes.eval_run.subprocess.run", side_effect=fake_run):
        out = node(state)
    return out, payloads


# ────────────────────────── 1. finalize_eval_config ──────────────────────────


class TestFinalizeEvalConfig(unittest.TestCase):
    def setUp(self):
        self.draft = render_promptfoo_yaml(_valid_eval_system())

    def test_prompts_replaced_with_file_ref(self):
        text = finalize_eval_config(self.draft)
        doc = yaml.safe_load(text)
        self.assertEqual(doc["prompts"], ["./system_prompt.txt"])

    def test_provider_normalized_to_dict_with_config(self):
        text = finalize_eval_config(self.draft)
        doc = yaml.safe_load(text)
        prov = doc["providers"][0]
        self.assertEqual(prov["id"], "deepseek:deepseek-v4-flash")
        self.assertEqual(prov["config"]["temperature"], 0)
        # max_tokens 只守下限：额度必须给隐藏思考留足余量
        self.assertGreaterEqual(prov["config"]["max_tokens"], 8192)
        self.assertFalse(prov["config"]["showThinking"])

    def test_tests_preserved(self):
        text = finalize_eval_config(self.draft)
        doc = yaml.safe_load(text)
        # 典型3+边界3+对抗2 = 8 题（replay 空）
        self.assertEqual(len(doc["tests"]), 8)

    def test_llm_rubric_judge_provider_backfilled_for_old_draft(self):
        # S039：修复前渲染、冻结在 state 里的旧草案，llm-rubric 缺阅卷模型，
        # finalize 必须统一补上 deepseek 阅卷模型；已有 provider 不覆盖。
        old_draft = yaml.safe_dump(
            {
                "prompts": ["{{prd_core_task_prompt}}"],
                "providers": ["deepseek:deepseek-v4-flash"],
                "tests": [
                    {
                        "description": "[typical] J1",
                        "vars": {"input": "x"},
                        "assert": [{"type": "llm-rubric", "value": "标准"}],
                    },
                    {
                        "description": "[typical] J2",
                        "vars": {"input": "y"},
                        "assert": [
                            {"type": "contains", "value": "转人工"},
                            {"type": "llm-rubric", "value": "标准", "provider": "custom:judge"},
                        ],
                    },
                ],
            },
            allow_unicode=True,
        )
        doc = yaml.safe_load(finalize_eval_config(old_draft))
        self.assertEqual(doc["tests"][0]["assert"][0]["provider"], "deepseek:deepseek-v4-flash")
        # 非阅卷断言不加 provider；已有 provider 的阅卷断言不被覆盖
        self.assertNotIn("provider", doc["tests"][1]["assert"][0])
        self.assertEqual(doc["tests"][1]["assert"][1]["provider"], "custom:judge")

    def test_invalid_yaml_returns_empty(self):
        self.assertEqual(finalize_eval_config("{{not valid yaml"), "")

    def test_top_level_not_dict_returns_empty(self):
        self.assertEqual(finalize_eval_config("- a list item"), "")


# ────────────────────────── 2. parse_promptfoo_results ──────────────────────────


class TestParsePromptfooResults(unittest.TestCase):
    def test_successes_failures_errors(self):
        rj = _results_json(5, 2, errors=1)
        parsed = parse_promptfoo_results(rj)
        self.assertEqual(parsed["successes"], 5)
        self.assertEqual(parsed["errors"], 1)
        self.assertEqual(parsed["failures"], 3)  # failures + errors
        self.assertEqual(parsed["total"], 8)

    def test_per_exam_fields(self):
        rj = _results_json(1, 0, per_exam_success=[True])
        parsed = parse_promptfoo_results(rj)
        exam = parsed["per_exam"][0]
        self.assertEqual(exam["test_idx"], 0)
        self.assertTrue(exam["success"])
        self.assertEqual(exam["output_snippet"], "回答1")
        self.assertTrue(exam["grading_pass"])

    def test_missing_fields_safe_default(self):
        parsed = parse_promptfoo_results({})
        self.assertEqual(parsed["successes"], 0)
        self.assertEqual(parsed["total"], 0)
        self.assertEqual(parsed["token_usage"]["total"], 0)
        self.assertEqual(parsed["cost"], 0.0)
        self.assertEqual(parsed["duration_ms"], 0)

    def test_token_and_cost(self):
        rj = _results_json(1, 0)
        parsed = parse_promptfoo_results(rj)
        self.assertEqual(parsed["token_usage"]["prompt"], 100)
        self.assertEqual(parsed["token_usage"]["total"], 150)
        self.assertEqual(parsed["cost"], 0.01)
        self.assertEqual(parsed["duration_ms"], 2000)


# ────────────────────────── 3. judge_eval_report ──────────────────────────


class TestJudgeEvalReport(unittest.TestCase):
    # 考题顺序：T1,T2,T3,B1,B2,B3,A1,A2（与 _valid_eval_system 的 exam_sets 顺序一致）
    DESCRIPTIONS = ["T1 核心题", "T2 非关键", "T3 非关键",
                    "B1", "B2", "B3", "A1 注入", "A2 泄露"]

    def setUp(self):
        self.system = _valid_eval_system()
        self.pass_lines = self.system["pass_lines"]
        self.exam_sets = self.system["exam_sets"]

    def test_both_pass(self):
        # 8 题全过：overall 100%，关键题（T1+A1+A2=3）全过 critical 100%
        parsed = parse_promptfoo_results(_results_json(
            8, 0, per_exam_success=[True]*8, descriptions=self.DESCRIPTIONS))
        judge = judge_eval_report(parsed, self.pass_lines, self.exam_sets)
        self.assertTrue(judge["passed"])

    def test_overall_fail(self):
        # 8 题只过 5：overall 62.5% < 80%
        success = [True]*5 + [False]*3
        parsed = parse_promptfoo_results(_results_json(
            5, 3, per_exam_success=success, descriptions=self.DESCRIPTIONS))
        judge = judge_eval_report(parsed, self.pass_lines, self.exam_sets)
        self.assertFalse(judge["passed"])

    def test_critical_fail(self):
        # overall 过但关键题（A1/A2）没过：前 5 题过，A1,A2 失败 -> critical 1/3
        success = [True]*5 + [False]*2 + [True]
        parsed = parse_promptfoo_results(_results_json(
            6, 2, per_exam_success=success, descriptions=self.DESCRIPTIONS))
        judge = judge_eval_report(parsed, self.pass_lines, self.exam_sets)
        self.assertFalse(judge["passed"])
        self.assertTrue(len(judge["failed_critical_descriptions"]) > 0)

    def test_zero_critical_no_gate(self):
        system = _valid_eval_system()
        for es in system["exam_sets"]:
            if es["layer"] == "adversarial":
                es["exams"] = []
            if es["layer"] == "typical":
                for e in es["exams"]:
                    e["critical"] = False
        parsed = parse_promptfoo_results(_results_json(
            8, 0, per_exam_success=[True]*8, descriptions=self.DESCRIPTIONS))
        judge = judge_eval_report(parsed, system["pass_lines"], system["exam_sets"])
        self.assertTrue(judge["passed"])

    def test_total_zero_overall_zero(self):
        parsed = parse_promptfoo_results(_results_json(0, 0))
        judge = judge_eval_report(parsed, self.pass_lines, self.exam_sets)
        self.assertEqual(judge["overall_rate"], 0.0)
        self.assertFalse(judge["passed"])


# ────────────────────────── 4. 节点协议 ──────────────────────────


class TestEvalRunNode(unittest.TestCase):
    DESCRIPTIONS = ["T1 核心题", "T2 非关键", "T3 非关键",
                    "B1", "B2", "B3", "A1 注入", "A2 泄露"]

    def _base_state(self) -> dict:
        system = _valid_eval_system()
        return {
            "ai_core": True,
            "requirement_name": "eval-smoke",
            "eval_yaml_draft": render_promptfoo_yaml(system),
            "eval_archive": {"pass_lines": system["pass_lines"]},
            "eval_system": system,
            "eval_run_count": 0,
        }

    def test_non_ai_passthrough(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            state["ai_core"] = False
            out, payloads = run_eval_node(state, deps, answers=[])
            self.assertEqual(out, {})
            self.assertEqual(payloads, [])

    def test_await_prompt_then_continue(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            # 第一次 interrupt（await_prompt）恢复后创建 system_prompt.txt，继续执行
            out, payloads = run_eval_node(
                state, deps, answers=["ok"],
                prompt_exists_after=1,
                returncodes=[EXIT_OK],
                results_json_list=[_results_json(8, 0, per_exam_success=[True]*8, descriptions=self.DESCRIPTIONS)],
            )
            self.assertEqual(payloads[0]["status"], "await_prompt")
            self.assertIn("prompt_path", payloads[0])
            # 恢复后成功执行并达标
            self.assertTrue(out["eval_report"]["passed"])
            self.assertEqual(out["eval_run_count"], 1)  # await_prompt 阶段不计

    def test_tool_error_then_pass(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            out, payloads = run_eval_node(
                state, deps,
                answers=["retry"],  # tool_error 后恢复
                returncodes=[EXIT_TOOL_ERROR, EXIT_OK],
                results_json_list=[_results_json(8, 0, per_exam_success=[True]*8, descriptions=self.DESCRIPTIONS)],
            )
            self.assertEqual(payloads[0]["status"], "tool_error")
            self.assertTrue(out["eval_report"]["passed"])
            self.assertEqual(out["eval_run_count"], 1)  # 只成功那次计数

    def test_eval_failed_then_pass_on_rerun(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            success_fail = [True]*5 + [False]*3  # overall 62.5% 不达标
            success_pass = [True]*8
            out, payloads = run_eval_node(
                state, deps,
                answers=["rerun"],
                returncodes=[EXIT_CONTENT_FAIL, EXIT_OK],
                results_json_list=[
                    _results_json(5, 3, per_exam_success=success_fail, descriptions=self.DESCRIPTIONS),
                    _results_json(8, 0, per_exam_success=success_pass, descriptions=self.DESCRIPTIONS),
                ],
            )
            self.assertEqual(payloads[0]["status"], "eval_failed")
            self.assertIn("report", payloads[0])
            self.assertTrue(out["eval_report"]["passed"])
            self.assertEqual(out["eval_run_count"], 2)  # 两次执行都计数

    def test_pass_path_count_one(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            out, payloads = run_eval_node(
                state, deps, answers=[],
                returncodes=[EXIT_OK],
                results_json_list=[_results_json(8, 0, per_exam_success=[True]*8, descriptions=self.DESCRIPTIONS)],
            )
            self.assertEqual(payloads, [])
            self.assertTrue(out["eval_report"]["passed"])
            self.assertEqual(out["eval_run_count"], 1)
            self.assertIn("config_path", out["eval_artifacts"])
            self.assertIn("results_path", out["eval_artifacts"])
            self.assertIn("report_path", out["eval_artifacts"])

    def test_missing_upstream_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = self._base_state()
            state["eval_yaml_draft"] = ""
            node = make_eval_run(deps)
            with self.assertRaises(Exception):
                node(state)


# ────────────────────────── 5. 路由 ──────────────────────────


class TestRouting(unittest.TestCase):
    def test_route_after_eval_run_pass(self):
        self.assertEqual(
            route_after_eval_run({"eval_report": {"passed": True}}),
            "launch_plan",
        )

    def test_route_after_eval_run_fail(self):
        self.assertEqual(
            route_after_eval_run({"eval_report": {"passed": False}}),
            "eval_run",
        )

    def test_route_after_eval_run_empty(self):
        self.assertEqual(route_after_eval_run({}), "eval_run")

    def test_issue_confirm_ai_core_goes_eval_run(self):
        state = {
            "issue_plan": {"tickets": [1]},
            "prd_rewrite_feedback": "",
            "issue_revision_feedback": "",
            "ai_core": True,
        }
        self.assertEqual(route_after_issue_confirm(state), "eval_run")

    def test_issue_confirm_normal_goes_artifact(self):
        state = {
            "issue_plan": {"tickets": [1]},
            "prd_rewrite_feedback": "",
            "issue_revision_feedback": "",
            "ai_core": False,
        }
        self.assertEqual(route_after_issue_confirm(state), "artifact_persist")

    def test_issue_confirm_redraft_unchanged(self):
        state = {
            "issue_plan": {"tickets": [1]},
            "prd_rewrite_feedback": "",
            "issue_revision_feedback": "改一下",
            "ai_core": True,
        }
        self.assertEqual(route_after_issue_confirm(state), "issue_splitting")


# ────────────────────────── 6. 图编译 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_seventeen_nodes(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            graph = build_graph(deps, db_path=str(Path(tmp) / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            self.assertIn("eval_run", names)
            # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段新增 bake_off 后为 17 个真实节点
            # [C 2026-09-14 by codebuddy-ds41flash] S041 新增 requirement_refine 后为 18 个真实节点
            # （本测试仅随图节点数增长同步计数断言，eval_run 节点自身行为断言未改动）
            self.assertIn("bake_off", names)
            real = names - {"__start__", "__end__"}
            self.assertEqual(len(real), 18)


# ────────────────────────── 7. QUESTION 文案 ──────────────────────────


class TestQuestionText(unittest.TestCase):
    @staticmethod
    def _load_workflow_module():
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_eval_run",
            REPO_ROOT / "scripts" / "run_prd_workflow.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def test_eval_run_await_prompt_question(self):
        mod = self._load_workflow_module()
        q = mod._build_question(
            "eval_run",
            {"node": "eval_run", "status": "await_prompt",
             "prompt_path": "/tmp/p.txt"},
            {},
        )
        self.assertIn("system_prompt.txt", q)

    def test_eval_run_tool_error_question(self):
        mod = self._load_workflow_module()
        q = mod._build_question(
            "eval_run",
            {"node": "eval_run", "status": "tool_error", "reason": "退出码 1"},
            {},
        )
        self.assertIn("工具", q)

    def test_eval_run_eval_failed_question(self):
        mod = self._load_workflow_module()
        q = mod._build_question(
            "eval_run",
            {"node": "eval_run", "status": "eval_failed",
             "report": {"passed": False}},
            {},
        )
        self.assertIn("不设自动放行", q)


# [C 2026-09-12 by MA] tests/test_eval_run.py 新增完成
