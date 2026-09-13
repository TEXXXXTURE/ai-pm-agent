# [C 2026-09-12 by codebuddy-ds41flash] 验证AI可行性节点（feasibility_check + feasibility_confirm HITL）
"""验证AI可行性：流水线生成可行性报告（含探针方案）-> 人工执行探针后在确认门录入结论。

图位置（第 2 段，仅 AI 核心需求经过）：
    requirement_confirm -> needs_discovery ->（ai_core=True）feasibility_check -> feasibility_confirm(HITL)
    -> 条件边四态：
        pass        -> prd_generation（进 ai-native PRD）
        reclassify  -> prd_generation（ai_core 已改为 False，prd_generation 自动选普通模板）
        reshape     -> requirement_confirm（回第 1 段调整范围后重过判定；全程限 1 次）
        abandon     -> END（放弃，产出可行性结论留档）
普通需求（ai_core=False）不经过本模块，挖完需求后 needs_discovery 直接进 prd_generation。

feasibility_check（make_feasibility_check）：
- 调模型生成可行性报告并写入 state["feasibility_report"]；**不自动跑探针**，只产方案。

feasibility_confirm（make_feasibility_confirm，HITL，不调模型）：
- 展示可行性报告，interrupt 等用户录入探针实测结论；
- resume 四态分类（分类纯函数 classify_feasibility_answer，参照 hitl.py/issue_confirm 的写法）：
  "通过" -> pass；"改判普通"/"普通轨" -> reclassify（改 ai_core=False）；
  "重塑"/"调整范围" -> reshape（限 1 次，第 2 次自动升级暂停）；"放弃"/"不做" -> abandon；
  其他文本 -> 作为补充意见，默认按 pass 处理；
- 写入 state["feasibility_confirm"] = {verdict, user_feedback}。

路由（route_after_feasibility_confirm）：纯函数，便于零 API 单测。
"""
from __future__ import annotations

import re

from langgraph.graph import END
from langgraph.types import interrupt

from kernel.spec import NodeSpec

# 重塑额度：全程限 1 次；计数达到该值后再要求重塑 -> 先升级暂停，由人重新拍板 [C 2026-09-12]
MAX_FEASIBILITY_RESHAPE = 1

# 通过精确集合：归一化（strip + lower）后恰好属于其中才算 pass。
# 空串=通过（与 requirement_confirm / issue_confirm / launch_confirm 空答复放行一致）。
_PASS_WORDS: frozenset[str] = frozenset(
    {
        "",
        "confirmed",
        "confirm",
        "ok",
        "okay",
        "yes",
        "确认",
        "通过",
        "同意",
        "可行",
        "没问题",
        "可以",
        "放行",
        "继续",
        "就这样",
    }
)

# 改判普通轨关键词（命中即 reclassify，改 ai_core=False）
_RECLASSIFY_KEYWORDS: tuple[str, ...] = (
    "改判普通",
    "普通轨",
    "非ai",
    "改判非ai",
    "转普通",
    "走普通",
)

# 重塑关键词（命中即 reshape，回第 1 段调整范围；限 1 次）
_RESHAPE_KEYWORDS: tuple[str, ...] = (
    "重塑",
    "调整范围",
    "缩小范围",
    "收窄范围",
    "改范围",
)

# 放弃关键词（命中即 abandon，流程结束）
_ABANDON_KEYWORDS: tuple[str, ...] = (
    "放弃",
    "不做",
    "终止",
    "搁置",
    "停做",
)

# 重塑额度用尽后的升级暂停说明 [C 2026-09-12]
_RESHAPE_LIMIT_REASON = (
    "重塑额度已用尽（全程限 1 次），流水线升级暂停、不再自动回第 1 段改范围。"
    "请重新拍板：回复「通过」进 PRD；回复「改判普通」转普通轨；回复「放弃」结束流程。"
)


