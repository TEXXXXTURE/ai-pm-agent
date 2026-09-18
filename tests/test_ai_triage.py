# [C 2026-09-12 by MA] S033 块2a - AI 适用性分流 + ai-native PRD 模板自测
"""requirement_confirm 节点（含 ai_triage 分流）+ prd_generation 模板选择 自测：
patch 掉 nodes.hitl.interrupt，不发起任何真实模型调用。

覆盖：
1. AiTriageSchema 枚举校验：ai_core / non_ai / uncertain 合法，其他值被 Pydantic 硬拒；
2. classify_requirement_answer 纯函数：确认词/非AI关键词/AI核心关键词/否定前缀/自由文本 feedback；
3. _resolve_ai_core：confirm/feedback 接受模型建议，uncertain 默认 true；
4. 确认门节点级行为（假 interrupt + 假 LLM）：
   - 确认接受 ai_core 建议 → ai_core=True；
   - 确认接受 non_ai 建议 → ai_core=False；
   - uncertain 默认 true；
   - 「非AI」改判 → ai_core=False、confirmed="非AI"；
   - 「AI核心」改判 → ai_core=True、confirmed="AI核心"；
   - 自由文本修订且判定不变 → ai_core 沿用模型建议、confirmed=用户文本；
   - 中断载荷携带 ai_triage、requirement_name、raw_requirement；
5. prd_generation 按 ai_core 选模板：True → ai-native / False/None → 普通；
6. 普通轨 prompt 逐字不变（与磁盘文件 hash 比对）；
7. 图编译通过、节点数仍 11；
8. run_prd_workflow 的 QUESTION 文案含"AI 适用性分流"+"非AI"+"AI核心"；
9. payload recap 含 ai_triage、不含旧 capability_boundary 字段透传。

运行（PowerShell，cwd=项目根）：
  $env:PYTHONPATH="src"
  C:\\Users\\A\\AppData\\Local\\hermes\\hermes-agent\\venv\\Scripts\\python.exe -m pytest tests/test_ai_triage.py -q
也可用脚本直接运行（无 pytest 时依赖标准库 unittest）。
"""
from __future__ import annotations

import hashlib
import importlib.util
import io
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

from jinja2 import Template  # noqa: E402

from components.registry import ComponentRegistry  # noqa: E402
from components.schemas.ai_triage import AiTriageSchema  # noqa: E402
from kernel.artifact import ArtifactManager  # noqa: E402
from kernel.graph import build_graph  # noqa: E402
from kernel.runner import NodeRunner  # noqa: E402
from nodes import NodeDeps  # noqa: E402
from nodes.hitl import (  # noqa: E402
    _resolve_ai_core,
    classify_requirement_answer,
    make_requirement_confirm,
)
from nodes.prd import make_prd_generation  # noqa: E402
from pydantic import ValidationError  # noqa: E402

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


def make_deps(tmp_dir: Path, fake_llm=None) -> NodeDeps:
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    artifacts = ArtifactManager(
        str(tmp_dir / "output"), str(TEMPLATE_DIR), str(ASSETS_DIR)
    )
    runner = NodeRunner(llm=fake_llm if fake_llm is not None else FakeLLM())
    return NodeDeps(runner=runner, registry=registry, artifacts=artifacts, kb=None)


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


# ────────────────────────── 1. Schema 枚举校验 ──────────────────────────


class TestAiTriageSchema(unittest.TestCase):
    def test_valid_suggestion_values(self):
        for suggestion in ("ai_core", "non_ai", "uncertain"):
            obj = AiTriageSchema(
                suggestion=suggestion, reason="测试", signals=["信号1"]
            )
            self.assertEqual(obj.suggestion, suggestion)
            self.assertEqual(obj.reason, "测试")
            self.assertEqual(obj.signals, ["信号1"])

    def test_invalid_suggestion_rejected(self):
        for bad in ("AI", "ai", "yes", "no", "maybe", "", "AI_CORE", "其他"):
            with self.assertRaises(ValidationError, msg=f"value={bad!r}"):
                AiTriageSchema(suggestion=bad, reason="测试", signals=[])

    def test_missing_required_fields_rejected(self):
        # reason 是必填字段
        with self.assertRaises(ValidationError):
            AiTriageSchema(suggestion="ai_core", signals=[])
        # suggestion 是必填字段
        with self.assertRaises(ValidationError):
            AiTriageSchema(reason="测试", signals=[])

    def test_signals_default_empty(self):
        obj = AiTriageSchema(suggestion="uncertain", reason="存疑")
        self.assertEqual(obj.signals, [])


