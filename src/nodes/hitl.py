# [C 2026-09-09] M6 纵切联调 - HITL 节点（requirement_confirm 需求确认门）
# [C 2026-09-12 by MA] S033 块2a：确认门 capability_boundary 调用换成 ai_triage 分流判定；
#     interrupt 载荷携带 ai_triage；resume 协议四态（确认/「非AI」改判/「AI核心」改判/自由文本修订）；
#     旧 capability_boundary 字段保留供旧检查点兼容，本节点不再写入。
"""需求确认门节点：AI 适用性分流建议 -> interrupt 请用户确认/改判 -> 初始化评测用例。

resume 协议四态（与工单门/发布计划门的关键词风格一致）：
- 「确认」或空 -> 接受模型建议（uncertain 时按 v3.0 默认 ai_core=true）；
- 「非AI」 -> 改判普通轨（ai_core=false，文本仍作为 confirmed_requirement）；
- 「AI核心」 -> 改判 AI 全轨（ai_core=true，文本仍作为 confirmed_requirement）；
- 其他文本 -> 需求修订意见（confirmed_requirement 用用户文本，分流沿用模型建议）。

注意：LangGraph resume 时节点会从头重跑（边界 LLM 会再调一次），这是已知可接受行为。
"""
from __future__ import annotations

import re

from langgraph.types import interrupt

from components.tools.init_eval_cases import init_eval_cases
from kernel.spec import NodeSpec


# [C 2026-09-12 by MA] S033 块2a：分流答复分类常量
# "非AI" 关键词命中即判 non_ai（不做否定前缀排除——用户不会说"不非AI"）
_NON_AI_KEYWORDS: tuple[str, ...] = (
    "非ai",
    "非ai轨",
    "非a.i",
    "改判普通",
    "普通轨",
    "非核心",
)

# "AI核心" 关键词命中且前 3 字内无否定前缀时判 ai_core
_AI_CORE_KEYWORDS: tuple[str, ...] = (
    "ai核心",
    "ai轨",
    "ai全轨",
    "改判ai",
    "ai核心轨",
)

# ai_core 关键词否定前缀（防"不是AI核心""不要 AI 轨"误判）
_AI_CORE_NEGATIONS: tuple[str, ...] = (
    "不",
    "别",
    "勿",
    "不用",
    "不要",
    "不需",
    "不需要",
    "无需",
    "不是",
)


def _has_negative_prefix(text: str, idx: int) -> bool:
    """关键词命中位置前 3 字内是否紧邻否定语（不/别/勿/不用/不要/不是 等）。"""
    window = text[max(0, idx - 3):idx]
    return any(window.endswith(neg) for neg in _AI_CORE_NEGATIONS)


def _matches_ai_core(compact: str) -> bool:
    """去空白文本上任一 ai_core 关键词命中、且命中位置不紧邻否定前缀时返回 True。"""
    for keyword in _AI_CORE_KEYWORDS:
        start = 0
        while True:
            idx = compact.find(keyword, start)
            if idx < 0:
                break
            if not _has_negative_prefix(compact, idx):
                return True
            start = idx + 1  # 同一关键词可能多次出现，继续找下一处
    return False


def _matches_non_ai(compact: str) -> bool:
    """去空白文本上任一 non_ai 关键词命中即返回 True。"""
    for keyword in _NON_AI_KEYWORDS:
        if keyword in compact:
            return True
    return False


