# [C 2026-09-10] PRD 评审门（prd_review）自测
"""prd_review 节点零成本自测：全部使用可编排 FakeLLM，不发起任何真实模型调用。

覆盖：
1. judge_scores 硬判纯函数四场景（pass / pass_with_warning / reject / 边界 3.5）；
1.5 阻断一票否决（阻断级 blocker 不看分数直接打回，重要/建议不否决） [C 2026-09-12]；
2. 节点级：reject 计数递增+反馈文本、连续 reject 第 3 轮 forced 放行、pass 计数不变；
3. route_after_review 路由；
4. build_graph 图接线编译（不调模型）；
5. review.md.j2 模板渲染（含 forced 横幅、blockers 标题随 verdict/forced 切换、轮次文案）；
6. artifact_persist 评审报告落盘 + 空评审容错；
7. prd_generation.md 复审反馈注入条件块（首轮为空不渲染）。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  PYTHONPATH=src python tests/test_prd_review.py
也可用 pytest 收集（无 pytest 时直接脚本运行，仅依赖标准库 unittest）。
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

# Windows GBK 控制台兜底：断言信息与脚本输出尽量 ASCII，中文仅存在于测试数据中
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from jinja2 import Template  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.artifact import make_artifact_persist  # noqa: E402
from nodes.review import (  # noqa: E402
    build_revision_feedback,
    judge_scores,
    make_prd_review,
    route_after_review,
)

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"

DIMENSIONS = [
    "结构完整",
    "需求可验证SMART",
    "逻辑一致",
    "真人感",
    "信息缺口显式标注",
]


# ────────────────────────── 测试替身 ──────────────────────────


class FakeLLM:
    """按通道与调用次序返回预制响应的假模型（零网络、零花费）。"""

    def __init__(self, json_queue=None, text_queue=None):
        self.json_queue = list(json_queue or [])
        self.text_queue = list(text_queue or [])
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        if as_text:
            return self.text_queue.pop(0)
        return self.json_queue.pop(0)


def make_finding(issue="success metric not testable", severity="阻断"):
    return {
        "severity": severity,
        "location": "## 成功标准",
        "issue": issue,
        "suggestion": "改成可测量的通过/失败标准",
    }


def make_hypothesis(idx=1):
    return {
        "hypothesis": f"承重墙假设{idx}",
        "fail_if": f"若{idx}不成立则方案失败",
        "evidence": f"本周可取证据{idx}",
        "kill_criterion": f"阈值{idx}未达即放弃",
        "cheapest_test": f"最便宜验证动作{idx}",
    }


def review_payload(scores, blockers=None, warnings=None):
    """构造符合 PrdReviewSchema 的模型评审输出（scores 为 5 个 1-5 数值）。"""
    return {
        "restatement": "一句话复述：该 PRD 要解决 X 问题",
        "scores": [
            {
                "dimension": DIMENSIONS[i],
                "score": score,
                "rationale": f"锚点理由：引用「## 章节{i}」原文",
            }
            for i, score in enumerate(scores)
        ],
        "blockers": list(blockers or []),
        "warnings": list(warnings or []),
        "hypotheses": [make_hypothesis(i) for i in range(1, 4)],
        "strengths": "范围边界一节取舍清楚",
        "unassessable": ["缺少历史转化数据"],
        "summary": "总体可成形，但成功标准不可测",
    }


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    """用真实 registry/ArtifactManager + FakeLLM NodeRunner 装最小依赖。"""
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(runner=runner, registry=registry, artifacts=artifacts, kb=None)


# ────────────────────────── 1. 硬判纯函数 ──────────────────────────


class TestJudgeScores(unittest.TestCase):
    def test_pass_all_high(self):
        # [5,5,4,5,4] -> 23/5 = 4.6，最低 4，全部 >=3 -> pass
        avg, minimum, verdict = judge_scores(review_payload([5, 5, 4, 5, 4])["scores"])
        self.assertEqual((avg, minimum, verdict), (4.6, 4, "pass"))

    def test_warn_one_dim_two(self):
        # [4,4,4,4,2] -> 18/5 = 3.6 达标，但一维为 2 -> pass_with_warning
        avg, minimum, verdict = judge_scores(review_payload([4, 4, 4, 4, 2])["scores"])
        self.assertEqual((avg, minimum, verdict), (3.6, 2, "pass_with_warning"))

    def test_reject_low_avg(self):
        # [3,3,4,3,3] -> 16/5 = 3.2 < 3.5 -> reject
        avg, minimum, verdict = judge_scores(review_payload([3, 3, 4, 3, 3])["scores"])
        self.assertEqual((avg, minimum, verdict), (3.2, 3, "reject"))

    def test_boundary_avg_exactly_3_5(self):
        # 边界：原始均分恰好 3.5（整数评分实际取不到 3.5，这里直接用浮点构造边界，
        # 生产路径由 schema 保证 int；纯函数比较用未舍入均分）-> pass 侧
        scores = [
            {"dimension": d, "score": s, "rationale": "x"}
            for d, s in zip(DIMENSIONS, [3.5, 3.5, 3.5, 3.5, 3.5])
        ]
        avg, minimum, verdict = judge_scores(scores)
        self.assertEqual(avg, 3.5)
        self.assertEqual(verdict, "pass")
        # 紧邻下侧 3.4 必须 reject
        avg2, _, verdict2 = judge_scores(review_payload([3, 3, 4, 3, 4])["scores"])
        self.assertEqual((avg2, verdict2), (3.4, "reject"))

    def test_empty_scores_raises(self):
        with self.assertRaises(ValueError):
            judge_scores([])


# ─────────────────── 1.5 阻断一票否决 [C 2026-09-12] ───────────────────


class TestJudgeScoresBlockerVeto(unittest.TestCase):
    """阻断级 blocker 一票否决：不看分数直接打回（建议1落地）。"""

    def test_veto_rejects_even_all_high_scores(self):
        # 五维全 5 分，但存在阻断级 blocker -> reject（均分最低分照常返回供展示）
        payload = review_payload([5, 5, 5, 5, 5], blockers=[make_finding("核心功能未定义验收标准")])
        avg, minimum, verdict = judge_scores(payload["scores"], payload["blockers"])
        self.assertEqual((avg, minimum, verdict), (5.0, 5, "reject"))

    def test_important_severity_does_not_veto(self):
        # 仅「重要」级 blocker：不否决，按分数走原逻辑
        payload = review_payload([5, 5, 5, 5, 5], blockers=[make_finding("重要问题", severity="重要")])
        _, _, verdict = judge_scores(payload["scores"], payload["blockers"])
        self.assertEqual(verdict, "pass")

    def test_suggestion_severity_does_not_veto(self):
        payload = review_payload([4, 4, 4, 4, 4], blockers=[make_finding("建议项", severity="建议")])
        _, _, verdict = judge_scores(payload["scores"], payload["blockers"])
        self.assertEqual(verdict, "pass")

    def test_empty_blockers_unchanged(self):
        # blockers 为空：行为与旧版逐字一致
        payload = review_payload([5, 4, 5, 4, 4])
        avg, minimum, verdict = judge_scores(payload["scores"], payload["blockers"])
        self.assertEqual((avg, minimum, verdict), (4.4, 4, "pass"))

    def test_mixed_blockers_one_blocking_vetoes(self):
        # 多条 blocker 混合严重度：只要有一条「阻断」即否决
        payload = review_payload(
            [5, 5, 5, 5, 5],
            blockers=[
                make_finding("重要问题", severity="重要"),
                make_finding("致命缺口"),
            ],
        )
        _, _, verdict = judge_scores(payload["scores"], payload["blockers"])
        self.assertEqual(verdict, "reject")

    def test_node_level_veto_rejects_and_builds_feedback(self):
        # 节点级：模型给高分但标了阻断项 -> verdict=reject、计数+1、反馈含阻断内容
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(
                json_queue=[
                    review_payload(
                        [5, 5, 4, 5, 4],
                        blockers=[make_finding("高分但致命-EEE")],
                    )
                ]
            )
            deps = make_deps(Path(tmp), fake)
            node = make_prd_review(deps)
            state = {
                "requirement_name": "demo",
                "prd_markdown": "# demo PRD\n\nbody",
                "prd_revision_count": 0,
            }
            out = node(state)
            review = out["red_team_review"]
            self.assertEqual(review["verdict"], "reject")
            self.assertEqual(out["prd_revision_count"], 1)
            self.assertFalse(review["forced"])
            self.assertIn("高分但致命-EEE", review["revision_feedback"])


# ────────────────────────── 2. 节点级行为 ──────────────────────────


class TestReviewNode(unittest.TestCase):
    def test_reject_increments_count_and_builds_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(
                json_queue=[
                    review_payload(
                        [3, 3, 4, 3, 3],
                        blockers=[make_finding("阻断问题-AAA")],
                        warnings=[make_finding("警告-BBB", severity="建议")],
                    )
                ]
            )
            deps = make_deps(Path(tmp), fake)
            node = make_prd_review(deps)

            state = {
                "requirement_name": "demo",
                "prd_markdown": "# demo PRD\n\nbody",
                "prd_revision_count": 0,
            }
            out = node(state)

            self.assertEqual(out["prd_revision_count"], 1)
            review = out["red_team_review"]
            self.assertEqual(review["verdict"], "reject")
            self.assertFalse(review["forced"])
            self.assertEqual(review["round"], 1)
            self.assertIn("阻断问题-AAA", review["revision_feedback"])
            self.assertIn("第 1 轮", review["revision_feedback"])
            # 分数短板维度也应进反馈（3 分维度被点名）
            self.assertIn("评分短板", review["revision_feedback"])
            # 模型原始内容保留
            self.assertEqual(len(review["scores"]), 5)
            self.assertEqual(len(review["hypotheses"]), 3)
            # JSON 通道被使用（不是文本通道）
            self.assertTrue(all(not c["as_text"] for c in fake.calls))

    def test_three_rejects_then_forced_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(
                json_queue=[
                    review_payload([3, 3, 3, 3, 4]),  # 3.2 reject
                    review_payload([3, 3, 4, 3, 3]),  # 3.2 reject
                    review_payload([2, 3, 3, 3, 3]),  # 2.8 reject -> 强制放行
                ]
            )
            deps = make_deps(Path(tmp), fake)
            node = make_prd_review(deps)
            state = {
                "requirement_name": "demo",
                "prd_markdown": "# PRD v1",
                "prd_revision_count": 0,
            }
            verdicts = []
            for _ in range(3):
                out = node(state)
                state.update(out)
                verdicts.append(out["red_team_review"]["verdict"])

            self.assertEqual(
                verdicts, ["reject", "reject", "pass_with_warning"]
            )
            self.assertEqual(state["prd_revision_count"], 3)
            final = state["red_team_review"]
            self.assertTrue(final["forced"])
            self.assertEqual(final["round"], 3)
            # 强制放行不再回 prd_generation，反馈文本置空
            self.assertEqual(final["revision_feedback"], "")

    def test_pass_keeps_count_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(json_queue=[review_payload([5, 4, 5, 4, 4])])
            deps = make_deps(Path(tmp), fake)
            node = make_prd_review(deps)
            state = {
                "requirement_name": "demo",
                "prd_markdown": "# good PRD",
                "prd_revision_count": 0,
            }
            out = node(state)
            self.assertEqual(out["prd_revision_count"], 0)
            review = out["red_team_review"]
            self.assertEqual(review["verdict"], "pass")
            self.assertFalse(review["forced"])
            self.assertEqual(review["round"], 0)
            self.assertEqual(review["revision_feedback"], "")

    def test_feedback_text_helper_content(self):
        review = review_payload(
            [4, 4, 2, 4, 4],
            blockers=[make_finding("具体阻断项-CCC")],
        )
        text = build_revision_feedback(review, 2)
        self.assertIn("第 2 轮", text)
        self.assertIn("具体阻断项-CCC", text)
        self.assertIn("## 成功标准", text)
        self.assertIn("改成可测量", text)


# ────────────────────────── 3. 路由 ──────────────────────────


class TestRouter(unittest.TestCase):
    def test_reject_back_to_generation(self):
        state = {"red_team_review": {"verdict": "reject"}}
        self.assertEqual(route_after_review(state), "prd_generation")

    def test_pass_and_warn_to_issue_splitting(self):
        # [C 2026-09-11] 块1：通过分支不再直连落盘，改走 issue_splitting 拆研发工单
        self.assertEqual(
            route_after_review({"red_team_review": {"verdict": "pass"}}),
            "issue_splitting",
        )
        self.assertEqual(
            route_after_review(
                {"red_team_review": {"verdict": "pass_with_warning", "forced": True}}
            ),
            "issue_splitting",
        )

    def test_empty_review_safe(self):
        # 缺评审 dict 时不应炸（默认送去拆单，由下游节点容错）
        self.assertEqual(route_after_review({}), "issue_splitting")


# ────────────────────────── 4. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_review_node_and_branch(self):
        # SQLite 检查点连接在图生命周期内保持打开，Windows 下会锁住 db 文件，
        # 故临时目录清理忽略残留文件。
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())  # 队列空：只编译不执行，不会调模型
            db_path = tmp_path / "test_graph.db"
            graph = build_graph(deps, db_path=str(db_path))

            node_names = set(graph.get_graph().nodes.keys())
            self.assertIn("prd_review", node_names)
            self.assertIn("prd_generation", node_names)
            self.assertIn("issue_splitting", node_names)  # [C 2026-09-11] 块1新节点
            self.assertIn("artifact_persist", node_names)

            # 静态断言条件边存在：prd_review 的 mermaid 连线指向打回/拆单两个目标
            drawn = graph.get_graph().draw_mermaid()
            self.assertIn("prd_review", drawn)
            self.assertIn("prd_generation", drawn)
            self.assertIn("issue_splitting", drawn)
            self.assertIn("artifact_persist", drawn)


# ────────────────────────── 5/6. 模板渲染与落盘 ──────────────────────────


def sample_review_dict(verdict="pass", forced=False, round_no=1):
    payload = review_payload(
        [4, 4, 4, 4, 3] if verdict == "pass" else [4, 4, 4, 4, 2],
        blockers=[] if verdict == "pass" else [make_finding("阻断-DDD")],
        warnings=[make_finding("建议-EEE", severity="建议")],
    )
    payload.update(
        {
            "avg": 3.8 if verdict == "pass" else 3.6,
            "minimum": 3 if verdict == "pass" else 2,
            "verdict": verdict,
            "forced": forced,
            "round": round_no,
            "revision_feedback": "",
        }
    )
    return payload


class TestTemplateAndPersist(unittest.TestCase):
    def test_template_render_contains_verdict_and_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(),
                },
            )
            self.assertIn("第 1 轮", md)
            self.assertIn("3.8", md)
            self.assertIn("五维评分", md)
            self.assertIn("红队假设", md)
            self.assertIn("demo-req", md)
            # 非强制时不应出现强制放行横幅文案
            self.assertNotIn("强制放行待人工裁决", md)

    def test_template_forced_banner(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(
                        verdict="pass_with_warning", forced=True, round_no=3
                    ),
                },
            )
            self.assertIn("已达 3 轮修订上限", md)
            self.assertIn("强制放行待人工裁决", md)
            self.assertIn("第 3 轮", md)

    def test_blockers_title_followups_when_pass_with_blockers(self):
        # [C 2026-09-10] 修复1：verdict=pass 但 blockers 非空（模型标了阻断级 finding）时，
        # 标题不得再写"必须修后复审"，应切换为"待跟进问题"，表格内容照常渲染。
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            review = sample_review_dict(verdict="pass", round_no=0)
            review["blockers"] = [make_finding("阻断级误标-GGG")]
            md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": review,
                },
            )
            self.assertIn("待跟进问题", md)
            self.assertNotIn("必须修后复审", md)
            # 表格列与 finding 内容不受标题切换影响
            self.assertIn("阻断级误标-GGG", md)

    def test_blockers_title_reject_and_forced(self):
        # [C 2026-09-10] 修复1：reject 保持"Blockers（必须修后复审）"；
        # forced=True（第 3 轮强制放行）切换为"强制放行遗留问题"。
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            reject_md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(verdict="reject"),
                },
            )
            self.assertIn("必须修后复审", reject_md)

            forced_md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(
                        verdict="pass_with_warning", forced=True, round_no=3
                    ),
                },
            )
            self.assertIn("强制放行遗留问题", forced_md)
            self.assertNotIn("必须修后复审", forced_md)

    def test_round_label_first_round_and_re_review(self):
        # [C 2026-09-10] 修复2：round=0 显示"首轮评审"且不出现"第 0 轮"；
        # round=2 显示"第 2 轮修订后复审"。
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            first_md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(verdict="pass", round_no=0),
                },
            )
            self.assertIn("首轮评审", first_md)
            self.assertNotIn("第 0 轮", first_md)

            second_md = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-10",
                    "review": sample_review_dict(verdict="pass", round_no=2),
                },
            )
            self.assertIn("第 2 轮修订后复审", second_md)

    def test_artifact_persist_saves_review(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            state = {
                "requirement_name": "demo-req",
                "prd_markdown": "# demo-req PRD\n\n正文",
                "user_insights": {},
                "red_team_review": sample_review_dict(),
            }
            out = make_artifact_persist(deps)(state)
            self.assertIn("review", out["artifacts"])
            review_file = Path(out["artifacts"]["review"])
            self.assertTrue(review_file.exists())
            content = review_file.read_text(encoding="utf-8")
            self.assertIn("第 1 轮", content)
            self.assertIn("评审报告", content)
            # prd/insights 原有产物不受影响
            self.assertIn("prd", out["artifacts"])
            self.assertIn("insights", out["artifacts"])

    def test_artifact_persist_without_review_tolerated(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            state = {
                "requirement_name": "demo-req",
                "prd_markdown": "# demo-req PRD\n\n正文",
                "user_insights": {},
                "red_team_review": {},
            }
            out = make_artifact_persist(deps)(state)
            self.assertNotIn("review", out["artifacts"])
            self.assertIn("prd", out["artifacts"])


# ────────────────────────── 7. 复审反馈注入条件块 ──────────────────────────


class TestGenerationPromptConditional(unittest.TestCase):
    def test_revision_block_injected_on_reject(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_generation")
            review = {
                "verdict": "reject",
                "round": 2,
                "revision_feedback": "【第 2 轮修订要求】1. [阻断] 成功标准不可测",
            }
            rendered = Template(raw).render(
                confirmed_requirement="需求 X",
                section_plan={},
                user_insights={},
                red_team_review=review,
            )
            self.assertIn("上一轮评审打回意见", rendered)
            self.assertIn("第 2 轮修订", rendered)
            self.assertIn("成功标准不可测", rendered)

    def test_revision_block_empty_on_first_round(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_generation")
            rendered = Template(raw).render(
                confirmed_requirement="需求 X",
                section_plan={},
                user_insights={},
                red_team_review={},
            )
            self.assertNotIn("上一轮评审打回意见", rendered)
            # 首轮原 prompt 关键引导仍在
            self.assertIn("资深 PM 的质量标杆", rendered)

    def test_review_prompt_renders_with_state(self):
        # prd_review.md 自身 Jinja2 语法可用复审态 state 渲染通过
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_review")
            rendered = Template(raw).render(
                requirement_name="demo-req",
                prd_markdown="# demo PRD",
                red_team_review={
                    "verdict": "reject",
                    "round": 1,
                    "revision_feedback": "逐条修复 AAA",
                },
            )
            self.assertIn("demo PRD", rendered)
            self.assertIn("逐条修复 AAA", rendered)
            self.assertIn("第 1 轮", rendered)

    # [C 2026-09-12 by codebuddy-ds41flash] prd_review AI 专项检查条件块渲染测试
    AI_CHECK_KEYWORDS = ("协作边界", "负向验收", "风险登记册", "kill 阈值")

    def _render_prd_review(self, **extra):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_review")
            return Template(raw).render(
                requirement_name="x", prd_markdown="y", **extra
            )

    def test_prompt_ai_core_true_contains_ai_checks(self):
        # ai_core=True：注入 AI 专项检查四项（协作边界/负向验收/风险登记册/kill 阈值）
        rendered = self._render_prd_review(ai_core=True)
        for keyword in self.AI_CHECK_KEYWORDS:
            self.assertIn(keyword, rendered)

    def test_prompt_ai_core_false_no_ai_checks(self):
        # ai_core=False：条件块不渲染，AI 检查项关键词均不出现，五维评分维度名仍在
        rendered = self._render_prd_review(ai_core=False)
        for keyword in self.AI_CHECK_KEYWORDS:
            self.assertNotIn(keyword, rendered)
        self.assertIn("结构完整", rendered)

    def test_prompt_ai_core_none_no_ai_checks(self):
        # ai_core=None（未判定/旧检查点）：视为普通需求，同 False
        rendered = self._render_prd_review(ai_core=None)
        for keyword in self.AI_CHECK_KEYWORDS:
            self.assertNotIn(keyword, rendered)
        self.assertIn("结构完整", rendered)

    def test_schema_registration_and_validation(self):
        # registry 能按约定加载 PrdReviewSchema，且非法分数被拒
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            schema_cls = registry.load_schema("prd_review")
            ok = schema_cls.model_validate(review_payload([4, 4, 4, 4, 4]))
            self.assertEqual(len(ok.scores), 5)
            bad = review_payload([4, 4, 4, 4, 6])  # 6 分越界
            with self.assertRaises(Exception):
                schema_cls.model_validate(bad)


# ────────────────────────── R10 证据分级 ──────────────────────────
# [C 2026-09-16 by MA] R10：关键结论标来源等级 [T1]-[T5]，低等级驱动的决策显式标记。


class TestR10EvidenceTier(unittest.TestCase):
    def test_schema_accepts_evidence_tier_and_keeps_default_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            schema_cls = registry.load_schema("prd_review")
            # 未带 evidence_tier（旧 payload）：默认 None，向后兼容
            obj = schema_cls.model_validate(review_payload([4, 4, 4, 4, 4]))
            self.assertTrue(all(s.evidence_tier is None for s in obj.scores))
            # 显式标注 [T3]：能校验通过
            tagged = review_payload([4, 4, 3, 4, 3])
            tagged["scores"][0]["evidence_tier"] = "T3"
            obj2 = schema_cls.model_validate(tagged)
            self.assertEqual(obj2.scores[0].evidence_tier, "T3")
            # 只接受 [T1]-[T5] 之一或 null；非法值被拒
            illegal = review_payload([4, 4, 4, 4, 4])
            illegal["scores"][0]["evidence_tier"] = "T9"
            with self.assertRaises(Exception):
                schema_cls.model_validate(illegal)

    def test_template_renders_tier_when_present_and_dash_when_absent(self):
        with tempfile.TemporaryDirectory() as tmp:
            deps = make_deps(Path(tmp))
            # 无 tier：渲染 — 占位，且注释说明出现
            md_plain = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-16",
                    "review": sample_review_dict(),
                },
            )
            self.assertIn("证据等级", md_plain)
            self.assertIn("[T1] 实测数据", md_plain)  # 注释图例
            table = md_plain.split("五维评分")[1].split("均分")[0]
            self.assertIn("—", table)  # 未见 tier 的行显示 —
            # 带 tier：具体等级落进表格
            review = sample_review_dict()
            review["scores"][0]["evidence_tier"] = "T4"
            md_tagged = deps.artifacts.render(
                "review.md.j2",
                {
                    "requirement_name": "demo-req",
                    "generated_at": "2026-09-16",
                    "review": review,
                },
            )
            table_tagged = md_tagged.split("五维评分")[1].split("均分")[0]
            self.assertIn("[T4]", table_tagged)
            self.assertNotIn("| — |", table_tagged.split("[T4]")[0])  # 首行不带 tier


if __name__ == "__main__":
    unittest.main(verbosity=2)
