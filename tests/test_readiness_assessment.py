# [C 2026-09-16] 就绪度打分节点（readiness_assessment，R11）自测
"""readiness_assessment 零 API 假测试：FakeLLM 提供 11 维度打分，不发任何真实模型调用。

覆盖：
1. score_readiness 纯函数：加权均分（权重表与 AI PM Playbook 一致，eval_readiness 1.5 最高）、
   全 5 = 5.00 / 全 0 = 0.00 / 全 3 = 3.00；
2. _readiness_level 六档边界（1.99 / 2.0 / 2.69 / 2.7 / 3.29 / 3.3 / 3.69 / 3.7 / 4.49 / 4.5）；
3. 三级阻断条件硬判：评测就绪 <3、风险与安全 <3、可观测性 <3、监管就绪 <3、发布与运营 <3
   各触发一条阻断；非阻断维度（如 problem_fit <3）只进 low_dimensions 不阻断；
4. low_dimensions 收集 <3 的维度；dimension_scores 汇总 11 维；
5. 畸形输入容错：None / 非 dict / 空 dict / 维度缺失 / score 非数字 / score 越界（钳到 0-5）；
6. 节点协议（FakeLLM）：读 prompt + schema，写 state[\"readiness_assessment\"]（含模型原始字段 + 代码算出的
   weighted_avg / level / level_label / blockers / low_dimensions / dimension_scores）；
7. 图接线：readiness_assessment 注册在位；launch_plan -> readiness_assessment -> launch_confirm
   两条普通边；确认门不再由 launch_plan 直连；
8. launch_confirm 中断载荷携带 readiness_assessment（draft 与 escalated 两处）；
9. hitl_cli.PAYLOAD_RECAP_FIELDS 含 readiness_assessment；
10. 报告模板 readiness_assessment.md.j2 渲染出均分、档位、阻断项与 11 维表格；
11. _build_question 对 launch_confirm 的提示提到就绪度打分。

运行（bash，cwd=项目根）：
  PYTHONPATH=src <venv python> -m pytest tests/test_readiness_assessment.py -v
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.readiness_assessment import (  # noqa: E402
    DIMENSIONS,
    DIMENSION_WEIGHTS,
    _readiness_level,
    make_readiness_assessment,
    score_readiness,
)

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"


# ────────────────────────── 测试替身与夹具 ──────────────────────────


class FakeLLM:
    def __init__(self, payload=None):
        self.payload = payload if payload is not None else {}
        self.calls = []

    def __call__(self, prompt, as_text=False):
        self.calls.append({"as_text": as_text, "prompt": prompt})
        return self.payload


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(runner=runner, registry=registry, artifacts=artifacts, kb=None)


def _dim(score: int) -> dict:
    return {
        "score": score,
        "evidence": f"[T3] 示例证据 {score}",
        "risk": "无",
        "owner": "张三",
        "next_action": "维持",
    }


def _assessment(**overrides) -> dict:
    """造一份合法 11 维打分（默认全 4 分）；overrides 覆盖指定维度。"""
    base = {dim: _dim(4) for dim in DIMENSIONS}
    base.update(overrides)
    return base


# ────────────────────────── 1-2. 纯函数：加权均分与档位 ──────────────────────────


class TestScoreReadiness(unittest.TestCase):
    def test_weights_match_playbook(self):
        # 权重表契约：11 维、eval_readiness 最高 1.5，其次 risk_and_safety 1.4
        self.assertEqual(len(DIMENSION_WEIGHTS), 11)
        self.assertEqual(DIMENSION_WEIGHTS["eval_readiness"], 1.5)
        self.assertEqual(DIMENSION_WEIGHTS["risk_and_safety"], 1.4)
        self.assertEqual(DIMENSION_WEIGHTS["regulatory_readiness"], 1.3)
        self.assertEqual(DIMENSION_WEIGHTS["observability"], 1.3)
        # 最大权重就是 eval_readiness
        self.assertEqual(
            max(DIMENSION_WEIGHTS, key=DIMENSION_WEIGHTS.get), "eval_readiness"
        )

    def test_all_five_is_five(self):
        out = score_readiness(_assessment(**{d: _dim(5) for d in DIMENSIONS}))
        self.assertEqual(out["weighted_avg"], 5.0)
        self.assertEqual(out["level"], "scale_ready")

    def test_all_zero_is_zero(self):
        out = score_readiness(_assessment(**{d: _dim(0) for d in DIMENSIONS}))
        self.assertEqual(out["weighted_avg"], 0.0)
        self.assertEqual(out["level"], "not_ready")

    def test_all_three_is_three(self):
        out = score_readiness(_assessment(**{d: _dim(3) for d in DIMENSIONS}))
        self.assertEqual(out["weighted_avg"], 3.0)
        self.assertEqual(out["level"], "pilot_candidate")

    def test_eval_readiness_dominates(self):
        # 其他维全 4，唯 eval_readiness 拉到 5：均分应上抬；反向拉到 0：均分应下压
        high = score_readiness(_assessment(eval_readiness=_dim(5)))
        base = score_readiness(_assessment())
        low = score_readiness(_assessment(eval_readiness=_dim(0)))
        self.assertGreater(high["weighted_avg"], base["weighted_avg"])
        self.assertLess(low["weighted_avg"], base["weighted_avg"])

    def test_level_boundaries(self):
        # (均分下界, 期望档位)：直接测六档分界，用 0.01 步长确认边界归属
        cases = [
            (1.99, "not_ready"),
            (2.0, "prototype_only"),
            (2.69, "prototype_only"),
            (2.7, "pilot_candidate"),
            (3.29, "pilot_candidate"),
            (3.3, "pilot_ready"),
            (3.69, "pilot_ready"),
            (3.7, "limited_production"),
            (4.49, "limited_production"),
            (4.5, "scale_ready"),
            (5.0, "scale_ready"),
        ]
        for avg, expected in cases:
            with self.subTest(avg=avg):
                self.assertEqual(_readiness_level(avg)[0], expected)

    def test_dimension_scores_collected(self):
        out = score_readiness(_assessment())
        self.assertEqual(len(out["dimension_scores"]), 11)
        self.assertEqual(set(out["dimension_scores"]), set(DIMENSIONS))


# ────────────────────────── 3-4. 三级阻断与低分维度 ──────────────────────────


class TestBlockers(unittest.TestCase):
    def test_clean_high_scores_have_no_blockers(self):
        out = score_readiness(_assessment())
        self.assertEqual(out["blockers"], [])
        self.assertEqual(out["low_dimensions"], [])

    def test_each_blocking_dimension_below_three_blocks(self):
        # 维度 key -> 阻断文案里的中文标签
        labels = {
            "eval_readiness": "评测就绪度",
            "risk_and_safety": "风险与安全",
            "observability": "可观测性",
            "regulatory_readiness": "监管就绪",
            "launch_and_operations": "发布与运营",
        }
        for dim, label in labels.items():
            with self.subTest(dim=dim):
                out = score_readiness(_assessment(**{dim: _dim(2)}))
                self.assertTrue(out["blockers"], msg=dim)
                self.assertTrue(
                    any(label in b for b in out["blockers"]),
                    msg=f"{dim}（{label}）未出现在阻断项：{out['blockers']}",
                )

    def test_non_blocking_dimension_below_three_only_lists_low(self):
        # problem_fit <3 属低分但不阻断（Playbook 的阻断清单不含它）
        out = score_readiness(_assessment(problem_fit=_dim(2)))
        self.assertEqual(out["blockers"], [])
        self.assertEqual(
            [x["dimension"] for x in out["low_dimensions"]], ["problem_fit"]
        )

    def test_all_low_scores_block_all_five(self):
        out = score_readiness(_assessment(**{d: _dim(1) for d in DIMENSIONS}))
        self.assertEqual(len(out["blockers"]), 5)
        self.assertEqual(len(out["low_dimensions"]), 11)


# ────────────────────────── 5. 畸形输入容错 ──────────────────────────


class TestMalformed(unittest.TestCase):
    def test_none_and_non_dict(self):
        for bad in (None, "x", 42, []):
            with self.subTest(bad=bad):
                out = score_readiness(bad)
                self.assertEqual(out["weighted_avg"], 0.0)
                self.assertEqual(out["level"], "not_ready")
                self.assertEqual(len(out["dimension_scores"]), 11)

    def test_missing_dimensions_count_as_zero(self):
        out = score_readiness({"problem_fit": _dim(5)})
        self.assertEqual(out["dimension_scores"]["problem_fit"], 5)
        self.assertEqual(out["dimension_scores"]["eval_readiness"], 0)

    def test_non_numeric_score_is_zero(self):
        out = score_readiness({"eval_readiness": {"score": "abc"}})
        self.assertEqual(out["dimension_scores"]["eval_readiness"], 0)

    def test_out_of_range_score_is_clamped(self):
        self.assertEqual(
            score_readiness({"eval_readiness": {"score": 99}})["dimension_scores"][
                "eval_readiness"
            ],
            5,
        )
        self.assertEqual(
            score_readiness({"eval_readiness": {"score": -3}})["dimension_scores"][
                "eval_readiness"
            ],
            0,
        )

    def test_non_dict_dimension_is_zero(self):
        out = score_readiness({"eval_readiness": "nope"})
        self.assertEqual(out["dimension_scores"]["eval_readiness"], 0)


# ────────────────────────── 6. 节点协议 ──────────────────────────


class TestNode(unittest.TestCase):
    def test_node_writes_state_with_computed_fields(self):
        payload = _assessment(eval_readiness=_dim(2))
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp), FakeLLM(payload))
            node = make_readiness_assessment(deps)
            out = node({"requirement_name": "测试需求", "prd_markdown": "..."})

        record = out["readiness_assessment"]
        # 模型原始 11 维字段保留
        for dim in DIMENSIONS:
            self.assertIn(dim, record)
        # 代码算出的字段
        self.assertIn("weighted_avg", record)
        self.assertIn("level", record)
        self.assertIn("level_label", record)
        self.assertIn("blockers", record)
        self.assertIn("low_dimensions", record)
        self.assertIn("dimension_scores", record)
        # eval_readiness=2 -> 有阻断项
        self.assertTrue(record["blockers"])

    def test_node_prompt_carries_upstream_materials(self):
        fake = FakeLLM(_assessment())
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp), fake)
            node = make_readiness_assessment(deps)
            node(
                {
                    "requirement_name": "小说创作助手",
                    "prd_markdown": "PRD 正文占位",
                    "eval_report": {"overall_rate": 0.667, "critical_rate": 0.857},
                    "launch_plan": {"tier": "2", "positioning": "给作者用的助手"},
                }
            )
        self.assertTrue(fake.calls)
        prompt = fake.calls[0]["prompt"]
        self.assertIn("小说创作助手", prompt)
        self.assertIn("PRD 正文占位", prompt)


# ────────────────────────── 7. 图接线 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_readiness_registered_and_wired(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path)
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            self.assertIn("readiness_assessment", names)

            drawn = graph.get_graph().draw_mermaid()
            # launch_plan -> readiness_assessment -> launch_confirm 两条普通边
            self.assertTrue(
                any(
                    "launch_plan" in ln
                    and "readiness_assessment" in ln
                    and "-->" in ln
                    for ln in drawn.splitlines()
                ),
                msg=f"缺 launch_plan -> readiness_assessment 边\n{drawn}",
            )
            self.assertTrue(
                any(
                    "readiness_assessment" in ln
                    and "launch_confirm" in ln
                    and "-->" in ln
                    for ln in drawn.splitlines()
                ),
                msg=f"缺 readiness_assessment -> launch_confirm 边\n{drawn}",
            )
            # launch_plan 不再直连 launch_confirm
            self.assertFalse(
                any(
                    ln.startswith("launch_plan")
                    and "launch_confirm" in ln
                    and "-->" in ln
                    for ln in drawn.splitlines()
                ),
                msg="launch_plan 仍直连 launch_confirm（应经 readiness_assessment）",
            )


# ────────────────────────── 8-11. 载荷、展示与渲染 ──────────────────────────


class TestPresentation(unittest.TestCase):
    def test_payload_recap_includes_readiness(self):
        from cli.hitl_cli import PAYLOAD_RECAP_FIELDS

        self.assertIn("readiness_assessment", PAYLOAD_RECAP_FIELDS)

    def test_launch_confirm_interrupt_carries_readiness(self):
        # launch_confirm 首次中断载荷必须携带 readiness_assessment，供确认门展示
        from unittest.mock import patch

        from nodes.launch_plan import make_launch_confirm

        payloads: list[dict] = []

        def fake_interrupt(value):
            payloads.append(value)
            return "确认"

        state = {
            "requirement_name": "测试需求",
            "launch_plan": {"tier": "2", "positioning": "定位"},
            "readiness_assessment": {"weighted_avg": 3.5, "level": "pilot_ready"},
            "human_feedback": [],
        }
        node = make_launch_confirm(None)
        with patch("nodes.launch_plan.interrupt", side_effect=fake_interrupt):
            node(state)
        self.assertTrue(payloads)
        self.assertIn("readiness_assessment", payloads[0])
        self.assertEqual(payloads[0]["readiness_assessment"]["weighted_avg"], 3.5)

    def test_template_renders(self):
        assessment = {
            **_assessment(eval_readiness=_dim(2)),
            **score_readiness(_assessment(eval_readiness=_dim(2))),
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            md = deps.artifacts.render(
                "readiness_assessment.md.j2",
                {
                    "requirement_name": "小说创作助手",
                    "generated_at": "2026-09-16",
                    "assessment": assessment,
                },
            )
        self.assertIn("发布前就绪度打分", md)
        self.assertIn("小说创作助手", md)
        self.assertIn("评测就绪度", md)
        self.assertIn("阻断项", md)

    def test_question_text_mentions_readiness(self):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_readiness",
            REPO_ROOT / "scripts" / "run_prd_workflow.py",
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        q = module._build_question(
            "launch_confirm",
            {
                "node": "launch_confirm",
                "status": "draft",
                "requirement_name": "测试",
                "launch_plan": {"tier": "2"},
                "readiness_assessment": {"weighted_avg": 3.0},
            },
            {},
        )
        self.assertIn("就绪度", q)


    def test_artifact_persist_writes_readiness_report(self):
        # artifact_persist 在 launch_plan 之后落就绪度报告，并入产物清单
        from nodes.artifact import make_artifact_persist

        assessment = {
            **_assessment(eval_readiness=_dim(2)),
            **score_readiness(_assessment(eval_readiness=_dim(2))),
        }
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            deps = make_deps(Path(tmp))
            node = make_artifact_persist(deps)
            out = node(
                {
                    "requirement_name": "测试需求",
                    "prd_markdown": "# PRD 正文",
                    "readiness_assessment": assessment,
                }
            )
            artifacts = out["artifacts"]
            self.assertIn("readiness_assessment", artifacts)
            self.assertTrue(Path(artifacts["readiness_assessment"]).exists())


if __name__ == "__main__":
    unittest.main()
