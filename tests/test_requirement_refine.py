# [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点自测
"""requirement_refine 节点（含确认门 pending 标志）零 API 测试：
patch 掉 nodes.refine.interrupt / nodes.hitl.interrupt，假 LLM 回放预制响应，
不发起任何真实模型调用。

覆盖（R1-R17 原用例 + R18/S047 追加）：
1. classify_refine_answer 纯函数四分类：确认词/改判关键词/放弃关键词/feedback；
2. route_after_requirement_confirm：pending=True -> requirement_refine，False -> needs_discovery；
3. route_after_requirement_refine：abandon->END，feedback->requirement_refine，confirm/reclassify->needs_discovery；
4. 确认门 feedback -> confirmed_requirement=raw_requirement（不替换），pending=True；
5. 确认门 confirm -> confirmed_requirement=raw_requirement，pending=False，eval_cases 已初始化；
6. 确认门 纯改判 -> pending=False，confirmed=raw_requirement；
7. 确认门 改判带附言 -> pending=True，ai_core 已改，feedback=用户文本；
8. 整合节点首次调模型产草案，中断载荷含草案+变更说明+count；
9. 整合节点确认 -> confirmed_requirement=草案，eval_cases 重初始化，pending=False；
10. 整合节点 feedback -> verdict=feedback，count+1，草案存 state，自环；
11. [S047 改写] 连续 5 轮 feedback 每轮都调模型整合，无升级暂停（原"第 3 版升级暂停"用例改写）；
12. [S047 改写] 第 3 轮确认 -> 仍调模型、接受新版草案（原"升级暂停确认"用例改写）；
13. [S047 改写] 第 3 轮改判 -> 仍调模型、接受新版草案 + 改 ai_core（含 AI核心 关键词）；
14. [S047 改写] 第 3 轮放弃 -> END（原"升级暂停放弃"用例改写）；
15. 普通轨路径不变：confirm -> needs_discovery -> prd_generation（逐字不变）；
16. 图编译通过，节点数 18；
17. 可行性门 reshape 回确认门后提 feedback -> 进整合节点；
18. [S047] 重整合输入组装：已有上一版草案时「当前需求」用上一版草案（含反馈自环端到端），
    草案为空时（首轮）回落到 confirmed_requirement 原文；
19. [S047] audit_draft_progress 纯函数：几乎相同提示 / 缩水提示 / 意见重复提示 / 首轮全空；
20. [S047] 载荷接线：中断载荷含 draft_progress，PAYLOAD_RECAP_FIELDS 含该字段名。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe -m pytest tests/test_requirement_refine.py -v
也可用脚本直接运行（无 pytest 时依赖标准库 unittest）。
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# Windows GBK 控制台兜底
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
sys.path.insert(0, str(SRC_DIR))

import unittest  # noqa: E402

from langgraph.graph import END  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.exploration import (  # noqa: E402
    make_needs_discovery,
    route_after_needs_discovery,
)
from nodes.hitl import make_requirement_confirm  # noqa: E402
from nodes.prd import make_prd_generation  # noqa: E402
from nodes.refine import (  # noqa: E402
    audit_draft_progress,
    classify_refine_answer,
    make_requirement_refine,
    route_after_requirement_confirm,
    route_after_requirement_refine,
)

COMPONENTS_DIR = SRC_DIR / "components"
TEMPLATE_DIR = REPO_ROOT / "artifacts" / "templates"
ASSETS_DIR = REPO_ROOT / "artifacts" / "assets"


# ────────────────────────── 测试替身与夹具 ──────────────────────────


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


class StubKB:
    """最小知识库替身：retrieve_relevant 返回空检索结果。"""

    def retrieve_relevant(self, query):  # noqa: ARG002
        return {}


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(
        runner=runner, registry=registry, artifacts=artifacts, kb=StubKB()
    )


def confirm_state(**overrides):
    """需求确认门入口 state。"""
    state = {
        "requirement_name": "demo-req",
        "raw_requirement": "帮产品经理做会议纪要总结",
        "info_completeness": {"completeness": "medium"},
        "human_feedback": [],
    }
    state.update(overrides)
    return state


def refine_state(**overrides):
    """整合节点入口 state（pending=True 由确认门写入）。"""
    state = {
        "requirement_name": "demo-req",
        "raw_requirement": "帮产品经理做会议纪要总结",
        "confirmed_requirement": "帮产品经理做会议纪要总结",
        "requirement_refine_pending": True,
        "requirement_refine_feedback": "增加用户画像维度",
        "requirement_refine_count": 0,
        "requirement_draft": "",
        "requirement_refine_result": {},
        "human_feedback": [],
        "ai_core": True,
        "ai_triage": {"suggestion": "ai_core"},
    }
    state.update(overrides)
    return state


def run_confirm_node(state, answers, fake_llm):
    """patch 掉 nodes.hitl.interrupt，按 answers 次序回放 resume 值。

    返回 (节点输出 dict, 历次 interrupt 载荷 list)。
    """
    payloads: list[dict] = []
    queue = list(answers)

    def fake_interrupt(value):
        payloads.append(value)
        return queue.pop(0)

    node = make_requirement_confirm(make_deps(Path(tempfile.mkdtemp()), fake_llm))
    with patch("nodes.hitl.interrupt", side_effect=fake_interrupt):
        out = node(state)
    return out, payloads


def run_refine_node(state, answers, fake_llm=None):
    """patch 掉 nodes.refine.interrupt，按 answers 次序回放 resume 值。

    返回 (节点输出 dict, 历次 interrupt 载荷 list, FakeLLM 实例)。
    fake_llm 传 None 时构造默认 FakeLLM（json_queue 由调用方提前布好）。
    """
    payloads: list[dict] = []
    queue = list(answers)

    def fake_interrupt(value):
        payloads.append(value)
        return queue.pop(0)

    if fake_llm is None:
        fake_llm = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "帮产品经理做会议纪要总结，并按用户画像维度归类待办",
                    "change_summary": ["增加用户画像维度作为待办归类维度"],
                }
            ]
        )
    deps = make_deps(Path(tempfile.mkdtemp()), fake_llm)
    node = make_requirement_refine(deps)
    with patch("nodes.refine.interrupt", side_effect=fake_interrupt):
        out = node(state)
    return out, payloads, fake_llm


# ────────────────────────── R1. classify_refine_answer 纯函数 ──────────────────────────


class TestClassifyRefineAnswer(unittest.TestCase):
    def test_confirm_exact_words(self):
        for word in ("", "   ", None, "confirmed", "confirm", "ok", "确认", "通过", "同意"):
            self.assertEqual(classify_refine_answer(word), "confirm", msg=repr(word))

    def test_reclassify_non_ai_keywords(self):
        for word in ("非AI", "非ai", "非AI轨", "改判普通轨", "普通轨", "非核心需求"):
            self.assertEqual(classify_refine_answer(word), "reclassify", msg=word)

    def test_reclassify_ai_core_keywords(self):
        for word in ("AI核心", "ai核心", "AI轨", "AI全轨", "改判AI", "AI核心轨"):
            self.assertEqual(classify_refine_answer(word), "reclassify", msg=word)

    def test_ai_core_with_spaces_normalizes(self):
        self.assertEqual(classify_refine_answer("AI 核心"), "reclassify")
        self.assertEqual(classify_refine_answer("ai\u3000核心"), "reclassify")

    def test_negated_ai_core_is_feedback(self):
        for word in ("不是AI核心", "不要AI核心", "不用AI轨", "别改判AI"):
            self.assertEqual(classify_refine_answer(word), "feedback", msg=word)

    def test_abandon_keywords(self):
        for word in ("放弃", "不做", "终止", "搁置", "停做"):
            self.assertEqual(classify_refine_answer(word), "abandon", msg=word)

    def test_abandon_priority_over_reclassify(self):
        # "放弃改判普通" 含 abandon + reclassify 关键词，abandon 优先
        self.assertEqual(classify_refine_answer("放弃改判普通"), "abandon")

    def test_free_text_defaults_feedback(self):
        for word in ("增加用户画像维度", "I1 探针再加一条", "把需求范围收窄"):
            self.assertEqual(classify_refine_answer(word), "feedback", msg=word)

    def test_priority_over_confirm_word(self):
        # 关键词包含判定优先于确认精确匹配
        self.assertEqual(classify_refine_answer("非AI，确认"), "reclassify")
        self.assertEqual(classify_refine_answer("AI核心，确认"), "reclassify")
        self.assertEqual(classify_refine_answer("放弃，确认"), "abandon")


# ────────────────────────── R2/R3. 路由纯函数 ──────────────────────────


class TestRoutes(unittest.TestCase):
    def test_route_after_requirement_confirm_pending_true(self):
        # R2：pending=True -> requirement_refine
        self.assertEqual(
            route_after_requirement_confirm(
                {"requirement_refine_pending": True}
            ),
            "requirement_refine",
        )

    def test_route_after_requirement_confirm_pending_false(self):
        # R2：pending=False -> needs_discovery（原路径）
        self.assertEqual(
            route_after_requirement_confirm(
                {"requirement_refine_pending": False}
            ),
            "needs_discovery",
        )

    def test_route_after_requirement_confirm_missing_pending(self):
        # 缺字段按 False 处理 -> needs_discovery（保守放行，避免卡死）
        self.assertEqual(route_after_requirement_confirm({}), "needs_discovery")

    def test_route_after_requirement_refine_abandon(self):
        # R3：abandon -> END
        self.assertEqual(
            route_after_requirement_refine(
                {"requirement_refine_result": {"verdict": "abandon"}}
            ),
            END,
        )

    def test_route_after_requirement_refine_feedback(self):
        # R3：feedback -> requirement_refine 自环
        self.assertEqual(
            route_after_requirement_refine(
                {"requirement_refine_result": {"verdict": "feedback"}}
            ),
            "requirement_refine",
        )

    def test_route_after_requirement_refine_confirm(self):
        # R3：confirm -> needs_discovery
        self.assertEqual(
            route_after_requirement_refine(
                {"requirement_refine_result": {"verdict": "confirm"}}
            ),
            "needs_discovery",
        )

    def test_route_after_requirement_refine_reclassify(self):
        # R3：reclassify -> needs_discovery（与 confirm 同走原路径，ai_core 已改）
        self.assertEqual(
            route_after_requirement_refine(
                {"requirement_refine_result": {"verdict": "reclassify"}}
            ),
            "needs_discovery",
        )

    def test_route_after_requirement_refine_fallback(self):
        # R3：缺 verdict / 空 dict -> 保守放行进 needs_discovery
        self.assertEqual(route_after_requirement_refine({}), "needs_discovery")
        self.assertEqual(
            route_after_requirement_refine({"requirement_refine_result": {}}),
            "needs_discovery",
        )


# ────────────────────────── R4-R7. 确认门节点级行为 ──────────────────────────


class TestRequirementConfirmNodePending(unittest.TestCase):
    def test_feedback_sets_pending_true_keeps_raw(self):
        # R4：feedback -> confirmed=raw_requirement（不替换），pending=True
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        out, _ = run_confirm_node(
            confirm_state(), ["增加用户画像维度"], fake
        )
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )
        self.assertTrue(out["requirement_refine_pending"])
        self.assertEqual(out["requirement_refine_feedback"], "增加用户画像维度")
        # pending=True 时跳过 eval_cases 初始化
        self.assertNotIn("eval_cases", out)

    def test_confirm_sets_pending_false_eval_cases_initialized(self):
        # R5：confirm -> confirmed=raw_requirement，pending=False，eval_cases 已初始化
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        out, _ = run_confirm_node(confirm_state(), [""], fake)
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )
        self.assertFalse(out["requirement_refine_pending"])
        # eval_cases 已初始化（4 条模板题）
        self.assertIn("eval_cases", out)
        self.assertEqual(len(out["eval_cases"]), 4)
        self.assertEqual(out["eval_cases"][0]["id"], "eval-1")

    def test_pure_reclassify_sets_pending_false(self):
        # R6：纯改判 -> pending=False，confirmed=raw_requirement
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "non_ai", "reason": "普通", "signals": ["y"]}
            ]
        )
        out, _ = run_confirm_node(confirm_state(), ["AI核心"], fake)
        self.assertTrue(out["ai_core"])
        self.assertFalse(out["requirement_refine_pending"])
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )
        # 纯改判时 eval_cases 已初始化
        self.assertIn("eval_cases", out)
        self.assertEqual(len(out["eval_cases"]), 4)

    def test_reclassify_with_amendment_sets_pending_true(self):
        # R7：改判带附言 -> pending=True，ai_core 已改，feedback=用户文本
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        out, _ = run_confirm_node(
            confirm_state(), ["非AI，固定文案就好"], fake
        )
        self.assertFalse(out["ai_core"])  # 改判 non_ai 已生效
        self.assertTrue(out["requirement_refine_pending"])
        # confirmed 保留原 raw_requirement
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )
        # feedback=用户文本（含改判词）
        self.assertEqual(out["requirement_refine_feedback"], "非AI，固定文案就好")
        # pending=True 时跳过 eval_cases 初始化
        self.assertNotIn("eval_cases", out)

    def test_confirm_payload_carries_ai_triage(self):
        # 中断载荷仍携带 ai_triage（与既有确认门行为一致）
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        _, payloads = run_confirm_node(confirm_state(), [""], fake)
        self.assertEqual(payloads[0]["node"], "requirement_confirm")
        self.assertIn("ai_triage", payloads[0])
        self.assertEqual(payloads[0]["ai_triage"]["suggestion"], "ai_core")


# ────────────────────────── R8-R14. 整合节点级行为 ──────────────────────────


class TestRequirementRefineNode(unittest.TestCase):
    def test_first_call_produces_draft_payload(self):
        # R8：首次调模型产草案，中断载荷含草案+变更说明+count
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求文本-AAA",
                    "change_summary": ["新增维度1", "调整范围2"],
                }
            ]
        )
        out, payloads, fake_used = run_refine_node(refine_state(), [""], fake)
        # 调一次模型，JSON 通道
        self.assertEqual(len(fake_used.calls), 1)
        self.assertFalse(fake_used.calls[0]["as_text"])
        # 中断载荷含草案、变更说明、计数（new_count=1）
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["node"], "requirement_refine")
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(payloads[0]["requirement_draft"], "整合后的新需求文本-AAA")
        self.assertEqual(payloads[0]["change_summary"], ["新增维度1", "调整范围2"])
        self.assertEqual(payloads[0]["requirement_refine_count"], 1)

    def test_prompt_current_requirement_prefers_previous_draft(self):
        # R18（S047 主回归）：已有上一版草案时，「当前需求」段用上一版草案，不用原始需求
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-KKK",
                    "change_summary": ["调整"],
                }
            ]
        )
        state = refine_state(
            requirement_draft="上一版草案-含使用场景与痛点整段",
            confirmed_requirement="原始需求-帮产品经理做会议纪要总结",
            requirement_refine_count=1,
        )
        out, _, fake_used = run_refine_node(state, [""], fake)
        prompt = fake_used.calls[0]["prompt"]
        # 「当前需求」段含上一版草案全文本
        self.assertIn("- 当前需求：上一版草案-含使用场景与痛点整段", prompt)
        # 且不含原始需求原文（避免重整合把上一版已整合内容丢掉）
        self.assertNotIn("原始需求-帮产品经理做会议纪要总结", prompt)
        # 收尾不变：确认后 confirmed_requirement 等于最终版草案
        self.assertEqual(out["confirmed_requirement"], "整合后的新需求-KKK")

    def test_second_round_prompt_carries_first_round_draft(self):
        # R18b（S047 端到端）：feedback 自环后第二轮整合的输入含第一版草案
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第一版草案-含使用场景痛点现有方案",
                    "change_summary": ["首版整合"],
                },
                {
                    "refined_requirement": "第二版草案",
                    "change_summary": ["目标用户收窄"],
                },
            ]
        )
        base = refine_state()
        # 第一轮：用户提修订意见，节点产第一版草案并自环
        first_out, _, _ = run_refine_node(base, ["增加用户画像维度"], fake)
        self.assertEqual(first_out["requirement_refine_result"]["verdict"], "feedback")
        self.assertEqual(first_out["requirement_draft"], "第一版草案-含使用场景痛点现有方案")
        # 第二轮：带第一轮输出继续整合
        merged = {**base, **first_out}
        merged["requirement_refine_feedback"] = "再把目标用户收窄"
        merged["requirement_refine_count"] = first_out["requirement_refine_count"]
        _, _, fake_used = run_refine_node(merged, [""], fake)
        second_prompt = fake_used.calls[1]["prompt"]
        # 第二轮 prompt 的「当前需求」段是第一版草案，而非最初的需求原文
        self.assertIn("- 当前需求：第一版草案-含使用场景痛点现有方案", second_prompt)

    def test_prompt_current_requirement_falls_back_to_confirmed_on_first_round(self):
        # R19（首轮不变）：requirement_draft 为空时，「当前需求」仍是 confirmed_requirement 原文
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-LLL",
                    "change_summary": ["调整"],
                }
            ]
        )
        state = refine_state(
            requirement_draft="",
            confirmed_requirement="原始需求-帮产品经理做会议纪要总结",
        )
        _, _, fake_used = run_refine_node(state, [""], fake)
        prompt = fake_used.calls[0]["prompt"]
        self.assertIn("- 当前需求：原始需求-帮产品经理做会议纪要总结", prompt)

    def test_confirm_writes_back_confirmed_and_eval_cases(self):
        # R9：整合节点确认 -> confirmed_requirement=草案，eval_cases 重初始化，pending=False
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-BBB",
                    "change_summary": ["增加一项"],
                }
            ]
        )
        out, payloads, _ = run_refine_node(refine_state(), ["确认"], fake)
        self.assertEqual(out["requirement_refine_result"]["verdict"], "confirm")
        self.assertEqual(out["confirmed_requirement"], "整合后的新需求-BBB")
        # 草案写回 state
        self.assertEqual(out["requirement_draft"], "整合后的新需求-BBB")
        # eval_cases 重初始化（4 条）
        self.assertIn("eval_cases", out)
        self.assertEqual(len(out["eval_cases"]), 4)
        # eval_cases 用整合后的需求生成（含新需求文本）
        self.assertIn("整合后的新需求-BBB", out["eval_cases"][0]["question"])
        # pending=False，feedback 清零，计数=1
        self.assertFalse(out["requirement_refine_pending"])
        self.assertEqual(out["requirement_refine_count"], 1)
        self.assertEqual(out["requirement_refine_feedback"], "")

    def test_feedback_under_max_self_loops(self):
        # R10：feedback（count<MAX）-> verdict=feedback，count+1，草案存 state，自环
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-CCC",
                    "change_summary": ["增加一项"],
                }
            ]
        )
        out, _, _ = run_refine_node(refine_state(), ["再加一条：用户行为追踪"], fake)
        self.assertEqual(out["requirement_refine_result"]["verdict"], "feedback")
        # count+1（0 -> 1）
        self.assertEqual(out["requirement_refine_count"], 1)
        # 草案存 state
        self.assertEqual(out["requirement_draft"], "整合后的新需求-CCC")
        # 新意见存入 feedback（供下一轮整合注入 prompt）
        self.assertEqual(out["requirement_refine_feedback"], "再加一条：用户行为追踪")
        # pending 未显式写——route_after_requirement_refine 据 verdict=feedback 自环
        self.assertNotIn("requirement_refine_pending", out)
        # confirmed_requirement 未写（保留 state 中已有的值，整合未完成不替换）
        self.assertNotIn("confirmed_requirement", out)
        # eval_cases 未初始化（未确认）
        self.assertNotIn("eval_cases", out)
        # route_after_requirement_refine -> requirement_refine 自环
        self.assertEqual(route_after_requirement_refine(out), "requirement_refine")

    def test_five_rounds_feedback_each_round_calls_model(self):
        # R11（S047 改写）：取消次数上限后，连续 5 轮 feedback 每轮都调模型整合，
        # 不再出现 status=escalated（原"第 3 版升级暂停"用例改写）
        drafts = [
            "第一版草案：帮产品经理整理会议纪要，覆盖使用场景与痛点。",
            "第二版草案：帮产品经理整理会议纪要，覆盖使用场景、痛点与现有方案，目标用户收窄到中大型团队。",
            "第三版草案：帮产品经理整理会议纪要并自动抽待办，覆盖使用场景、痛点、现有方案与成功标准。",
            "第四版草案：帮产品经理整理会议纪要并自动抽待办，按用户画像归类，含成功标准与验收口径。",
            "第五版草案：帮产品经理整理会议纪要并自动抽待办，按用户画像归类，含成功标准、验收口径与风险清单。",
        ]
        fake = FakeLLM(
            json_queue=[
                {"refined_requirement": text, "change_summary": [f"第{i}轮调整"]}
                for i, text in enumerate(drafts, start=1)
            ]
        )
        state = refine_state()
        for i in range(1, 6):
            out, payloads, _ = run_refine_node(
                state, [f"第{i}轮意见：再收窄一点"], fake
            )
            # 每轮都调了一次模型（5 轮共 5 次），且走 JSON 通道
            self.assertEqual(len(fake.calls), i, msg=f"第{i}轮")
            self.assertFalse(fake.calls[-1]["as_text"], msg=f"第{i}轮")
            # 每一轮都停在"草案门"，没有升级暂停载荷
            self.assertEqual(payloads[0]["status"], "draft", msg=f"第{i}轮")
            self.assertNotIn("reason", payloads[0], msg=f"第{i}轮")
            self.assertNotIn("escalated", str(payloads[0]), msg=f"第{i}轮")
            # 每轮都按 feedback 自环，计数照旧累加（只记录、不拦截）
            self.assertEqual(out["requirement_refine_result"]["verdict"], "feedback")
            self.assertEqual(out["requirement_refine_count"], i, msg=f"第{i}轮")
            self.assertEqual(out["requirement_draft"], drafts[i - 1])
            state = {**state, **out}
        self.assertEqual(len(fake.calls), 5)
        self.assertEqual(state["requirement_refine_count"], 5)

    def test_confirm_at_third_round_still_calls_model(self):
        # R12（S047 改写）：原"升级暂停确认"用例——count=2（旧上限）后仍调模型产新版草案，
        # 确认则接受新版草案，不再是"接受上一版草案"
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第三版草案-EEE",
                    "change_summary": ["再收窄一点"],
                }
            ]
        )
        state = refine_state(
            requirement_refine_count=2,
            requirement_draft="上一版草案-DDD",
            requirement_refine_feedback="还想再加一点",
        )
        out, payloads, fake_used = run_refine_node(state, ["确认"], fake)
        # 调了一次模型（升级暂停路径已删除）
        self.assertEqual(len(fake_used.calls), 1)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(payloads[0]["requirement_draft"], "第三版草案-EEE")
        self.assertEqual(payloads[0]["requirement_refine_count"], 3)
        # 接受新版草案
        self.assertEqual(out["requirement_refine_result"]["verdict"], "confirm")
        self.assertEqual(out["confirmed_requirement"], "第三版草案-EEE")
        self.assertEqual(out["requirement_draft"], "第三版草案-EEE")
        self.assertEqual(out["requirement_refine_count"], 3)
        # eval_cases 用新版草案重初始化
        self.assertIn("eval_cases", out)
        self.assertIn("第三版草案-EEE", out["eval_cases"][0]["question"])
        # pending=False
        self.assertFalse(out["requirement_refine_pending"])

    def test_reclassify_at_third_round_changes_ai_core(self):
        # R13（S047 改写）：原"升级暂停改判"用例——第 3 轮仍调模型，改判接受新版草案 + 改 ai_core
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第三版草案-FFF",
                    "change_summary": ["收窄"],
                }
            ]
        )
        state = refine_state(
            requirement_refine_count=2,
            requirement_draft="上一版草案-DDD",
            ai_core=True,
        )
        out, payloads, fake_used = run_refine_node(state, ["非AI"], fake)
        self.assertEqual(len(fake_used.calls), 1)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(out["requirement_refine_result"]["verdict"], "reclassify")
        self.assertIs(out["ai_core"], False)
        self.assertEqual(out["confirmed_requirement"], "第三版草案-FFF")
        self.assertIn("eval_cases", out)
        self.assertFalse(out["requirement_refine_pending"])

    def test_reclassify_ai_core_keyword_at_third_round(self):
        # R13b（S047 改写）：原"升级暂停 AI核心 关键词"用例——第 3 轮仍调模型，改判 ai_core=True
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第三版草案-GGG",
                    "change_summary": ["转向"],
                }
            ]
        )
        state = refine_state(
            requirement_refine_count=2,
            requirement_draft="上一版草案-DDD",
            ai_core=False,
        )
        out, payloads, fake_used = run_refine_node(state, ["AI核心"], fake)
        self.assertEqual(len(fake_used.calls), 1)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(out["requirement_refine_result"]["verdict"], "reclassify")
        self.assertIs(out["ai_core"], True)
        self.assertEqual(out["confirmed_requirement"], "第三版草案-GGG")
        self.assertIn("eval_cases", out)
        self.assertFalse(out["requirement_refine_pending"])

    def test_abandon_at_third_round_goes_to_end(self):
        # R14（S047 改写）：原"升级暂停放弃"用例——第 3 轮仍调模型，放弃走 END
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第三版草案-HHH",
                    "change_summary": ["调整"],
                }
            ]
        )
        state = refine_state(
            requirement_refine_count=2,
            requirement_draft="上一版草案-DDD",
        )
        out, payloads, fake_used = run_refine_node(state, ["放弃"], fake)
        self.assertEqual(len(fake_used.calls), 1)
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(out["requirement_refine_result"]["verdict"], "abandon")
        # pending=False（流程结束）
        self.assertFalse(out["requirement_refine_pending"])
        # confirmed_requirement 未改（仍是 refine_state 的 confirmed）
        self.assertNotIn("confirmed_requirement", out)
        # route_after_requirement_refine -> END
        self.assertEqual(route_after_requirement_refine(out), END)

    def test_normal_path_reclassify_changes_ai_core(self):
        # 正常路径下用户改判（带附言或纯改判）-> ai_core 改、confirmed=草案、eval_cases 重初始化
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-III",
                    "change_summary": ["调整"],
                }
            ]
        )
        out, _, _ = run_refine_node(refine_state(), ["非AI"], fake)
        self.assertEqual(out["requirement_refine_result"]["verdict"], "reclassify")
        self.assertIs(out["ai_core"], False)
        self.assertEqual(out["confirmed_requirement"], "整合后的新需求-III")
        self.assertIn("eval_cases", out)
        self.assertFalse(out["requirement_refine_pending"])

    def test_normal_path_abandon_does_not_init_eval_cases(self):
        # 正常路径下用户放弃 -> verdict=abandon，pending=False，不初始化 eval_cases
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "整合后的新需求-JJJ",
                    "change_summary": ["调整"],
                }
            ]
        )
        out, _, _ = run_refine_node(refine_state(), ["放弃"], fake)
        self.assertEqual(out["requirement_refine_result"]["verdict"], "abandon")
        self.assertFalse(out["requirement_refine_pending"])
        # 放弃路径不写 confirmed_requirement / eval_cases
        self.assertNotIn("eval_cases", out)
        self.assertNotIn("confirmed_requirement", out)
        # route -> END
        self.assertEqual(route_after_requirement_refine(out), END)


# ────────────────────────── R15. 普通轨路径不变 ──────────────────────────


class TestNormalTrackUnchanged(unittest.TestCase):
    def test_confirm_route_to_needs_discovery(self):
        # R15：普通轨 confirm -> needs_discovery -> prd_generation（逐字不变）
        # 1) 确认门 confirm 后 route -> needs_discovery
        state = confirm_state()
        state["requirement_refine_pending"] = False  # confirm 路径
        self.assertEqual(
            route_after_requirement_confirm(state), "needs_discovery"
        )

    def test_normal_track_chaining_confirm_discovery_prd(self):
        # 端到端：确认 -> 挖需求 -> 写 PRD（普通轨，逐字不变）
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(
                json_queue=[
                    {"suggestion": "non_ai", "reason": "普通", "signals": ["y"]},
                    # needs_discovery 的 6 类洞察
                    {
                        "target_users": ["产品经理"],
                        "scenarios": ["会议结束后整理纪要"],
                        "pain_points": ["手工整理耗时"],
                        "current_solutions": ["人工听录音记笔记"],
                        "gaps": ["希望自动抽待办"],
                        "success_criteria": ["待办字段齐全无遗漏"],
                    },
                ],
                text_queue=["# 普通 PRD 全文"],
            )
            deps = make_deps(Path(tmp), fake)
            base = confirm_state()
            # 1) 需求确认门答复空（confirm）-> pending=False, ai_core 沿用模型建议=False
            confirm_node = make_requirement_confirm(deps)
            with patch("nodes.hitl.interrupt", return_value=""):
                confirm_out = confirm_node(base)
            self.assertFalse(confirm_out["requirement_refine_pending"])
            self.assertIs(confirm_out["ai_core"], False)
            # route -> needs_discovery
            self.assertEqual(
                route_after_requirement_confirm(confirm_out), "needs_discovery"
            )

            # 2) 合并 state 后挖需求
            merged = {**base, **confirm_out}
            discovery_out = make_needs_discovery(deps)(merged)
            merged.update(discovery_out)
            # user_insights 非空
            insights = merged.get("user_insights") or {}
            self.assertTrue(any(bool(v) for v in insights.values()))

            # 3) 挖完按 ai_core=False 路由去写 PRD
            self.assertEqual(
                route_after_needs_discovery(merged), "prd_generation"
            )

            # 4) PRD 生成（普通模板，逐字不变）
            prd_out = make_prd_generation(deps)(merged)
            self.assertEqual(prd_out["prd_markdown"], "# 普通 PRD 全文")
            # 普通 prompt 不含 ai-native 专有锚点
            self.assertNotIn("AI 协作边界表", fake.calls[-1]["prompt"])


# ────────────────────────── S047. audit_draft_progress 纯函数（零 API） ──────────────────────────


class TestAuditDraftProgress(unittest.TestCase):
    """变化体检纯函数：只提示、不拦流程，四个规则逐条覆盖。"""

    PREV_DRAFT = (
        "第一版草案：帮产品经理整理会议纪要，覆盖使用场景、痛点、现有方案与成功标准，"
        "并自动抽出待办事项清单。"
    )

    def test_similar_drafts_produces_nearly_same_note(self):
        # 规则 1：新版与上一版几乎相同 -> "几乎相同"提示
        result = audit_draft_progress(
            self.PREV_DRAFT, self.PREV_DRAFT, "意见A", "意见B"
        )
        self.assertGreaterEqual(result["similarity"], 0.95)
        self.assertTrue(
            any("几乎相同" in note for note in result["notes"]),
            msg=result["notes"],
        )
        # 长度字段齐备
        self.assertEqual(result["length_prev"], len(self.PREV_DRAFT))
        self.assertEqual(result["length_new"], len(self.PREV_DRAFT))
        self.assertEqual(result["length_delta"], 0)
        self.assertEqual(result["length_ratio"], 1.0)

    def test_nearly_identical_drafts_still_note(self):
        # 规则 1 边界：仅改 1 个字的近同草案也应命中（相似度 >= 0.95）
        new_draft = self.PREV_DRAFT.replace("第一版", "第二版")
        result = audit_draft_progress(
            self.PREV_DRAFT, new_draft, "意见A", "意见B"
        )
        self.assertGreaterEqual(result["similarity"], 0.95)
        self.assertTrue(any("几乎相同" in note for note in result["notes"]))

    def test_shrunk_draft_produces_length_note(self):
        # 规则 2：新版比上一版少 30% 字 -> "少了 N 字"提示
        prev = self.PREV_DRAFT
        new = prev[: int(len(prev) * 0.7)]
        dropped = len(prev) - len(new)
        result = audit_draft_progress(prev, new, "", "")
        self.assertLessEqual(result["length_ratio"], 0.8)
        self.assertEqual(result["length_delta"], -dropped)
        self.assertTrue(
            any(f"少了 {dropped} 字" in note for note in result["notes"]),
            msg=result["notes"],
        )

    def test_repeated_feedback_produces_similar_note(self):
        # 规则 3：本轮意见与上一轮意见高相似 -> "与上一轮提的高度相似"提示
        result = audit_draft_progress(
            "上一版草案：帮产品经理整理会议纪要，覆盖使用场景、痛点与现有方案。",
            "新版草案：帮产品经理整理会议纪要，并自动抽出待办，覆盖使用场景与痛点。",
            "把目标用户收窄到中大型团队",
            "把目标用户再收窄到中大型团队",
        )
        self.assertGreaterEqual(result["feedback_similarity"], 0.8)
        # 草案本身不同、字数未明显缩水，只出意见重复这一条
        self.assertTrue(
            any("与上一轮提的高度相似" in note for note in result["notes"]),
            msg=result["notes"],
        )
        self.assertFalse(any("几乎相同" in note for note in result["notes"]))

    def test_first_round_has_no_notes(self):
        # 规则 4：首轮（prev_draft 为空）-> 全部提示为空、两个相似度为 None
        new_draft = "首版草案：帮产品经理整理会议纪要。"
        result = audit_draft_progress("", new_draft, "", "把目标用户收窄")
        self.assertEqual(result["notes"], [])
        self.assertIsNone(result["similarity"])
        self.assertIsNone(result["feedback_similarity"])
        self.assertEqual(result["length_prev"], 0)
        self.assertEqual(result["length_new"], len(new_draft))
        self.assertIsNone(result["length_ratio"])

    def test_missing_prev_feedback_skips_feedback_note(self):
        # 取不到上一轮意见时（第 2 轮）：feedback_similarity=None，不产意见重复提示
        result = audit_draft_progress(
            "上一版草案：帮产品经理整理会议纪要，覆盖使用场景与痛点。",
            "新版草案：帮产品经理整理会议纪要，覆盖使用场景、痛点与现有方案。",
            "",
            "把目标用户收窄到中大型团队",
        )
        self.assertIsNone(result["feedback_similarity"])
        self.assertFalse(
            any("与上一轮提的高度相似" in note for note in result["notes"])
        )


class TestDraftProgressWiring(unittest.TestCase):
    """载荷接线：中断载荷携带 draft_progress，且渲染白名单含该字段。"""

    def test_first_round_payload_carries_empty_notes(self):
        # 验收 5：首轮 payload 含 draft_progress，notes 为空列表
        refined = "修正后的首版需求文本"
        fake = FakeLLM(
            json_queue=[
                {"refined_requirement": refined, "change_summary": ["首版整合"]}
            ]
        )
        _, payloads, _ = run_refine_node(refine_state(), ["确认"], fake)
        self.assertIn("draft_progress", payloads[0])
        progress = payloads[0]["draft_progress"]
        self.assertEqual(progress["notes"], [])
        self.assertIsNone(progress["similarity"])
        self.assertEqual(progress["length_prev"], 0)
        self.assertEqual(progress["length_new"], len(refined))
        # 老字段照旧在载荷里
        self.assertEqual(payloads[0]["status"], "draft")
        self.assertEqual(payloads[0]["requirement_refine_count"], 1)

    def test_second_round_shrink_payload_carries_note_and_keeps_verdict(self):
        # 验收 3+6：第二轮草案缩水 -> 载荷带"少了 N 字"，四态判定不受提示影响（仍 feedback）
        prev = (
            "上一版草案：帮产品经理整理会议纪要，覆盖使用场景、痛点、现有方案与成功标准，"
            "并自动抽出待办事项清单。"
        )
        new = prev[: int(len(prev) * 0.7)]
        fake = FakeLLM(
            json_queue=[
                {"refined_requirement": new, "change_summary": ["收窄范围"]}
            ]
        )
        state = refine_state(
            requirement_refine_count=1,
            requirement_draft=prev,
            requirement_refine_feedback="把范围收窄",
        )
        out, payloads, _ = run_refine_node(state, ["再改一处"], fake)
        notes = payloads[0]["draft_progress"]["notes"]
        self.assertTrue(any("少了" in note and "字" in note for note in notes), msg=notes)
        # 提示不拦流程：用户文本仍按 feedback 自环
        self.assertEqual(out["requirement_refine_result"]["verdict"], "feedback")
        self.assertEqual(out["requirement_refine_count"], 2)

    def test_third_round_repeated_feedback_note_end_to_end(self):
        # 验收 4（端到端接线）：第 3 轮重复提同一意见 -> 载荷 draft_progress 出现意见重复提示。
        # 同时覆盖"上一轮意见"从 human_feedback 日志按轮次标签取回的真实链路。
        fake = FakeLLM(
            json_queue=[
                {
                    "refined_requirement": "第一版草案：帮产品经理整理会议纪要，覆盖使用场景与痛点。",
                    "change_summary": ["首版整合"],
                },
                {
                    "refined_requirement": "第二版草案：帮产品经理整理会议纪要并自动抽待办，覆盖使用场景与痛点。",
                    "change_summary": ["增加待办抽取"],
                },
                {
                    "refined_requirement": "第三版草案：帮产品经理整理会议纪要并自动抽待办，覆盖使用场景、痛点与成功标准。",
                    "change_summary": ["增加成功标准"],
                },
            ]
        )
        answers = [
            "把目标用户收窄到中大型团队",
            "把目标用户再收窄到中大型团队",
            "把目标用户再收窄到中大型团队",
        ]
        state = refine_state()
        seen: list[dict] = []
        for answer in answers:
            out, payloads, _ = run_refine_node(state, [answer], fake)
            seen.append(payloads[0]["draft_progress"])
            state = {**state, **out}
        # 前两轮取不到"上一轮意见"（第 1 轮首版、第 2 轮日志里还没有更早一轮），不产该提示
        self.assertIsNone(seen[0]["feedback_similarity"])
        self.assertIsNone(seen[1]["feedback_similarity"])
        # 第 3 轮：本轮意见与上一轮高相似 -> 意见重复提示上载荷
        self.assertGreaterEqual(seen[2]["feedback_similarity"], 0.8)
        self.assertTrue(
            any("与上一轮提的高度相似" in note for note in seen[2]["notes"]),
            msg=seen[2]["notes"],
        )
        # 提示不拦流程：三轮都调模型、都按 feedback 自环，计数累加到 3
        self.assertEqual(len(fake.calls), 3)
        self.assertEqual(state["requirement_refine_count"], 3)

    def test_payload_recap_fields_contains_draft_progress(self):
        # 验收 6：渲染白名单含 draft_progress（run_prd_workflow 从同一常量 import，无需重复改）
        from cli.hitl_cli import PAYLOAD_RECAP_FIELDS

        self.assertIn("draft_progress", PAYLOAD_RECAP_FIELDS)
        # 确认导入侧同源：以独立模块名加载脚本（加载不触发 main）
        script_path = REPO_ROOT / "scripts" / "run_prd_workflow.py"
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_refine", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.assertIn("draft_progress", module.PAYLOAD_RECAP_FIELDS)


# ────────────────────────── R16. 图编译与节点数 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_eighteen_nodes(self):
        # R16：图编译通过，节点数 18（17 + requirement_refine）
        # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 新增 eval_gate 后为 19
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            # 新增 requirement_refine 必在位
            self.assertIn("requirement_refine", names)
            # 17 个原节点仍全部在位
            for name in (
                "kb_lookup",
                "intake",
                "requirement_confirm",
                "feasibility_check",
                "feasibility_confirm",
                "needs_discovery",
                "prd_generation",
                "prd_review",
                "eval_design",
                "eval_confirm",
                "bake_off",
                "issue_splitting",
                "issue_confirm",
                "eval_run",
                # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 第 8 段拆两步新增判定门
                "eval_gate",
                "launch_plan",
                # [C 2026-09-16] R11 就绪度打分
                "readiness_assessment",
                "launch_confirm",
                "artifact_persist",
            ):
                self.assertIn(name, names, msg=name)
            # 真实业务节点数 = 20（剔除 langgraph 内置 __start__/__end__）
            real_nodes = names - {"__start__", "__end__"}
            self.assertEqual(len(real_nodes), 20)

    def test_graph_edges_requirement_confirm_conditional(self):
        # 确认门出口改条件边：到 needs_discovery 和 requirement_refine 两条
        # 旧直连边 requirement_confirm -> needs_discovery（无条件）已被条件边取代
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            edges = graph.get_graph().edges
            pairs = {(edge.source, edge.target) for edge in edges}
            # 条件边两态（确认门出口）
            self.assertIn(("requirement_confirm", "needs_discovery"), pairs)
            self.assertIn(("requirement_confirm", "requirement_refine"), pairs)

    def test_graph_edges_requirement_refine_conditional(self):
        # 整合节点条件边三态：到 needs_discovery / requirement_refine 自环 / END
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            edges = graph.get_graph().edges
            pairs = {(edge.source, edge.target) for edge in edges}
            # 条件边三态（整合节点出口）
            self.assertIn(("requirement_refine", "needs_discovery"), pairs)
            self.assertIn(("requirement_refine", "requirement_refine"), pairs)
            self.assertIn(("requirement_refine", END), pairs)

    def test_registry_has_refine_components(self):
        # registry 注册了 requirement_refine prompt 与 schema
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            registered = registry.get_registered()
            self.assertIn("requirement_refine", registered["prompts"])
            self.assertIn("requirement_refine", registered["schemas"])


# ────────────────────────── R17. 可行性门 reshape 回确认门后提 feedback ──────────────────────────


class TestReshapeThenFeedback(unittest.TestCase):
    def test_reshape_back_to_confirm_then_feedback_routes_to_refine(self):
        # R17：可行性门 reshape -> 回 requirement_confirm；
        # 用户在确认门提 feedback -> pending=True，route -> requirement_refine
        # 这条用例只验证路由：reshape 后 state 已写回 requirement_confirm，
        # 用户在确认门提 feedback -> pending=True，route_after_requirement_confirm -> requirement_refine
        # 模拟 reshape 回确认门后的 state（feasibility_confirm 写 verdict=reshape
        # 后由 graph 条件边回 requirement_confirm 节点；user 重新答复 feedback）
        post_reshape_state = {
            "requirement_name": "demo-ai-req",
            "raw_requirement": "用模型整理会议纪要",
            "confirmed_requirement": "用模型整理会议纪要",
            "ai_core": True,  # 上一轮判定
            "feasibility_confirm": {"verdict": "reshape", "user_feedback": "缩小范围"},
            "feasibility_reshape_count": 1,
            "requirement_refine_pending": False,  # 上一轮确认时 pending=False
            "human_feedback": [],
        }
        # 用户在确认门重新答复 feedback
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        out, _ = run_confirm_node(
            post_reshape_state, ["收窄到只整理待办"], fake
        )
        # feedback -> pending=True，feedback=用户文本
        self.assertTrue(out["requirement_refine_pending"])
        self.assertEqual(out["requirement_refine_feedback"], "收窄到只整理待办")
        # confirmed 仍是 raw_requirement（不替换）
        self.assertEqual(out["confirmed_requirement"], "用模型整理会议纪要")
        # route -> requirement_refine
        self.assertEqual(
            route_after_requirement_confirm(out), "requirement_refine"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)


# [C 2026-09-14 by codebuddy-ds41flash] tests/test_requirement_refine.py 新增完成