def classify_feasibility_answer(text: str) -> str:
    """纯函数：把确认AI可行性门的用户答复归一化分类为 pass / reclassify / reshape / abandon / feedback。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做四态关键词包含判定**（去空白含全角空格后）：放弃 > 重塑 > 改判普通；
       命中即返回对应态（更"重"的态优先，避免"放弃重塑"被误判为重塑）；
    3. **再做通过精确集合判定**：归一化后恰好属于 ``_PASS_WORDS`` 才 pass，
       空串=通过（与其他确认门空答复放行一致）；
    4. 其余一律 feedback（补充意见，节点内默认按 pass 处理并留痕）。

    Returns:
        ``pass`` / ``reclassify`` / ``reshape`` / ``abandon`` / ``feedback``
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    if any(kw in compact for kw in _ABANDON_KEYWORDS):
        return "abandon"
    if any(kw in compact for kw in _RESHAPE_KEYWORDS):
        return "reshape"
    if any(kw in compact for kw in _RECLASSIFY_KEYWORDS):
        return "reclassify"
    if lowered in _PASS_WORDS:
        return "pass"
    return "feedback"
    # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门答复分类纯函数


def route_after_feasibility_confirm(state: dict) -> str:
    """确认AI可行性门后的条件边路由：按 feasibility_confirm.verdict 四态映射。

    - pass -> ``prd_generation``
    - reclassify -> ``prd_generation``（ai_core 已改为 False，prd_generation 自动选普通模板）
    - reshape -> ``requirement_confirm``（回第 1 段调整范围后重过判定）
    - abandon -> ``END``
    - 其余/缺失 -> ``prd_generation``（缺 verdict 时保守放行进 PRD，避免卡死）
    """
    confirm = state.get("feasibility_confirm") or {}
    verdict = str(confirm.get("verdict") or "")
    if verdict == "reshape":
        return "requirement_confirm"
    if verdict == "abandon":
        return END
    return "prd_generation"
    # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门四态条件边路由纯函数


def _normalize_answer(answer: object) -> tuple[str, str]:
    """resume 值归一化：返回 (分类, strip 后原文)；None/非字符串安全转空串。"""
    if isinstance(answer, str):
        text = answer.strip()
    elif answer is None:
        text = ""
    else:
        text = str(answer).strip()
    return classify_feasibility_answer(text), text


def _prior_feasibility_feedbacks(state: dict) -> list[dict]:
    """从 human_feedback 摘出此前在确认AI可行性门留下的答复，供升级暂停载荷 recap。"""
    summary: list[dict] = []
    for item in state.get("human_feedback") or []:
        if isinstance(item, dict) and item.get("node") == "feasibility_confirm":
            summary.append(
                {
                    "kind": item.get("kind", ""),
                    "round": item.get("round", ""),
                    "feedback": item.get("feedback", ""),
                }
            )
    return summary


def make_feasibility_check(deps):
    """可行性报告节点工厂：返回签名 (state: dict) -> dict 的节点函数。

    调模型生成可行性报告（三色表/探针方案/风险表/成本区间/初步结论），写入
    state["feasibility_report"]。**不跑探针**——探针只产方案，由人工执行实测后在
    feasibility_confirm 门录入结论。
    """

    def feasibility_check(state: dict) -> dict:
        prompt = deps.registry.read_prompt("feasibility_check")
        schema = deps.registry.load_schema("feasibility")
        spec = NodeSpec(
            name="feasibility_check",
            prompt_template=prompt,
            output_schema=schema,
        )
        report = deps.runner.run_raw(spec, state)
        # 不跑探针：只把方案（含 probe_plan）落 state，等人工执行后回确认门录结论
        return {"feasibility_report": report}
        # [C 2026-09-12 by codebuddy-ds41flash] 只产报告不跑探针

    return feasibility_check


def make_feasibility_confirm(deps):  # noqa: ARG001 - 工厂签名与其他节点保持一致，本节点不调模型/不取依赖
    """确认AI可行性门节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    交互协议（首次中断 status="draft"）：
    - pass（含自由文本补充意见，默认按 pass）-> 只追加 human_feedback、写 verdict=pass
      -> 条件边去 prd_generation；
    - reclassify -> 写 verdict=reclassify、ai_core=False -> 条件边去 prd_generation；
    - reshape（feasibility_reshape_count=0）-> 计数 +1、写 verdict=reshape
      -> 条件边回 requirement_confirm 调整范围（限 1 次）；
    - reshape（计数已达上限）-> 先 interrupt 升级暂停（status="escalated"），
      二次答复按 通过/改判普通/放弃 分流；再次要求重塑则按通过处理（不再回第 1 段）；
    - abandon -> 写 verdict=abandon -> 条件边到 END，流程结束。
    """

    def feasibility_confirm(state: dict) -> dict:
        report = state.get("feasibility_report") or {}
        requirement_name = state.get("requirement_name", "")
        reshape_count = int(state.get("feasibility_reshape_count") or 0)
        feedback_log = [
            dict(item)
            for item in (state.get("human_feedback") or [])
            if isinstance(item, dict)
        ]

        def append_log(kind: str, text: str, round_label: str) -> None:
            feedback_log.append(
                {
                    "node": "feasibility_confirm",
                    "kind": kind,
                    "round": round_label,
                    "feedback": text,
                }
            )

        def decision_update(
            verdict: str, text: str, round_label: str, extra: dict | None = None
        ) -> dict:
            # 记录 verdict（pass/reclassify/reshape/abandon），留痕一条后再叠加额外字段
            append_log(verdict, text, round_label)
            update = {
                "feasibility_confirm": {"verdict": verdict, "user_feedback": text},
                "human_feedback": feedback_log,
            }
            if extra:
                update.update(extra)
            return update

        def escalation_stop(first_text: str) -> dict:
            """重塑额度用尽：升级暂停，按二次答复分流。

            二次答复不再回 requirement_confirm；若仍要求重塑，按通过处理并留痕说明，
            防理论无限递归。人工驱动的暂停不是空转：每轮都在等真人输入、不调模型。
            """
            seq = 0
            while True:
                seq += 1
                answer = interrupt(
                    {
                        "node": "feasibility_confirm",
                        "status": "escalated",
                        "reason": _RESHAPE_LIMIT_REASON,
                        "requirement_name": requirement_name,
                        "feasibility_report": report,
                        "feasibility_reshape_count": reshape_count,
                        "prior_feedbacks": _prior_feasibility_feedbacks(state),
                    }
                )
                kind2, text2 = _normalize_answer(answer)
                label = "escalated" if seq == 1 else f"escalated-{seq}"
                if kind2 == "reclassify":
                    return decision_update(
                        "reclassify", text2, f"{label}-reclassify", {"ai_core": False}
                    )
                if kind2 == "abandon":
                    return decision_update("abandon", text2, f"{label}-abandon")
                if kind2 == "reshape":
                    # 仍要求重塑：额度已用尽，改判为通过（不再回第 1 段）
                    note = f"{text2}；重塑额度已用尽，按通过处理".lstrip("；")
                    return decision_update("pass", note, f"{label}-reshape-limit")
                # pass / feedback：按通过处理
                return decision_update("pass", text2, f"{label}-pass")

        # ── 首次中断：请用户审阅可行性报告并录入探针实测结论 ──
        first_answer = interrupt(
            {
                "node": "feasibility_confirm",
                "status": "draft",
                "requirement_name": requirement_name,
                "feasibility_report": report,
                "feasibility_reshape_count": reshape_count,
            }
        )
        kind, text = _normalize_answer(first_answer)

        if kind == "reshape":
            if reshape_count >= MAX_FEASIBILITY_RESHAPE:
                # 第 2 次要求重塑：自动升级暂停
                return escalation_stop(text)
            # 首次重塑：计数 +1，回 requirement_confirm 调整范围
            append_log("reshape", text, "draft-reshape")
            return {
                "feasibility_confirm": {"verdict": "reshape", "user_feedback": text},
                "feasibility_reshape_count": reshape_count + 1,
                "human_feedback": feedback_log,
            }

        if kind == "reclassify":
            return decision_update(
                "reclassify", text, "draft-reclassify", {"ai_core": False}
            )

        if kind == "abandon":
            return decision_update("abandon", text, "draft-abandon")

        # pass / feedback：自由文本作为补充意见，默认按通过处理
        return decision_update("pass", text, "draft-pass")

    return feasibility_confirm
    # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门：四态分类/重塑限 1 次/升级暂停


# [C 2026-09-12 by codebuddy-ds41flash] nodes/feasibility.py 新增完成
