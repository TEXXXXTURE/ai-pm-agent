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
      feedback  -> requirement_refine 自环（带新意见重整合，不设次数上限）

交互协议：
- 单一路径：调模型产草案 -> interrupt 展示（含「变化体检」draft_progress） ->
  用户 confirm/reclassify/abandon/feedback 四态分流。
- 每次重整合都必须真人回话才会发生，模型不会自己循环，故不设整合次数上限；
  `requirement_refine_count` 只作记录，不用于拦截。

复用 hitl.py 的确认词集合和改判关键词（_REQUIREMENT_CONFIRM_WORDS / _NON_AI_KEYWORDS /
_AI_CORE_KEYWORDS / _matches_non_ai / _matches_ai_core / _strip_reclassify_keywords）；
放弃关键词与 feasibility.py 一致。
"""
from __future__ import annotations

import difflib
import re

from langgraph.graph import END
from langgraph.types import interrupt

from components.tools.init_eval_cases import init_eval_cases
from kernel.spec import NodeSpec

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

# 变化体检阈值（纯提示，不拦流程） [C 2026-09-16 by codebuddy-deepseek-v4.1-flash]
_SIMILARITY_NOTE_THRESHOLD = 0.95  # 草案与上一版相似度达此值 -> 提示"几乎相同"
_SHRINK_RATIO_THRESHOLD = 0.8  # 新版字数 / 上一版字数 <= 此值 -> 提示"少了 N 字"
_FEEDBACK_SIMILARITY_NOTE_THRESHOLD = 0.8  # 与上一轮意见相似度达此值 -> 提示"高度相似"


def audit_draft_progress(
    prev_draft: str,
    new_draft: str,
    prev_feedback: str,
    new_feedback: str,
) -> dict:
    """纯函数：体检"这一版草案相对上一版有没有变化"，产出给人看的提示（零 API、只提示）。

    输入口径（相邻两轮对比）：
    - ``prev_draft``：上一轮展示过的草案（首轮为空串）；``new_draft``：本轮模型新产草案；
    - ``prev_feedback``：上一轮整合所依据的意见；``new_feedback``：本轮整合所依据的意见。

    判定规则（命中即加一条 notes 文案，规则之间不互斥）：
    1. 草案相似度 >= 0.95 -> "几乎相同"；
    2. 上一版非空且新版字数比 <= 0.8（缩水 >= 20%）-> "少了 N 字"；
    3. 意见相似度 >= 0.8 -> "与上一轮提的高度相似"；
    4. 首轮（``prev_draft`` 为空）-> ``notes`` 为空列表，两个相似度字段为 ``None``。

    相似度用标准库 ``difflib.SequenceMatcher`` 计算（不引新依赖）。

    Returns:
        含 ``similarity`` / ``length_prev`` / ``length_new`` / ``length_delta`` /
        ``length_ratio`` / ``feedback_similarity`` / ``notes`` 的字典。
    """
    prev = str(prev_draft or "")
    new = str(new_draft or "")
    length_prev = len(prev)
    length_new = len(new)
    length_delta = length_new - length_prev
    length_ratio = round(length_new / length_prev, 4) if length_prev > 0 else None

    # 首轮：没有上一版草案可比，全部提示为空
    if not prev:
        return {
            "similarity": None,
            "length_prev": length_prev,
            "length_new": length_new,
            "length_delta": length_delta,
            "length_ratio": length_ratio,
            "feedback_similarity": None,
            "notes": [],
        }

    similarity = round(difflib.SequenceMatcher(None, prev, new).ratio(), 4)
    prev_fb = str(prev_feedback or "")
    new_fb = str(new_feedback or "")
    feedback_similarity = (
        round(difflib.SequenceMatcher(None, prev_fb, new_fb).ratio(), 4)
        if prev_fb and new_fb
        else None
    )

    notes: list[str] = []
    if similarity >= _SIMILARITY_NOTE_THRESHOLD:
        notes.append(
            f"这一版草案与上一版几乎相同（相似度 {round(similarity * 100)}%），"
            "这条意见可能已经在草案里体现了"
        )
    if length_ratio is not None and length_ratio <= _SHRINK_RATIO_THRESHOLD:
        notes.append(
            f"这一版比上一版少了 {length_prev - length_new} 字"
            f"（从 {length_prev} 缩到 {length_new}），可能丢了内容，请核对"
        )
    if (
        feedback_similarity is not None
        and feedback_similarity >= _FEEDBACK_SIMILARITY_NOTE_THRESHOLD
    ):
        notes.append(
            f"这条意见与上一轮提的高度相似（{round(feedback_similarity * 100)}%），"
            "上一轮的处理见上一版草案"
        )

    return {
        "similarity": similarity,
        "length_prev": length_prev,
        "length_new": length_new,
        "length_delta": length_delta,
        "length_ratio": length_ratio,
        "feedback_similarity": feedback_similarity,
        "notes": notes,
    }
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S047 草案变化体检纯函数（零 API）


def classify_refine_answer(text: str) -> str:
    """纯函数：把整合确认门用户答复归一化为四分类。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做放弃关键词包含判定**（去空白含全角空格后）：命中即 abandon；
    3. **再做改判关键词包含判定**：命中 _NON_AI_KEYWORDS 或 _AI_CORE_KEYWORDS
       （后者需前 3 字内无否定前缀）即 reclassify（与确认门 classify 一致）；
    4. **再做确认词精确集合判定**：归一化后恰好属于 _REQUIREMENT_CONFIRM_WORDS 才 confirm；
    5. 其余文本 -> feedback（带新意见重整合，正常路径下自环）。

    [MA 2026-09-19] S056：空串不属于确认词集合，本函数对空串返回 feedback；
    空答复由节点在调用本函数之前拦下（不当作确认、不当作意见），继续停等下一句。

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

        # ── 调模型整合当前需求 + 修订意见 -> 中断展示草案 -> 四态分流 ──
        # 不设整合次数上限：每次重整合都必须真人回话才会发生，模型不会自己循环；
        # count 只作记录、不作拦截。 [C 2026-09-16 by codebuddy-deepseek-v4.1-flash]
        prompt = deps.registry.read_prompt("requirement_refine")
        schema = deps.registry.load_schema("requirement_refine")
        spec = NodeSpec(
            name="requirement_refine",
            prompt_template=prompt,
            output_schema=schema,
        )
        # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S047 重整合丢内容修复：
        # prompt 模板的「当前需求」取自 confirmed_requirement，而 feedback 自环分支只写
        # requirement_draft、不写 confirmed_requirement，导致第二轮起整合输入恒为最初需求原文、
        # 上一版草案从未进入输入（真机 thread a320de8e：856 字草案缩到 235 字）。
        # 此处只在节点侧覆盖这一处输入：上一版草案优先，其为空（首轮）时回落到已确认需求。
        prev_draft = str(state.get("requirement_draft") or "")
        base_requirement = prev_draft or str(state.get("confirmed_requirement") or "")
        draft = deps.runner.run_raw(
            spec, {**state, "confirmed_requirement": base_requirement}
        )
        refined = str(draft.get("refined_requirement") or "")
        changes = draft.get("change_summary") or []
        new_count = count + 1

        # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S047 变化体检接线：
        # 本轮意见 = 本轮整合所依据的意见（state 的 requirement_refine_feedback）；
        # 上一轮意见 = 上一轮整合所依据的意见（从 human_feedback 日志按轮次标签取，
        # 日志中 draft-{k}-feedback 记录的是第 k 轮用户答复，即第 k+1 轮的整合输入）。
        # 取不到（首轮 / 第 2 轮）时留空 -> feedback_similarity=None，不产意见重复提示。
        prev_feedback = ""
        target_round = f"draft-{count - 1}-feedback"
        for item in feedback_log:
            if (
                item.get("kind") == "feedback"
                and str(item.get("round") or "") == target_round
            ):
                prev_feedback = str(item.get("feedback") or "")
        draft_progress = audit_draft_progress(
            prev_draft,
            refined,
            prev_feedback,
            str(state.get("requirement_refine_feedback") or ""),
        )

        payload = {
            "node": "requirement_refine",
            "status": "draft",
            "requirement_name": req_name,
            "requirement_draft": refined,
            "change_summary": changes,
            "requirement_refine_count": new_count,
            "draft_progress": draft_progress,
        }
        answer = interrupt(payload)
        text = answer.strip() if isinstance(answer, str) else str(answer).strip()
        # [MA 2026-09-19] S056：空答复不当作确认、不当作意见，继续停在本节点等下一句
        while not text:
            answer = interrupt({**payload, "note": "没收到答复，仍在这里等你的决定"})
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
