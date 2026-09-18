# [C 2026-09-09] M6 纵切联调 - HITL 节点（requirement_confirm 需求确认门）
# [C 2026-09-12 by MA] S033 块2a：确认门 capability_boundary 调用换成 ai_triage 分流判定；
#     interrupt 载荷携带 ai_triage；resume 协议四态（确认/「非AI」改判/「AI核心」改判/自由文本修订）；
#     旧 capability_boundary 字段保留供旧检查点兼容，本节点不再写入。
"""需求确认门节点：AI 适用性分流建议 -> interrupt 请用户确认/改判 -> 初始化评测用例。

resume 协议四态（与工单门/发布计划门的关键词风格一致）：
- 确认词（如「确认」「行吧」「按这个来」）-> 接受模型建议（uncertain 时按 v3.0 默认 ai_core=true）；
  空答复不算确认：不 resume，继续停在本节点等下一句；
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

# [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：确认词集合对齐工单门（issues.py _CONFIRM_WORDS）
# [MA 2026-09-19] S056：去空串（空答复不再算确认，节点在分类前拦空并继续停等），
# 补日常肯定说法，措辞不在词表不再被丢弃。
_REQUIREMENT_CONFIRM_WORDS: frozenset[str] = frozenset(
    {
        "confirmed",
        "confirm",
        "ok",
        "okay",
        "yes",
        "确认",
        "通过",
        "通过吧",
        "同意",
        "同意了",
        "认可",
        "没问题",
        "没意见",
        "可以",
        "可以吧",
        "可以了",
        "行",
        "行吧",
        "行了",
        "好",
        "好的",
        "就这样",
        "就这样吧",
        "就这版",
        "按这个来",
        "听你的",
        "继续",
        "落盘",
        "放行",
        "放行吧",
    }
)

# 改判答复首尾标点（剥离后判断是否还有实质内容）
_PUNCT_LEADING = "，。,.!！、；;"


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


# [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：剥离改判关键词+标点，判断纯改判 vs 带附言
def _strip_reclassify_keywords(text: str) -> str:
    """剥离改判关键词和首尾标点后的剩余文本；空串=纯改判无附言。"""
    compact = re.sub(r"[\s\u3000]+", "", text.lower())
    for kw in _NON_AI_KEYWORDS + _AI_CORE_KEYWORDS:
        compact = compact.replace(kw, "")
    return compact.lstrip(_PUNCT_LEADING).rstrip(_PUNCT_LEADING)


def classify_requirement_answer(text: str) -> str:
    """纯函数：把需求确认门用户答复归一化为四分类。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做非AI/AI核心关键词包含判定**：去空白（含全角空格）后，
       - 命中任一 ``_NON_AI_KEYWORDS`` -> ``non_ai``（ai_core=false）；
       - 命中任一 ``_AI_CORE_KEYWORDS`` 且前 3 字内无否定前缀 -> ``ai_core``
         （ai_core=true）；
       - ai_core 关键词紧邻否定前缀（如"不是AI核心"）不判 ai_core，落 feedback；
    3. **再做确认精确判定**：归一化后恰好属于 ``_REQUIREMENT_CONFIRM_WORDS`` 才算 confirm；
    4. 其余文本 -> feedback（需求修订意见，分流沿用模型建议，confirmed_requirement 用用户文本）。

    [MA 2026-09-19] S056：空串不在确认词集合里，本函数对空串返回 feedback；
    空答复由节点在调用本函数之前拦下（不当作确认、不当作意见），继续停等下一句。

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
    # [C 2026-09-14 by codebuddy-ds41flash] S041 小块1：确认词集合对齐工单门
    if lowered in _REQUIREMENT_CONFIRM_WORDS:
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
        payload = {
            "node": "requirement_confirm",
            "requirement_name": state.get("requirement_name", ""),
            "raw_requirement": state.get("raw_requirement", ""),
            "info_completeness": state.get("info_completeness"),
            "ai_triage": ai_triage,
        }
        user_input = interrupt(value=payload)
        # [MA 2026-09-19] S056：空答复不当作确认、不当作意见，继续停在本节点等下一句
        while not str(user_input if user_input is not None else "").strip():
            user_input = interrupt(
                value={**payload, "note": "没收到答复，仍在这里等你的决定"}
            )

        # 3. resume 值归一化四态：confirm / non_ai / ai_core / feedback
        original = user_input if isinstance(user_input, str) else str(user_input)
        kind = classify_requirement_answer(original)
        text = original.strip()
        ai_core = _resolve_ai_core(kind, model_suggestion)

        # 4. confirmed_requirement 解析 + refine_pending 标志
        #    [C 2026-09-14 by codebuddy-ds41flash] S041 整合节点接入：
        #    - confirm：confirmed=raw_requirement, pending=False
        #    - non_ai / ai_core 纯改判（remaining 为空）：confirmed=raw_requirement, pending=False
        #    - non_ai / ai_core 改判带附言（remaining 非空）：confirmed=raw_requirement,
        #      pending=True, feedback=text（交整合节点整合后再写回 confirmed_requirement）
        #    - feedback：confirmed=raw_requirement, pending=True, feedback=text
        #    （原 feedback/改判带附言直接用用户文本替换 confirmed_requirement 的行为已废弃——
        #      那会让原需求丢失；新版交给 requirement_refine 节点模型整合。）
        if kind == "confirm":
            confirmed = state.get("raw_requirement", "")
            refine_pending = False
            refine_feedback = ""
        elif kind in ("non_ai", "ai_core"):
            remaining = _strip_reclassify_keywords(text)
            if remaining:
                # 改判带附言：走整合（confirmed 保留原需求，附言交整合节点处理）
                confirmed = state.get("raw_requirement", "")
                refine_pending = True
                refine_feedback = text
            else:
                # 纯改判：保留原需求，不污染
                confirmed = state.get("raw_requirement", "")
                refine_pending = False
                refine_feedback = ""
        else:  # feedback
            confirmed = state.get("raw_requirement", "")
            refine_pending = True
            refine_feedback = text

        # 5. 初始化评测用例（纯确定性模板生成）
        #    [C 2026-09-14 by codebuddy-ds41flash] S041：pending=True 时跳过初始化，
        #    整合节点确认后再初始化（避免用过时的 confirmed_requirement 生成 eval_cases）
        if not refine_pending:
            eval_cases = init_eval_cases({**state, "confirmed_requirement": confirmed})
        else:
            eval_cases = None  # 整合节点确认后初始化

        result = {
            "ai_triage": ai_triage,
            "ai_core": ai_core,
            "confirmed_requirement": confirmed,
            "proceed_decision": True,
            "human_feedback": (state.get("human_feedback") or [])
            + [{"node": "requirement_confirm", "feedback": original, "kind": kind}],
            "current_stage": "探索",
            "requirement_refine_pending": refine_pending,
        }
        if refine_pending:
            result["requirement_refine_feedback"] = refine_feedback
        if eval_cases is not None:
            result["eval_cases"] = eval_cases
        return result

    return requirement_confirm


# [C 2026-09-12 by MA] S033 块2a：确认门接入 ai_triage 分流 + resume 四态协议