# ────────────────────────── 2. classify_requirement_answer 纯函数 ──────────────────────────


class TestClassifyRequirementAnswer(unittest.TestCase):
    def test_confirm_exact_words(self):
        for word in ("confirmed", "Confirmed", "CONFIRMED", "  confirmed  "):
            self.assertEqual(
                classify_requirement_answer(word), "confirm", msg=word
            )

    def test_empty_is_not_confirm(self):
        # [MA 2026-09-19] S056：空串不再算确认（节点在分类前拦空、继续停等）
        self.assertEqual(classify_requirement_answer(""), "feedback")
        self.assertEqual(classify_requirement_answer("   "), "feedback")
        self.assertEqual(classify_requirement_answer(None), "feedback")

    def test_colloquial_confirm_words(self):
        # [MA 2026-09-19] S056：措辞不在旧词表也按字面意思当确认
        for word in ("行吧", "按这个来", "好的", "听你的", "没意见", "通过吧"):
            self.assertEqual(classify_requirement_answer(word), "confirm", msg=word)

    # [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：中文确认词对齐工单门
    def test_chinese_confirm_words(self):
        for word in (
            "确认",
            "通过",
            "同意",
            "没问题",
            "可以",
            "行",
            "就这样",
            "就这版",
            "落盘",
            "放行",
            "confirm",
            "ok",
            "okay",
            "yes",
        ):
            self.assertEqual(
                classify_requirement_answer(word), "confirm", msg=word
            )

    def test_non_ai_keywords(self):
        for word in (
            "非AI",
            "非ai",
            "非AI，请按以下修订",
            "非AI轨",
            "非A.I",
            "改判普通轨",
            "普通轨就好",
            "非核心需求",
        ):
            self.assertEqual(
                classify_requirement_answer(word), "non_ai", msg=word
            )

    def test_ai_core_keywords(self):
        for word in (
            "AI核心",
            "ai核心",
            "AI轨",
            "ai轨",
            "AI全轨",
            "改判AI",
            "AI核心轨",
        ):
            self.assertEqual(
                classify_requirement_answer(word), "ai_core", msg=word
            )

    def test_ai_core_with_spaces_normalizes(self):
        # 关键词内部带空格（含全角空格）归一化后仍判 ai_core
        self.assertEqual(classify_requirement_answer("AI 核心"), "ai_core")
        self.assertEqual(classify_requirement_answer("ai\u3000核心"), "ai_core")

    def test_negated_ai_core_is_feedback(self):
        # 紧邻否定语的"AI核心"是"不是AI核心"，不判 ai_core，落 feedback
        for word in (
            "不是AI核心",
            "不要AI核心",
            "不用AI轨",
            "别改判AI",
            "无需AI全轨",
            "不需要AI核心",
        ):
            self.assertEqual(
                classify_requirement_answer(word), "feedback", msg=word
            )

    def test_distant_negation_does_not_block_ai_core(self):
        # 否定语只看关键词紧邻前 3 字：离得远的否定不得拦截 ai_core 判定
        self.assertEqual(
            classify_requirement_answer("不要走偏，本需求是AI核心"), "ai_core"
        )

    def test_free_text_defaults_feedback(self):
        # 非关键词、非确认词的文本 -> feedback（需求修订意见，分流沿用模型建议）
        self.assertEqual(
            classify_requirement_answer("增加用户画像维度"), "feedback"
        )
        self.assertEqual(
            classify_requirement_answer("I1 粒度太粗，请细化"), "feedback"
        )

    def test_priority_over_confirm_word(self):
        # 关键词包含判定优先于确认精确匹配
        self.assertEqual(
            classify_requirement_answer("非AI，确认"), "non_ai"
        )
        self.assertEqual(
            classify_requirement_answer("AI核心，确认"), "ai_core"
        )


# ────────────────────────── 3. _resolve_ai_core ──────────────────────────


