# [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点（requirement_refine HITL）
"""需求修订整合节点：用户在需求确认门提修订意见时，模型把"当前需求 + 修订意见"整合为一版完整新需求草案。

图位置（第 1 段探索阶段，requirement_confirm 与 needs_discovery 之间的条件分支）：
    requirement_confirm(HITL) -> 条件边：
      pending=False（纯确认 / 纯改判）-> needs_discovery（原路径，行为逐字不变）
      pending=True（feedback / 改判带附言）-> requirement_refine（本节点）
    requirement_refine(HITL，调模型整合）-> 条件边三态：
      confirm   -> needs_discovery（整合后需求写回 confirmed_requirement，eval_cases 重初始化）
      reclassify -> needs_discovery（接受整合后需求 + 改判 ai_core，eval_cases 重初始化）
      abandon   -> END（放弃，留档当前草案与意见）
      feedback  -> requirement_refine 自环（带新意见重整合；前 2 版自动，第 3 版升级暂停）

交互协议：
- 正常路径（count < MAX_REQUIREMENT_REFINES=2）：调模型产草案 -> interrupt 展示 ->
  用户 confirm/reclassify/abandon/feedback 四态分流；
- 升级暂停路径（count >= MAX）：不再调模型，展示上一版草案 ->
  只接受 confirm/reclassify/abandon；feedback 按确认处理（防理论无限递归）。

复用 hitl.py 的确认词集合和改判关键词（_REQUIREMENT_CONFIRM_WORDS / _NON_AI_KEYWORDS /
_AI_CORE_KEYWORDS / _matches_non_ai / _matches_ai_core / _strip_reclassify_keywords）；
放弃关键词与 feasibility.py 一致。
"""
from __future__ import annotations

import re

from langgraph.graph import END
from langgraph.types import interrupt

from components.tools.init_eval_cases import init_eval_cases
from kernel.spec import NodeSpec

# 自动整合最多 2 版；第 3 版（计数达 MAX）起进入升级暂停 [C 2026-09-14]
MAX_REQUIREMENT_REFINES = 2

# 复用 hitl.py 的确认词集合和改判关键词——直接 import，避免重复定义 [C 2026-09-14]
from nodes.hitl import (  # noqa: E402 - 延迟导入避免循环（hitl.py 不依赖本模块）
    _AI_CORE_KEYWORDS,
    _NON_AI_KEYWORDS,
    _REQUIREMENT_CONFIRM_WORDS,
    _matches_ai_core,
    _matches_non_ai,
    _strip_reclassify_keywords,
)

# 放弃关键词（与 feasibility.py 一致）
_ABANDON_KEYWORDS: tuple[str, ...] = ("放弃", "不做", "终止", "搁置", "停做")

# 升级暂停说明（额度用尽后不再自动调模型，等真人拍板） [C 2026-09-14]
_REFINE_LIMIT_REASON = (
    "需求整合额度已用尽（限 2 次自动整合），流水线升级暂停、不再自动整合。"
    "请重新拍板：回复「确认」按当前草案进挖需求；回复「非AI/普通轨」或「AI核心」改判分流；"
    "回复「放弃」结束流程。"
)


