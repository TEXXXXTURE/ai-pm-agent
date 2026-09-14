# [C 2026-09-13 by codebuddy-ds41flash] 对比选型模型节点（bake_off）自测
"""bake_off 零 API 假测试：patch 掉 nodes.bake_off.interrupt 与 nodes.eval_run.subprocess.run，
subprocess 全部 mock，不发起任何真实模型调用、不跑真 Promptfoo、不碰真机。

覆盖：
1. classify_bakeoff_answer 三态（run/skip/other）+ 否定式（"不跳过"=跑、"不跑"=跳过）；
2. build_provider_config：单 provider、prompts 替换、chat/reasoner 两种 config、非法 YAML 返回空串；
3. aggregate_candidate_stats：P50/P95 手算例（1 题 / 偶数题 / 奇数题 / 空列表）、
   整体通过率、关键题通过率（复用 eval_run 口径）、cost/token_total；
4. judge_bakeoff：最高通过率胜、整体平局比关键题率、再平局比成本、全平局按顺序兜底、空列表；
5. route_after_bake_off：completed/skipped/缺失三态；
6. 节点协议（mock interrupt + subprocess）：
   - 非 ai_core 直通返回 {} 且零调用；
   - skip：零 subprocess、写 model_selection.status=skipped + 默认 DeepSeek；
   - run 双 provider 成功：落两份单模型配置/结果、写 model_selection + 追加 eval_archive、
     写 bakeoff_artifacts 三键、报告落盘、配置只保留当前 provider（chat/reasoner 分档）；
   - await_decision 其他文本 -> 继续 interrupt（不猜不空转），再给"跑"才继续；
   - await_prompt：缺 system_prompt.txt -> interrupt(status=await_prompt) 含 prompt_path；
   - tool_error：subprocess 返回码 1 -> interrupt(status=tool_error) 含 provider_id；
   - 上游缺失 -> NodeExecutionError；缺 bake_off_config -> NodeExecutionError；
7. 图接线：bake_off 注册、17 节点、eval_confirm 确认分支去 bake_off、bake_off 两态；
8. QUESTION 文案：run_prd_workflow._build_question 对 bake_off 三态输出对应提示；
9. 配置与字段：config.yaml bake_off 段候选、state 默认 bakeoff_artifacts、hitl_cli 展示 model_selection。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe -m pytest tests/test_bake_off.py -v
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import MagicMock, patch

# Windows GBK 控制台兜底
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
from kernel.exceptions import NodeExecutionError  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from kernel.state import default_state  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.bake_off import (  # noqa: E402
    aggregate_candidate_stats,
    build_provider_config,
    classify_bakeoff_answer,
    judge_bakeoff,
    make_bake_off,
    route_after_bake_off,
)
from nodes.eval_design import render_promptfoo_yaml  # noqa: E402
from nodes.eval_run import EXIT_CONTENT_FAIL, EXIT_OK, EXIT_TOOL_ERROR  # noqa: E402

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"

# 8 题描述（与 _valid_eval_system 的考题顺序一致）
DESCRIPTIONS = ["T1 核心题", "T2 非关键", "T3 非关键", "B1", "B2", "B3", "A1 注入", "A2 泄露"]

DEFAULT_CANDIDATES = {
    "candidates": [
        {"id": "deepseek:deepseek-chat", "label": "DeepSeek-Chat",
         "kind": "chat", "api_key_env": "DEEPSEEK_API_KEY"},
        {"id": "deepseek:deepseek-reasoner", "label": "DeepSeek-Reasoner",
         "kind": "reasoner", "api_key_env": "DEEPSEEK_API_KEY"},
    ]
}


# ────────────────────────── 测试替身与夹具 ──────────────────────────


class FakeLLM:
    def __init__(self, json_queue=None):
        self.json_queue = list(json_queue or [])
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        return self.json_queue.pop(0) if self.json_queue else {}


def make_deps(tmp_dir: Path, fake_llm=None, bake_off_config=None, eval_tool=True) -> NodeDeps:
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
        eval_tool={"promptfoo_dir": str(tmp_dir / "pf")} if eval_tool else None,
        bake_off_config=DEFAULT_CANDIDATES if bake_off_config is None else bake_off_config,
    )


def _valid_eval_system() -> dict:
    """合法评测体系（典型3/边界3/对抗2/replay0；T1 标 critical，对抗层视为关键）。"""
    def exam(exam_id, layer, critical=False):
        return {
            "id": exam_id, "layer": layer, "description": exam_id,
            "prompt_hint": f"in-{exam_id}", "scorer": "assertion",
            "assertion": "contains: x", "judge_rubric": None,
            "manual_review_ratio": 0.0, "critical": critical,
        }
    return {
        "purpose": "测试用",
        "exam_sets": [
            {"layer": "typical", "exams": [
                exam("T1 核心题", "typical", True),
                exam("T2 非关键", "typical"),
                exam("T3 非关键", "typical"),
            ], "placeholder_note": ""},
            {"layer": "boundary", "exams": [
                exam("B1", "boundary"), exam("B2", "boundary"), exam("B3", "boundary"),
            ], "placeholder_note": ""},
            {"layer": "adversarial", "exams": [
                exam("A1 注入", "adversarial"), exam("A2 泄露", "adversarial"),
            ], "placeholder_note": ""},
            {"layer": "replay", "exams": [], "placeholder_note": "二期填充"},
        ],
        "pass_lines": {"overall_pass_rate": 0.8, "critical_pass_rate": 0.9, "note": "测试"},
    }


def _results(per_success: list[bool], descriptions: list[str] | None = None,
             latencies: list[int] | None = None, cost: float = 0.01,
             token_total: int = 150) -> dict:
    """构造 Promptfoo results.json（stats + 逐题）。"""
    descriptions = descriptions or DESCRIPTIONS
    latencies = latencies if latencies is not None else [100] * len(per_success)
    successes = sum(1 for ok in per_success if ok)
    per_exam = []
    for i, ok in enumerate(per_success):
        per_exam.append({
            "testIdx": i,
            "description": descriptions[i] if i < len(descriptions) else f"题{i}",
            "success": ok,
            "score": 1.0 if ok else 0.0,
            "latencyMs": latencies[i] if i < len(latencies) else 100,
            "response": {"output": f"out{i}"},
            "gradingResult": {"pass": ok},
        })
    return {
        "stats": {
            "successes": successes,
            "failures": len(per_success) - successes,
            "errors": 0,
            "tokenUsage": {"prompt": 100, "completion": 50, "total": token_total},
            "totalCost": cost,
            "durationMs": 2000,
        },
        "results": per_exam,
    }


def _base_state(ai_core: bool = True) -> dict:
    system = _valid_eval_system()
    return {
        "ai_core": ai_core,
        "requirement_name": "bakeoff-smoke",
        "eval_yaml_draft": render_promptfoo_yaml(system),
        "eval_archive": {"pass_lines": system["pass_lines"], "archive_version": 1},
        "eval_system": system,
    }


def run_bakeoff_node(state, deps, answers, runs=None):
    """运行 bake_off 节点，mock interrupt 按 answers 回放、subprocess 按 runs 回放。

    - answers: interrupt 的 resume 值序列；
    - runs: 每次 subprocess 的 (returncode, result) 序列；result 为 dict（写 JSON）/
      str（原样写，用于非法 JSON）/ None（不写文件）；缺省 []（skip 路径不调 subprocess）。
    - await_prompt 中断时，按载荷 prompt_path 自动创建 system_prompt.txt（模拟用户放入）。
    """
    payloads: list[dict] = []
    ans_queue = list(answers)
    run_queue = list(runs if runs is not None else [])

    def fake_interrupt(value):
        payloads.append(value)
        # 模拟用户按中断提示放入被测 prompt 文件
        if isinstance(value, dict) and value.get("status") == "await_prompt":
            prompt_path = value.get("prompt_path")
            if prompt_path:
                p = Path(prompt_path)
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("你是被测系统", encoding="utf-8")
        return ans_queue.pop(0) if ans_queue else None

    def fake_run(command, **kwargs):
        rc, result = run_queue.pop(0) if run_queue else (EXIT_OK, _results([True] * 8))
        proc = MagicMock()
        proc.returncode = rc
        proc.stderr = ""
        proc.stdout = ""
        if rc in (EXIT_OK, EXIT_CONTENT_FAIL) and result is not None:
            cwd = Path(kwargs.get("cwd", "."))
            if isinstance(result, str):
                (cwd / "results.json").write_text(result, encoding="utf-8")
            else:
                (cwd / "results.json").write_text(
                    json.dumps(result, ensure_ascii=False), encoding="utf-8"
                )
        return proc

    node = make_bake_off(deps)
    with patch("nodes.bake_off.interrupt", side_effect=fake_interrupt), \
         patch("nodes.eval_run.subprocess.run", side_effect=fake_run):
        out = node(state)
    return out, payloads


def _eval_dir(deps: NodeDeps, name: str = "bakeoff-smoke") -> Path:
    return Path(deps.artifacts.output_root) / sanitize_name(name) / "评测"


def _preset_prompt(deps: NodeDeps, name: str = "bakeoff-smoke") -> None:
    """预置被测 system_prompt.txt，使节点跳过 await_prompt（隔离被测分支）。"""
    eval_dir = _eval_dir(deps, name)
    eval_dir.mkdir(parents=True, exist_ok=True)
    (eval_dir / "system_prompt.txt").write_text("你是被测系统", encoding="utf-8")


# ────────────────────────── 1. classify_bakeoff_answer ──────────────────────────


class TestClassifyBakeoffAnswer(unittest.TestCase):
    def test_run_words(self):
        for word in ("跑", "开始", "确认", "横跑", "执行", "运行", "对比", "run", "yes", "ok"):
            self.assertEqual(classify_bakeoff_answer(word), "run", msg=repr(word))

    def test_skip_words(self):
        for word in ("跳过", "不用", "先用默认", "用默认", "直接用 deepseek", "skip", "no"):
            self.assertEqual(classify_bakeoff_answer(word), "skip", msg=repr(word))

    def test_negated_skip_is_run(self):
        # 否定式：否定"跳过"= 要跑（防误判）
        for word in ("不跳过", "别跳过", "不用跳过", "不要跳过"):
            self.assertEqual(classify_bakeoff_answer(word), "run", msg=repr(word))

    def test_negated_run_is_skip(self):
        # 否定式：否定"跑"= 跳过
        for word in ("不跑", "别跑"):
            self.assertEqual(classify_bakeoff_answer(word), "skip", msg=repr(word))

    def test_other_text(self):
        for word in ("随便看看", "先讨论一下", "我考虑考虑", None, "", "   "):
            self.assertEqual(classify_bakeoff_answer(word), "other", msg=repr(word))


# ────────────────────────── 2. build_provider_config ──────────────────────────


class TestBuildProviderConfig(unittest.TestCase):
    def setUp(self):
        self.draft = render_promptfoo_yaml(_valid_eval_system())

    def test_single_provider_and_prompts_replaced(self):
        text = build_provider_config(self.draft, "deepseek:deepseek-chat", "chat")
        doc = yaml.safe_load(text)
        self.assertEqual(doc["prompts"], ["./system_prompt.txt"])
        self.assertEqual(len(doc["providers"]), 1)
        self.assertEqual(doc["providers"][0]["id"], "deepseek:deepseek-chat")

    def test_chat_config(self):
        doc = yaml.safe_load(
            build_provider_config(self.draft, "deepseek:deepseek-chat", "chat")
        )
        config = doc["providers"][0]["config"]
        self.assertEqual(config, {"temperature": 0, "max_tokens": 2048, "showThinking": False})

    def test_reasoner_config_only_max_tokens(self):
        doc = yaml.safe_load(
            build_provider_config(self.draft, "deepseek:deepseek-reasoner", "reasoner")
        )
        config = doc["providers"][0]["config"]
        self.assertEqual(config, {"max_tokens": 2048})
        self.assertNotIn("temperature", config)
        self.assertNotIn("showThinking", config)

    def test_tests_preserved(self):
        doc = yaml.safe_load(build_provider_config(self.draft, "x:y", "chat"))
        self.assertEqual(len(doc["tests"]), 8)

    def test_llm_rubric_judge_provider_backfilled(self):
        # S039：旧草案里缺 provider 的 llm-rubric 断言必须补上固定阅卷模型
        old_draft = yaml.safe_dump(
            {
                "prompts": ["{{prd_core_task_prompt}}"],
                "providers": ["deepseek:deepseek-v4-flash"],
                "tests": [
                    {
                        "description": "[typical] J1 旧题",
                        "vars": {"input": "x"},
                        "assert": [{"type": "llm-rubric", "value": "标准"}],
                    },
                    {
                        "description": "[typical] J2 已指定阅卷模型",
                        "vars": {"input": "y"},
                        "assert": [
                            {"type": "llm-rubric", "value": "标准", "provider": "custom:judge"}
                        ],
                    },
                ],
            },
            allow_unicode=True,
        )
        doc = yaml.safe_load(build_provider_config(old_draft, "x:y", "chat"))
        self.assertEqual(doc["tests"][0]["assert"][0]["provider"], "deepseek:deepseek-v4-flash")
        # 已有 provider 的断言原样保留
        self.assertEqual(doc["tests"][1]["assert"][0]["provider"], "custom:judge")

    def test_invalid_yaml_returns_empty(self):
        self.assertEqual(build_provider_config("{{not valid yaml", "x:y", "chat"), "")
        self.assertEqual(build_provider_config("- a list item", "x:y", "chat"), "")


# ────────────────────────── 3. aggregate_candidate_stats ──────────────────────────


class TestAggregateCandidateStats(unittest.TestCase):
    def test_empty_latencies(self):
        stats = aggregate_candidate_stats({"per_exam": []})
        self.assertEqual(stats["latency_p50_ms"], 0.0)
        self.assertEqual(stats["latency_p95_ms"], 0.0)

    def test_single_latency_equal(self):
        stats = aggregate_candidate_stats({"per_exam": [{"latency_ms": 100}]})
        self.assertEqual(stats["latency_p50_ms"], 100.0)
        self.assertEqual(stats["latency_p95_ms"], 100.0)

    def test_even_latencies(self):
        # 最近秩法：n=4 -> P50=ceil(0.5*4)-1=1 -> 20；P95=ceil(3.8)-1=3 -> 40
        per_exam = [{"latency_ms": v} for v in (10, 20, 30, 40)]
        stats = aggregate_candidate_stats({"per_exam": per_exam})
        self.assertEqual(stats["latency_p50_ms"], 20.0)
        self.assertEqual(stats["latency_p95_ms"], 40.0)

    def test_odd_latencies(self):
        # 最近秩法：n=5 -> P50=ceil(2.5)-1=2 -> 30；P95=ceil(4.75)-1=4 -> 50
        per_exam = [{"latency_ms": v} for v in (10, 20, 30, 40, 50)]
        stats = aggregate_candidate_stats({"per_exam": per_exam})
        self.assertEqual(stats["latency_p50_ms"], 30.0)
        self.assertEqual(stats["latency_p95_ms"], 50.0)

    def test_non_numeric_latency_ignored(self):
        per_exam = [{"latency_ms": None}, {"latency_ms": "x"}, {"latency_ms": 42}]
        stats = aggregate_candidate_stats({"per_exam": per_exam})
        self.assertEqual(stats["latency_p50_ms"], 42.0)
        self.assertEqual(stats["latency_p95_ms"], 42.0)

    def test_overall_rate_and_cost_token(self):
        parsed = {"successes": 3, "failures": 1, "errors": 0, "total": 4,
                  "cost": 0.02, "token_usage": {"total": 400},
                  "per_exam": [{"latency_ms": 10}]}
        stats = aggregate_candidate_stats(parsed)
        self.assertEqual(stats["overall_rate"], 0.75)
        self.assertEqual(stats["cost"], 0.02)
        self.assertEqual(stats["token_total"], 400)

    def test_critical_rate_reuses_eval_run(self):
        # 关键题：T1 + A1 + A2 = 3；A1/A2 失败 -> 1/3
        parsed = {"successes": 6, "failures": 2, "errors": 0, "total": 8,
                  "per_exam": [
                      {"description": d, "success": ok}
                      for d, ok in zip(DESCRIPTIONS, [True] * 6 + [False, False])
                  ]}
        stats = aggregate_candidate_stats(parsed, _valid_eval_system()["exam_sets"])
        self.assertAlmostEqual(stats["critical_rate"], 1 / 3)
        self.assertEqual(stats["critical_total"], 3)

    def test_zero_critical_no_gate(self):
        system = _valid_eval_system()
        for exam_set in system["exam_sets"]:
            if exam_set["layer"] == "adversarial":
                exam_set["exams"] = []
            if exam_set["layer"] == "typical":
                for exam in exam_set["exams"]:
                    exam["critical"] = False
        stats = aggregate_candidate_stats({"per_exam": []}, system["exam_sets"])
        self.assertEqual(stats["critical_rate"], 1.0)
        self.assertEqual(stats["critical_total"], 0)


# ────────────────────────── 4. judge_bakeoff ──────────────────────────


def _cand(pid, overall, critical, cost):
    return {"provider_id": pid, "overall_rate": overall,
            "critical_rate": critical, "cost": cost}


class TestJudgeBakeoff(unittest.TestCase):
    def test_highest_overall_wins(self):
        result = judge_bakeoff([_cand("a", 0.5, 0.9, 0.1), _cand("b", 0.9, 0.1, 0.9)])
        self.assertEqual(result["recommended_provider_id"], "b")

    def test_tie_overall_then_critical(self):
        result = judge_bakeoff([_cand("a", 0.9, 0.3, 0.1), _cand("b", 0.9, 0.9, 0.9)])
        self.assertEqual(result["recommended_provider_id"], "b")

    def test_tie_overall_critical_then_cost(self):
        result = judge_bakeoff([_cand("a", 0.9, 0.9, 0.5), _cand("b", 0.9, 0.9, 0.1)])
        self.assertEqual(result["recommended_provider_id"], "b")

    def test_full_tie_keeps_first(self):
        result = judge_bakeoff([_cand("a", 0.9, 0.9, 0.1), _cand("b", 0.9, 0.9, 0.1)])
        self.assertEqual(result["recommended_provider_id"], "a")

    def test_empty_candidates(self):
        result = judge_bakeoff([])
        self.assertIsNone(result["recommended_provider_id"])
        self.assertIsNone(result["recommended"])
        self.assertEqual(result["candidates"], [])


# ────────────────────────── 5. route_after_bake_off ──────────────────────────


class TestRoute(unittest.TestCase):
    def test_completed_and_skipped_go_issue_splitting(self):
        for status in ("completed", "skipped"):
            self.assertEqual(
                route_after_bake_off({"model_selection": {"status": status}}),
                "issue_splitting",
                msg=status,
            )

    def test_missing_or_other_loops(self):
        for state in ({}, {"model_selection": {}}, {"model_selection": {"status": "x"}}):
            self.assertEqual(route_after_bake_off(state), "bake_off", msg=repr(state))


# ────────────────────────── 6. 节点协议 ──────────────────────────


class TestBakeOffNode(unittest.TestCase):
    def test_non_ai_passthrough_zero_calls(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = _base_state(ai_core=False)
            with patch("nodes.eval_run.subprocess.run") as mock_run:
                out, payloads = run_bakeoff_node(state, deps, answers=[])
            self.assertEqual(out, {})
            self.assertEqual(payloads, [])
            mock_run.assert_not_called()

    def test_await_decision_payload_carries_candidates(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["跳过"]
            )
            self.assertEqual(payloads[0]["node"], "bake_off")
            self.assertEqual(payloads[0]["status"], "await_decision")
            self.assertIn("暂无历史选型记录", payloads[0]["reason"])
            ids = [c["id"] for c in payloads[0]["candidates"]]
            self.assertEqual(ids, ["deepseek:deepseek-chat", "deepseek:deepseek-reasoner"])
            self.assertEqual(out["model_selection"]["status"], "skipped")

    def test_skip_zero_subprocess_and_default_recommended(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            with patch("nodes.eval_run.subprocess.run") as mock_run:
                out, payloads = run_bakeoff_node(_base_state(), deps, answers=["跳过"])
            mock_run.assert_not_called()
            selection = out["model_selection"]
            self.assertEqual(selection["status"], "skipped")
            self.assertEqual(selection["recommended"]["provider_id"], "deepseek:deepseek-chat")
            self.assertEqual(selection["recommended"]["label"], "DeepSeek-Chat")
            self.assertEqual(selection["skip_reason"], "跳过")
            self.assertIn("ran_at", selection)
            self.assertEqual(route_after_bake_off(out), "issue_splitting")

    def test_other_answer_keeps_interrupting(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["随便看看", "跳过"]
            )
            # 第一、二次都是 await_decision（不猜、继续等明确答复）
            self.assertEqual([p["status"] for p in payloads], ["await_decision", "await_decision"])
            self.assertEqual(out["model_selection"]["status"], "skipped")

    def test_run_two_providers_success(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path)
            chat_ok = _results([True] * 8, cost=0.02)
            reasoner_mixed = _results(
                [True] * 6 + [False, False], cost=0.01
            )
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["跑"],
                runs=[(EXIT_OK, chat_ok), (EXIT_CONTENT_FAIL, reasoner_mixed)],
            )
            self.assertEqual(payloads[0]["status"], "await_decision")

            selection = out["model_selection"]
            self.assertEqual(selection["status"], "completed")
            self.assertEqual(len(selection["candidates"]), 2)
            # 通过率高者（chat 100%）胜
            self.assertEqual(selection["recommended"]["provider_id"], "deepseek:deepseek-chat")
            self.assertEqual(selection["recommended"]["overall_rate"], 1.0)
            # 逐模型三维字段齐全
            for cand in selection["candidates"]:
                for field in ("provider_id", "label", "overall_rate", "critical_rate",
                              "successes", "failures", "errors", "cost", "token_total",
                              "latency_p50_ms", "latency_p95_ms"):
                    self.assertIn(field, cand, msg=field)

            # eval_archive 追加 model_selection（先读后并）
            self.assertEqual(out["eval_archive"]["archive_version"], 1)
            self.assertEqual(out["eval_archive"]["model_selection"], selection)

            # bakeoff_artifacts 三键
            artifacts = out["bakeoff_artifacts"]
            self.assertIn("report_path", artifacts)
            self.assertEqual(len(artifacts["config_paths"]), 2)
            self.assertEqual(len(artifacts["results_paths"]), 2)
            self.assertTrue(Path(artifacts["report_path"]).exists())
            report = Path(artifacts["report_path"]).read_text(encoding="utf-8")
            self.assertIn("模型对比选型报告", report)
            self.assertIn("deepseek:deepseek-chat", report)

            # 落盘位置统一在「评测」目录
            eval_dir = _eval_dir(deps)
            for path in artifacts["config_paths"] + artifacts["results_paths"]:
                self.assertEqual(Path(path).parent, eval_dir)
            # 每份配置只保留当前 provider，且 chat/reasoner 分档
            by_id = {}
            for path in artifacts["config_paths"]:
                doc = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
                self.assertEqual(len(doc["providers"]), 1)
                self.assertEqual(doc["prompts"], ["./system_prompt.txt"])
                by_id[doc["providers"][0]["id"]] = doc["providers"][0]["config"]
            self.assertEqual(
                by_id["deepseek:deepseek-chat"],
                {"temperature": 0, "max_tokens": 2048, "showThinking": False},
            )
            self.assertEqual(by_id["deepseek:deepseek-reasoner"], {"max_tokens": 2048})

    def test_await_prompt_when_file_missing(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["跑", "ok"],
                runs=[(EXIT_OK, _results([True] * 8)), (EXIT_OK, _results([True] * 8))],
            )
            self.assertEqual(payloads[0]["status"], "await_decision")
            self.assertEqual(payloads[1]["status"], "await_prompt")
            self.assertIn("prompt_path", payloads[1])
            self.assertEqual(out["model_selection"]["status"], "completed")

    def test_tool_error_interrupt_then_rerun(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            _preset_prompt(deps)
            ok = _results([True] * 8)
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["跑", "retry"],
                runs=[
                    (EXIT_TOOL_ERROR, None),
                    (EXIT_OK, ok),
                    (EXIT_OK, ok),
                ],
            )
            self.assertEqual(payloads[0]["status"], "await_decision")
            self.assertEqual(payloads[1]["status"], "tool_error")
            self.assertEqual(payloads[1]["provider_id"], "deepseek:deepseek-chat")
            self.assertEqual(out["model_selection"]["status"], "completed")

    def test_invalid_results_json_is_tool_error(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            _preset_prompt(deps)
            ok = _results([True] * 8)
            out, payloads = run_bakeoff_node(
                _base_state(), deps, answers=["跑", "retry"],
                runs=[
                    (EXIT_OK, "{not valid json"),
                    (EXIT_OK, ok),
                    (EXIT_OK, ok),
                ],
            )
            self.assertEqual(payloads[1]["status"], "tool_error")
            self.assertIn("results.json", payloads[1]["reason"])
            self.assertEqual(out["model_selection"]["status"], "completed")

    def test_missing_upstream_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            state = _base_state()
            state["eval_yaml_draft"] = ""
            with self.assertRaises(NodeExecutionError):
                make_bake_off(deps)(state)

    def test_missing_bake_off_config_raises(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp), bake_off_config={"candidates": []})
            with self.assertRaises(NodeExecutionError):
                make_bake_off(deps)(_base_state())

    def test_missing_eval_tool_raises_on_run(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp), eval_tool=False)
            with self.assertRaises(NodeExecutionError):
                run_bakeoff_node(_base_state(), deps, answers=["跑"])


# ────────────────────────── 7. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_bake_off_registered_and_wired(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path)
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            self.assertIn("bake_off", names)
            # [C 2026-09-14 by codebuddy-ds41flash] S041 新增 requirement_refine 后为 18 个真实节点
            real = names - {"__start__", "__end__"}
            self.assertEqual(len(real), 18)
            drawn = graph.get_graph().draw_mermaid()
            self.assertTrue(
                any("eval_confirm" in ln and "bake_off" in ln
                    for ln in drawn.splitlines()),
                msg=drawn,
            )
            self.assertIn("bake_off -.-> issue_splitting", drawn)
            self.assertIn("bake_off -.-> bake_off", drawn)


# ────────────────────────── 8. QUESTION 文案 ──────────────────────────


class TestQuestionText(unittest.TestCase):
    @staticmethod
    def _load_workflow_module():
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_bakeoff",
            REPO_ROOT / "scripts" / "run_prd_workflow.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_await_decision_question_mentions_real_calls(self):
        module = self._load_workflow_module()
        question = module._build_question(
            "bake_off", {"node": "bake_off", "status": "await_decision"}, {}
        )
        self.assertIn("对比选型", question)
        self.assertIn("真实产生多次模型调用", question)
        self.assertIn("candidates", question)

    def test_await_prompt_question(self):
        module = self._load_workflow_module()
        question = module._build_question(
            "bake_off", {"node": "bake_off", "status": "await_prompt",
                         "prompt_path": "/tmp/p.txt"}, {}
        )
        self.assertIn("system_prompt.txt", question)

    def test_tool_error_question(self):
        module = self._load_workflow_module()
        question = module._build_question(
            "bake_off", {"node": "bake_off", "status": "tool_error", "reason": "退出码 1"}, {}
        )
        self.assertIn("provider_id", question)

    def test_bake_off_in_escalation_passthrough(self):
        module = self._load_workflow_module()
        payload = {
            "node": "bake_off",
            "status": "tool_error",
            "reason": "退出码 1",
            "provider_id": "deepseek:deepseek-chat",
            "requirement_name": "bakeoff-smoke",
        }
        graph = MagicMock()
        graph.get_state.return_value.values = {}
        buf = io.StringIO()
        with redirect_stdout(buf):
            module._emit_hitl(graph, {}, "tid-bakeoff", payload)
        out = buf.getvalue()
        self.assertIn("NODE: bake_off", out)
        self.assertIn("status: tool_error", out)
        self.assertIn("reason:", out)


# ────────────────────────── 9. 配置与字段 ──────────────────────────


class TestConfigAndFields(unittest.TestCase):
    def test_config_yaml_bake_off_section(self):
        cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
        candidates = cfg["bake_off"]["candidates"]
        self.assertEqual(
            [c["id"] for c in candidates],
            ["deepseek:deepseek-chat", "deepseek:deepseek-reasoner"],
        )
        for cand in candidates:
            self.assertIn("label", cand)
            self.assertIn("kind", cand)
            self.assertEqual(cand["api_key_env"], "DEEPSEEK_API_KEY")

    def test_default_state_has_bakeoff_artifacts(self):
        state = default_state()
        self.assertEqual(state["bakeoff_artifacts"], {})
        self.assertEqual(state["model_selection"], {})

    def test_hitl_fields_include_model_selection(self):
        from cli.hitl_cli import DECISION_MATERIAL_FIELDS, PAYLOAD_RECAP_FIELDS
        self.assertIn("model_selection", DECISION_MATERIAL_FIELDS)
        self.assertIn("candidates", PAYLOAD_RECAP_FIELDS)

    def test_artifact_dir_routes_bakeoff_to_pingce(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            manager = ArtifactManager(str(Path(tmp) / "out"), str(TEMPLATE_DIR), str(ASSETS_DIR))
            report = manager.save("x", "req", "bakeoff_report", ext=".md")
            self.assertEqual(Path(report).parent.name, "评测")
            config = manager.save("y", "req", "bakeoff-deepseek_chat-eval_config", ext=".yaml")
            self.assertEqual(Path(config).parent.name, "评测")
            results = manager.save("z", "req", "bakeoff-deepseek_chat-results", ext=".json")
            self.assertEqual(Path(results).parent.name, "评测")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# [C 2026-09-13 by codebuddy-ds41flash] tests/test_bake_off.py 新增完成