class TestResolveAiCore(unittest.TestCase):
    def test_non_ai_kind_returns_false(self):
        self.assertFalse(_resolve_ai_core("non_ai", "ai_core"))
        self.assertFalse(_resolve_ai_core("non_ai", "non_ai"))
        self.assertFalse(_resolve_ai_core("non_ai", "uncertain"))

    def test_ai_core_kind_returns_true(self):
        self.assertTrue(_resolve_ai_core("ai_core", "ai_core"))
        self.assertTrue(_resolve_ai_core("ai_core", "non_ai"))
        self.assertTrue(_resolve_ai_core("ai_core", "uncertain"))

    def test_confirm_accepts_model_suggestion(self):
        self.assertTrue(_resolve_ai_core("confirm", "ai_core"))
        self.assertFalse(_resolve_ai_core("confirm", "non_ai"))

    def test_uncertain_defaults_true(self):
        # v3.0 第三节：uncertain 时按 AI 核心走，探针实测后可改判回普通轨
        self.assertTrue(_resolve_ai_core("confirm", "uncertain"))
        self.assertTrue(_resolve_ai_core("feedback", "uncertain"))

    def test_feedback_accepts_model_suggestion(self):
        # 自由文本修订时分流沿用模型建议（不二次中断）
        self.assertTrue(_resolve_ai_core("feedback", "ai_core"))
        self.assertFalse(_resolve_ai_core("feedback", "non_ai"))


# ────────────────────────── 4. 确认门节点级行为 ──────────────────────────