def classify_requirement_answer(text: str) -> str:
    """纯函数：把需求确认门用户答复归一化为四分类。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做非AI/AI核心关键词包含判定**：去空白（含全角空格）后，
       - 命中任一 ``_NON_AI_KEYWORDS`` -> ``non_ai``（ai_core=false）；
       - 命中任一 ``_AI_CORE_KEYWORDS`` 且前 3 字内无否定前缀 -> ``ai_core``
         （ai_core=true）；
       - ai_core 关键词紧邻否定前缀（如"不是AI核心"）不判 ai_core，落 feedback；
    3. **再做确认精确判定**：归一化后恰好为空串或 "confirmed" 才算 confirm
       （与既有 requirement_confirm 行为一致，普通轨行为不变）；
    4. 其余文本 -> feedback（需求修订意见，分流沿用模型建议，confirmed_requirement 用用户文本）。

    Returns:
        ``confirm`` / ``non_ai`` / ``ai_core`` / ``feedback``
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    # 去除空白（含全角空格）做包含判定
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    if _matches_non_ai(compact):
        return "non_ai"
    if _matches_ai_core(compact):
        return "ai_core"
    if not lowered or lowered == "confirmed":
        return "confirm"
    return "feedback"
    # [C 2026-09-12 by MA] 需求确认门答复分类纯函数


def _resolve_ai_core(kind: str, model_suggestion: str) -> bool:
    """根据用户答复 kind 与模型建议 suggestion 推断最终 ai_core 布尔值。

    - non_ai -> False
    - ai_core -> True
    - confirm/feedback -> 接受模型建议：ai_core -> True / non_ai -> False /
      uncertain -> True（v3.0 第三节：存疑时按 AI 核心走，探针实测后可改判回普通轨）
    """
    if kind == "non_ai":
        return False
    if kind == "ai_core":
        return True
    # confirm / feedback：接受模型建议，uncertain -> True
    return model_suggestion != "non_ai"


def make_requirement_confirm(deps):
    """需求确认门（HITL）：AI 适用性分流建议 + 用户确认/改判 + 初始化评测用例。"""

    def requirement_confirm(state: dict) -> dict:
        # 1. AI 适用性分流建议（替换旧 capability_boundary 调用）
        # [C 2026-09-12 by MA] S033 块2a：旧 capability_boundary 字段保留不写，
        # 块 3 可行性门会重新设计为语义不同的产品能力三色表
        prompt = deps.registry.read_prompt("ai_triage")
        schema = deps.registry.load_schema("ai_triage")
        spec = NodeSpec(
            name="ai_triage",
            prompt_template=prompt,
            output_schema=schema,
        )
        ai_triage = deps.runner.run_raw(spec, state)
        model_suggestion = str(ai_triage.get("suggestion") or "")

        # 2. 中断，请用户确认/改判/修订需求
        user_input = interrupt(
            value={
                "node": "requirement_confirm",
                "requirement_name": state.get("requirement_name", ""),
                "raw_requirement": state.get("raw_requirement", ""),
                "info_completeness": state.get("info_completeness"),
                "ai_triage": ai_triage,
            }
        )

        # 3. resume 值归一化四态：confirm / non_ai / ai_core / feedback
        original = user_input if isinstance(user_input, str) else str(user_input)
        kind = classify_requirement_answer(original)
        text = original.strip()
        ai_core = _resolve_ai_core(kind, model_suggestion)

        # 4. confirmed_requirement 解析
        #    - confirm：用原 raw_requirement
        #    - non_ai / ai_core：用户若附文本则用文本，否则用原 raw_requirement
        #    - feedback：用用户文本作为需求修订
        if kind == "confirm":
            confirmed = state.get("raw_requirement", "")
        elif kind in ("non_ai", "ai_core"):
            confirmed = text if text else state.get("raw_requirement", "")
        else:  # feedback
            confirmed = text

        # 5. 初始化评测用例（纯确定性模板生成）
        eval_cases = init_eval_cases({**state, "confirmed_requirement": confirmed})

        return {
            "ai_triage": ai_triage,
            "ai_core": ai_core,
            "confirmed_requirement": confirmed,
            "proceed_decision": True,
            "eval_cases": eval_cases,
            "human_feedback": (state.get("human_feedback") or [])
            + [{"node": "requirement_confirm", "feedback": original, "kind": kind}],
            "current_stage": "探索",
        }

    return requirement_confirm


# [C 2026-09-12 by MA] S033 块2a：确认门接入 ai_triage 分流 + resume 四态协议