def classify_refine_answer(text: str) -> str:
    """纯函数：把整合确认门用户答复归一化为四分类。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做放弃关键词包含判定**（去空白含全角空格后）：命中即 abandon；
    3. **再做改判关键词包含判定**：命中 _NON_AI_KEYWORDS 或 _AI_CORE_KEYWORDS
       （后者需前 3 字内无否定前缀）即 reclassify（与确认门 classify 一致）；
    4. **再做确认词精确集合判定**：归一化后恰好属于 _REQUIREMENT_CONFIRM_WORDS 才 confirm；
    5. 其余文本 -> feedback（带新意见重整合，正常路径下自环）。

    Returns:
        ``confirm`` / ``reclassify`` / ``abandon`` / ``feedback``
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    if any(kw in compact for kw in _ABANDON_KEYWORDS):
        return "abandon"
    if _matches_non_ai(compact) or _matches_ai_core(compact):
        return "reclassify"
    if lowered in _REQUIREMENT_CONFIRM_WORDS:
        return "confirm"
    return "feedback"
    # [C 2026-09-14 by codebuddy-ds41flash] 整合确认门答复分类纯函数


def route_after_requirement_confirm(state: dict) -> str:
    """确认门后路由：requirement_refine_pending=True -> requirement_refine，否则 needs_discovery。"""
    if state.get("requirement_refine_pending"):
        return "requirement_refine"
    return "needs_discovery"
    # [C 2026-09-14 by codebuddy-ds41flash] 确认门后条件边路由纯函数


def route_after_requirement_refine(state: dict) -> str:
    """整合节点后路由：abandon->END, feedback->requirement_refine 自环, 其余->needs_discovery。"""
    result = state.get("requirement_refine_result") or {}
    verdict = str(result.get("verdict") or "")
    if verdict == "abandon":
        return END
    if verdict == "feedback":
        return "requirement_refine"
    return "needs_discovery"
    # [C 2026-09-14 by codebuddy-ds41flash] 整合节点条件边三态路由纯函数


def make_requirement_refine(deps):
    """需求修订整合节点工厂：返回签名 (state: dict) -> dict 的节点函数（调模型 + HITL）。"""

    def requirement_refine(state: dict) -> dict:
        count = int(state.get("requirement_refine_count") or 0)
        feedback_log = [
            dict(item)
            for item in (state.get("human_feedback") or [])
            if isinstance(item, dict)
        ]
        req_name = state.get("requirement_name", "")

        def append_log(kind: str, text: str, round_label: str) -> None:
            feedback_log.append(
                {
                    "node": "requirement_refine",
                    "kind": kind,
                    "round": round_label,
                    "feedback": text,
                }
            )

        # ── 升级暂停路径：不再调模型，展示上一版草案，二次答复分流 ──
        if count >= MAX_REQUIREMENT_REFINES:
            last_draft = state.get("requirement_draft", "")
            answer = interrupt(
                {
                    "node": "requirement_refine",
                    "status": "escalated",
                    "reason": _REFINE_LIMIT_REASON,
                    "requirement_name": req_name,
                    "requirement_draft": last_draft,
                    "requirement_refine_count": count,
                }
            )
            text = answer.strip() if isinstance(answer, str) else str(answer).strip()
            kind = classify_refine_answer(text)

            if kind == "abandon":
                append_log("abandon", text, "escalated-abandon")
                return {
                    "requirement_refine_result": {
                        "verdict": "abandon",
                        "user_feedback": text,
                    },
                    "requirement_refine_pending": False,
                    "human_feedback": feedback_log,
                }

            # reclassify：接受上一版草案 + 改判 ai_core
            if kind == "reclassify":
                compact = re.sub(r"[\s\u3000]+", "", text.lower())
                ai_core = not _matches_non_ai(compact)
                eval_cases = init_eval_cases(
                    {**state, "confirmed_requirement": last_draft}
                )
                append_log("reclassify", text, "escalated-reclassify")
                return {
                    "requirement_refine_result": {
                        "verdict": "reclassify",
                        "user_feedback": text,
                    },
                    "confirmed_requirement": last_draft,
                    "ai_core": ai_core,
                    "eval_cases": eval_cases,
                    "requirement_refine_pending": False,
                    "human_feedback": feedback_log,
                }

            # confirm 或 feedback（升级后 feedback 按确认处理，不再调模型重整合）
            append_log("confirm", text, "escalated-confirm")
            eval_cases = init_eval_cases(
                {**state, "confirmed_requirement": last_draft}
            )
            return {
                "requirement_refine_result": {
                    "verdict": "confirm",
                    "user_feedback": text,
                },
                "confirmed_requirement": last_draft,
                "eval_cases": eval_cases,
                "requirement_refine_pending": False,
                "human_feedback": feedback_log,
            }

        # ── 正常路径：调模型整合当前需求 + 修订意见 -> 中断展示草案 -> 四态分流 ──
        prompt = deps.registry.read_prompt("requirement_refine")
        schema = deps.registry.load_schema("requirement_refine")
        spec = NodeSpec(
            name="requirement_refine",
            prompt_template=prompt,
            output_schema=schema,
        )
        draft = deps.runner.run_raw(spec, state)
        refined = str(draft.get("refined_requirement") or "")
        changes = draft.get("change_summary") or []
        new_count = count + 1

        answer = interrupt(
            {
                "node": "requirement_refine",
                "status": "draft",
                "requirement_name": req_name,
                "requirement_draft": refined,
                "change_summary": changes,
                "requirement_refine_count": new_count,
            }
        )
        text = answer.strip() if isinstance(answer, str) else str(answer).strip()
        kind = classify_refine_answer(text)

        if kind == "confirm":
            eval_cases = init_eval_cases(
                {**state, "confirmed_requirement": refined}
            )
            append_log("confirm", text, f"draft-{new_count}-confirm")
            return {
                "requirement_refine_result": {
                    "verdict": "confirm",
                    "user_feedback": text,
                },
                "confirmed_requirement": refined,
                "requirement_draft": refined,
                "eval_cases": eval_cases,
                "requirement_refine_pending": False,
                "requirement_refine_count": new_count,
                "requirement_refine_feedback": "",
                "human_feedback": feedback_log,
            }

        if kind == "reclassify":
            compact = re.sub(r"[\s\u3000]+", "", text.lower())
            ai_core = not _matches_non_ai(compact)
            eval_cases = init_eval_cases(
                {**state, "confirmed_requirement": refined}
            )
            append_log("reclassify", text, f"draft-{new_count}-reclassify")
            return {
                "requirement_refine_result": {
                    "verdict": "reclassify",
                    "user_feedback": text,
                },
                "confirmed_requirement": refined,
                "requirement_draft": refined,
                "ai_core": ai_core,
                "eval_cases": eval_cases,
                "requirement_refine_pending": False,
                "requirement_refine_count": new_count,
                "requirement_refine_feedback": "",
                "human_feedback": feedback_log,
            }

        if kind == "abandon":
            append_log("abandon", text, f"draft-{new_count}-abandon")
            return {
                "requirement_refine_result": {
                    "verdict": "abandon",
                    "user_feedback": text,
                },
                "requirement_refine_count": new_count,
                "requirement_refine_pending": False,
                "human_feedback": feedback_log,
            }

        # feedback：存意见，自环重整合（pending 已为 True，无需再设）
        append_log("feedback", text, f"draft-{new_count}-feedback")
        return {
            "requirement_refine_result": {
                "verdict": "feedback",
                "user_feedback": text,
            },
            "requirement_draft": refined,
            "requirement_refine_feedback": text,
            "requirement_refine_count": new_count,
            "human_feedback": feedback_log,
        }

    return requirement_refine
    # [C 2026-09-14 by codebuddy-ds41flash] 整合节点工厂完成


# [C 2026-09-14 by codebuddy-ds41flash] nodes/refine.py 新增完成