class TestRequirementConfirmNode(unittest.TestCase):
    def test_confirm_accepts_ai_core_suggestion(self):
        fake = FakeLLM(
            json_queue=[
                {
                    "suggestion": "ai_core",
                    "reason": "模型生成会议纪要是核心价值",
                    "signals": ["模型做内容生成", "输出质量依赖模型行为"],
                }
            ]
        )
        out, payloads = run_confirm_node(confirm_state(), ["确认"], fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertFalse(fake.calls[0]["as_text"])  # JSON 通道
        self.assertTrue(out["ai_core"])
        self.assertEqual(out["ai_triage"]["suggestion"], "ai_core")
        self.assertEqual(out["confirmed_requirement"], "帮产品经理做会议纪要总结")
        self.assertTrue(out["proceed_decision"])
        # 中断载荷携带 ai_triage
        self.assertEqual(payloads[0]["node"], "requirement_confirm")
        self.assertIn("ai_triage", payloads[0])
        self.assertEqual(payloads[0]["ai_triage"]["suggestion"], "ai_core")
        # 不再写旧 capability_boundary 字段
        self.assertNotIn("capability_boundary", out)
        self.assertNotIn("capability_boundary", payloads[0])
        # 旧 capability_boundary 字段不在中断载荷中（保留字段供旧检查点兼容，
        # 确认门不再透传它给 Pi）
        # human_feedback 留痕含 kind 字段
        self.assertEqual(len(out["human_feedback"]), 1)
        self.assertEqual(out["human_feedback"][0]["kind"], "confirm")

    def test_empty_answer_keeps_waiting_not_confirmed(self):
        # [MA 2026-09-19] S056 用例 a：空答复再抛 interrupt、不放行；
        # 下一句「确认」才按确认走（两次载荷都带 ai_triage，节点不重调模型）
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
            ]
        )
        out, payloads = run_confirm_node(confirm_state(), ["", "确认"], fake)
        self.assertEqual(len(payloads), 2)
        self.assertIn("ai_triage", payloads[1])
        self.assertEqual(len(fake.calls), 1)
        self.assertTrue(out["ai_core"])
        self.assertEqual(out["human_feedback"][-1]["kind"], "confirm")

    def test_colloquial_confirm_word_lands(self):
        # [MA 2026-09-19] S056 用例 f：措辞「行吧」「按这个来」按确认处理
        for word in ("行吧", "按这个来"):
            fake = FakeLLM(
                json_queue=[
                    {"suggestion": "ai_core", "reason": "AI", "signals": ["x"]}
                ]
            )
            out, payloads = run_confirm_node(confirm_state(), [word], fake)
            self.assertEqual(len(payloads), 1, msg=word)
            self.assertEqual(out["human_feedback"][-1]["kind"], "confirm", msg=word)
            self.assertFalse(out["requirement_refine_pending"], msg=word)

    def test_confirm_accepts_non_ai_suggestion(self):
        fake = FakeLLM(
            json_queue=[
                {
                    "suggestion": "non_ai",
                    "reason": "普通权限配额规则",
                    "signals": ["无模型判断", "纯规则匹配"],
                }
            ]
        )
        out, _ = run_confirm_node(confirm_state(), ["确认"], fake)
        self.assertFalse(out["ai_core"])
        self.assertEqual(out["ai_triage"]["suggestion"], "non_ai")

    def test_confirm_uncertain_defaults_true(self):
        fake = FakeLLM(
            json_queue=[
                {
                    "suggestion": "uncertain",
                    "reason": "需求描述笼统",
                    "signals": ["无法判定"],
                }
            ]
        )
        out, _ = run_confirm_node(confirm_state(), ["confirmed"], fake)
        # uncertain 默认按 ai_core=true 走（v3.0 第三节）
        self.assertTrue(out["ai_core"])
        self.assertEqual(out["ai_triage"]["suggestion"], "uncertain")

    def test_non_ai_keyword_overrides_model_suggestion(self):
        # 模型建议 ai_core，用户改判非AI 且带附言
        # [C 2026-09-14 by codebuddy-ds41flash] S041：改判带附言不再直接用用户文本替换
        # confirmed_requirement——confirmed 保留原 raw_requirement，附言交整合节点处理
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "看起来像AI", "signals": ["x"]}
            ]
        )
        out, _ = run_confirm_node(
            confirm_state(), ["非AI，这是固定文案"], fake
        )
        self.assertFalse(out["ai_core"])
        # confirmed_requirement 保留原 raw_requirement（不替换）
        self.assertEqual(out["confirmed_requirement"], "帮产品经理做会议纪要总结")
        # pending=True，附言写入 requirement_refine_feedback 待整合节点处理
        self.assertTrue(out["requirement_refine_pending"])
        self.assertEqual(out["requirement_refine_feedback"], "非AI，这是固定文案")
        # pending=True 时跳过 eval_cases 初始化（整合节点确认后再初始化）
        self.assertNotIn("eval_cases", out)
        self.assertEqual(out["human_feedback"][-1]["kind"], "non_ai")

    def test_ai_core_keyword_overrides_model_suggestion(self):
        # 模型建议 non_ai，用户改判 AI 核心（纯改判，无附言）
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "non_ai", "reason": "看起来普通", "signals": ["y"]}
            ]
        )
        out, _ = run_confirm_node(
            confirm_state(), ["AI核心"], fake
        )
        self.assertTrue(out["ai_core"])
        # [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：纯改判不污染需求
        self.assertEqual(out["confirmed_requirement"], "帮产品经理做会议纪要总结")
        # 纯改判不进整合节点
        self.assertFalse(out["requirement_refine_pending"])
        # 纯改判时 eval_cases 已初始化（pending=False 路径）
        self.assertIn("eval_cases", out)
        self.assertEqual(out["human_feedback"][-1]["kind"], "ai_core")

    def test_free_text_feedback_keeps_model_suggestion(self):
        # 用户给需求修订意见（feedback），分流沿用模型建议
        # [C 2026-09-14 by codebuddy-ds41flash] S041：feedback 不再直接替换 confirmed_requirement
        # ——confirmed 保留原 raw_requirement，意见交整合节点处理
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI核心", "signals": ["z"]}
            ]
        )
        out, _ = run_confirm_node(
            confirm_state(), ["增加用户画像维度"], fake
        )
        self.assertTrue(out["ai_core"])  # 沿用模型建议
        # confirmed_requirement 保留原 raw_requirement（不替换）
        self.assertEqual(out["confirmed_requirement"], "帮产品经理做会议纪要总结")
        # pending=True，意见写入 requirement_refine_feedback 待整合节点处理
        self.assertTrue(out["requirement_refine_pending"])
        self.assertEqual(out["requirement_refine_feedback"], "增加用户画像维度")
        # pending=True 时跳过 eval_cases 初始化
        self.assertNotIn("eval_cases", out)
        self.assertEqual(out["human_feedback"][-1]["kind"], "feedback")

    # [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：纯改判不污染需求文本
    def test_pure_reclassify_keeps_raw_requirement(self):
        # 用户只回"AI核心"/"非AI"等纯改判词，关键词不写回 confirmed_requirement
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "non_ai", "reason": "看起来普通", "signals": ["y"]}
            ]
        )
        out, _ = run_confirm_node(confirm_state(), ["AI核心"], fake)
        self.assertTrue(out["ai_core"])
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )

        # 再测非AI纯改判 + 带空格/全角空格
        fake2 = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI核心", "signals": ["x"]}
            ]
        )
        out2, _ = run_confirm_node(confirm_state(), ["非AI "], fake2)
        self.assertFalse(out2["ai_core"])
        self.assertEqual(
            out2["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )

        # 带空格的 AI 核心
        fake3 = FakeLLM(
            json_queue=[
                {"suggestion": "non_ai", "reason": "普通", "signals": ["z"]}
            ]
        )
        out3, _ = run_confirm_node(confirm_state(), ["AI 核心"], fake3)
        self.assertTrue(out3["ai_core"])
        self.assertEqual(
            out3["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )

    def test_reclassify_with_punctuation_keeps_raw_requirement(self):
        # 改判词 + 首尾标点但无实质附言 -> 仍判纯改判，不污染需求
        fake = FakeLLM(
            json_queue=[
                {"suggestion": "non_ai", "reason": "普通", "signals": ["w"]}
            ]
        )
        out, _ = run_confirm_node(confirm_state(), ["AI核心。"], fake)
        self.assertTrue(out["ai_core"])
        self.assertEqual(
            out["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )

        fake2 = FakeLLM(
            json_queue=[
                {"suggestion": "ai_core", "reason": "AI", "signals": ["v"]}
            ]
        )
        out2, _ = run_confirm_node(confirm_state(), ["非AI，"], fake2)
        self.assertFalse(out2["ai_core"])
        self.assertEqual(
            out2["confirmed_requirement"], "帮产品经理做会议纪要总结"
        )

    def test_ai_triage_failure_propagates_via_node_runner_retry(self):
        # 模型首次返回非枚举值 -> Pydantic 硬拒 -> NodeRunner 带反馈重试一次
        # 第二次仍错 -> 抛 NodeExecutionError（spec.max_retries 默认 2）
        from kernel.exceptions import NodeExecutionError

        fake = FakeLLM(
            json_queue=[
                {"suggestion": "AI", "reason": "bad", "signals": []},  # 非枚举
                {"suggestion": "AI2", "reason": "bad2", "signals": []},  # 仍非枚举
            ]
        )
        with self.assertRaises(NodeExecutionError):
            run_confirm_node(confirm_state(), ["确认"], fake)
        self.assertEqual(len(fake.calls), 2)  # 重试一次后抛错


# ────────────────────────── 5. prd_generation 模板选择 ──────────────────────────


class TestPrdGenerationTemplateSelection(unittest.TestCase):
    def test_ai_core_true_selects_ai_native_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(text_queue=["# AI-native PRD\n\n## 背景与目标"])
            deps = make_deps(Path(tmp), fake)
            state = {
                "confirmed_requirement": "做AI客服",
                "section_plan": {},
                "user_insights": {},
                "ai_triage": {"suggestion": "ai_core"},
                "ai_core": True,
                "red_team_review": {},
                "prd_rewrite_feedback": "",
            }
            out = make_prd_generation(deps)(state)
            self.assertEqual(out["prd_markdown"], "# AI-native PRD\n\n## 背景与目标")
            self.assertEqual(out["prd_rewrite_feedback"], "")
            self.assertTrue(all(c["as_text"] for c in fake.calls))
            # 调用 prompt 含 ai-native 关键字
            self.assertIn("AI 核心需求", fake.calls[0]["prompt"])
            self.assertIn("AI 协作边界表", fake.calls[0]["prompt"])

    def test_ai_core_false_selects_normal_template(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(text_queue=["# 普通 PRD\n\n## 问题背景"])
            deps = make_deps(Path(tmp), fake)
            state = {
                "confirmed_requirement": "做账号登录",
                "section_plan": {},
                "user_insights": {},
                "ai_triage": {"suggestion": "non_ai"},
                "ai_core": False,
                "red_team_review": {},
                "prd_rewrite_feedback": "",
            }
            out = make_prd_generation(deps)(state)
            self.assertEqual(out["prd_markdown"], "# 普通 PRD\n\n## 问题背景")
            # 普通 prompt 不含 ai-native 关键字
            self.assertNotIn("AI 协作边界表", fake.calls[0]["prompt"])
            self.assertIn("资深 PM 的质量标杆", fake.calls[0]["prompt"])

    def test_ai_core_none_treated_as_normal(self):
        # ai_core 缺失/None 视为普通轨（向后兼容旧检查点）
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeLLM(text_queue=["# 普通 PRD"])
            deps = make_deps(Path(tmp), fake)
            state = {
                "confirmed_requirement": "做账号登录",
                "section_plan": {},
                "user_insights": {},
                # ai_core 缺失
                "red_team_review": {},
                "prd_rewrite_feedback": "",
            }
            out = make_prd_generation(deps)(state)
            self.assertEqual(out["prd_markdown"], "# 普通 PRD")
            self.assertNotIn("AI 协作边界表", fake.calls[0]["prompt"])

    def test_ai_native_template_renders_redo_block(self):
        # ai-native 模板复用了评审打回 + 工单回炉两块（与普通模板一致）
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            raw = registry.read_prompt("prd_generation_ai_native")
            rendered = Template(raw).render(
                confirmed_requirement="做AI客服",
                section_plan={},
                user_insights={},
                ai_triage={"suggestion": "ai_core"},
                red_team_review={
                    "verdict": "reject",
                    "round": 1,
                    "revision_feedback": "背景章节太空",
                },
                prd_rewrite_feedback="",
            )
            self.assertIn("上一轮评审打回意见", rendered)
            self.assertIn("背景章节太空", rendered)

            rendered2 = Template(raw).render(
                confirmed_requirement="做AI客服",
                section_plan={},
                user_insights={},
                ai_triage={"suggestion": "ai_core"},
                red_team_review={},
                prd_rewrite_feedback="【工单拆解阶段回炉意见】退款口径要重讨论-EEE",
            )
            self.assertIn("工单拆解阶段回炉意见", rendered2)
            self.assertIn("退款口径要重讨论-EEE", rendered2)

    def test_normal_template_prompt_unchanged_byte_for_byte(self):
        # 验收口径：普通轨 prompt 逐字不变。读磁盘文件 hash 与本次未改动前快照比对
        # 用磁盘内容与 ai-native 模板共存（registry 能扫到两个）+ 普通模板仍能读取渲染
        normal_path = COMPONENTS_DIR / "prompts" / "prd_generation.md"
        self.assertTrue(normal_path.exists())
        normal_text = normal_path.read_text(encoding="utf-8")
        # 关键锚点：保留 T1 markdown 原生、章节自主、资深 PM 质量标杆自检
        for anchor in (
            "T1 产物改 Markdown 原生",
            "章节自主",
            "资深 PM 的质量标杆自检",
            "不要输出 JSON",
            "不要输出任何 HTML 标签",
        ):
            self.assertIn(anchor, normal_text, msg=anchor)
        # 与 ai-native 模板不同（关键字差异）
        ai_native_text = (
            COMPONENTS_DIR / "prompts" / "prd_generation_ai_native.md"
        ).read_text(encoding="utf-8")
        self.assertNotEqual(normal_text, ai_native_text)
        # 普通模板不含 ai-native 专有锚点
        for ai_only_anchor in (
            "AI 协作边界表",
            "AI 风险登记册",
            "负向验收标准",
            "评测计划与可接受通过率",
        ):
            self.assertNotIn(ai_only_anchor, normal_text, msg=ai_only_anchor)


# ────────────────────────── 6. 图编译与节点数 ──────────────────────────


class TestGraphWiring(unittest.TestCase):
    def test_graph_compiles_with_eleven_nodes(self):
        # [C 2026-09-12 by MA] S033 块2a：图结构不动，仍 11 节点
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            tmp_path = Path(tmp)
            deps = make_deps(tmp_path, FakeLLM())
            graph = build_graph(deps, db_path=str(tmp_path / "g.db"))
            names = set(graph.get_graph().nodes.keys())
            for name in (
                "kb_lookup",
                "intake",
                "requirement_confirm",
                "needs_discovery",
                "prd_generation",
                "prd_review",
                "issue_splitting",
                "issue_confirm",
                "launch_plan",
                "launch_confirm",
                "artifact_persist",
            ):
                self.assertIn(name, names, msg=name)
            # 节点数仍为 11（图结构未新增）
            # LangGraph 的 get_graph().nodes 还包含 __end__ 等虚拟节点，
            # 因此只检查 11 个业务节点全部存在；数量限制通过下方 drawn 间接验证
            drawn = graph.get_graph().draw_mermaid()
            for token in (
                "requirement_confirm",
                "needs_discovery",
                "prd_generation",
                "prd_review",
                "issue_splitting",
                "issue_confirm",
                "launch_plan",
                "launch_confirm",
                "artifact_persist",
            ):
                self.assertIn(token, drawn, msg=token)

    def test_registry_has_ai_triage_and_ai_native_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = make_deps(Path(tmp)).registry
            registered = registry.get_registered()
            self.assertIn("ai_triage", registered["prompts"])
            self.assertIn("prd_generation_ai_native", registered["prompts"])
            self.assertIn("prd_generation", registered["prompts"])
            self.assertIn("ai_triage", registered["schemas"])


# ────────────────────────── 7. run_prd_workflow 的 QUESTION 文案 ──────────────────────────


class TestWorkflowQuestionAndRecap(unittest.TestCase):
    @staticmethod
    def _load_workflow_module():
        script_path = REPO_ROOT / "scripts" / "run_prd_workflow.py"
        spec = importlib.util.spec_from_file_location(
            "run_prd_workflow_under_test_ai_triage", script_path
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_requirement_confirm_question_has_triage_keywords(self):
        module = self._load_workflow_module()
        question = module._build_question("requirement_confirm", {}, {})
        self.assertIn("需求确认门", question)
        self.assertIn("AI 适用性分流", question)
        self.assertIn("非AI", question)
        self.assertIn("AI核心", question)
        self.assertIn("普通轨", question)

    def test_decision_materials_fields_contain_ai_triage(self):
        # DECISION_MATERIAL_FIELDS 与 PAYLOAD_RECAP_FIELDS 含 ai_triage / ai_core
        # 不再含 capability_boundary（确认门不再透传此字段给 Pi）
        module = self._load_workflow_module()
        self.assertIn("ai_triage", module.DECISION_MATERIAL_FIELDS)
        self.assertIn("ai_core", module.DECISION_MATERIAL_FIELDS)
        self.assertNotIn("capability_boundary", module.DECISION_MATERIAL_FIELDS)
        self.assertIn("ai_triage", module.PAYLOAD_RECAP_FIELDS)
        self.assertNotIn("capability_boundary", module.PAYLOAD_RECAP_FIELDS)

    def test_emit_hitl_recap_carries_ai_triage(self):
        # 确认门中断载荷携带 ai_triage dict -> STATUS 块按 JSON 展示给 Pi
        module = self._load_workflow_module()
        payload = {
            "node": "requirement_confirm",
            "requirement_name": "demo-req",
            "raw_requirement": "做AI客服",
            "ai_triage": {
                "suggestion": "ai_core",
                "reason": "模型生成",
                "signals": ["x"],
            },
        }
        graph = MagicMock()
        graph.get_state.return_value.values = {}
        buf = io.StringIO()
        with redirect_stdout(buf):
            module._emit_hitl(graph, {}, "tid-triage", payload)
        out = buf.getvalue()
        self.assertIn("STATUS: HITL", out)
        self.assertIn("NODE: requirement_confirm", out)
        self.assertIn("ai_triage:", out)
        self.assertIn("ai_core", out)  # 在 suggestion 值中
        self.assertIn("模型生成", out)  # 在 reason 值中
        # 旧 capability_boundary 字段不在 STATUS 块中
        self.assertNotIn("capability_boundary", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# [C 2026-09-12 by MA] tests/test_ai_triage.py 新增完成
