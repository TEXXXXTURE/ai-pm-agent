# [C 2026-09-11] 发布计划节点（launch_plan）块1
"""发布计划：模型输出 7 步骨架 + Tier1 扩展 JSON，关键字段非空由 Python 硬判。

图位置（块1 临时形态）：issue_confirm 确认分支 -> launch_plan -> artifact_persist
（块2 会在 launch_plan 与 artifact_persist 之间插入 launch_confirm HITL 门）。

硬判内容（judge_launch_plan，纯函数，返回 (errors, warnings) tuple）：
- errors（关键字段缺失，触发一次带反馈自检重调）：
  tier 为空 / positioning 为空 / workstreams 为空 / timeline 为空 /
  rollback 为空 / go_no_go_checklist 为空 / on_call 为空 / risks 为空 /
  Tier1 时 tier1_extension 为空。
- warnings（不阻断，仅随产物展示）：
  workstreams 超过 15 条 / timeline 无关键路径标注 /
  rollback_trigger 无数字 / go_no_go 项含模糊词（基本/差不多/大概 等）。
- AI 轨专属（仅 ai_core=True 追加，普通轨逐字不变）：
  ai_guardrails 少于 2 条 / threshold|window 无数字 / cohort_rollout 少于 2 批 /
  percent|dwell_time|promotion_criteria 无数字 -> errors；
  无质量类指标 / 无接管率类指标 / cohort 末批非全量 -> warnings。

自检重调：首轮 errors 非空时，把 errors+warnings 拼成中文反馈注入
launch_revision_feedback（仅本轮调用的局部 state，不写回全局 state），重跑一次
（SELF_FIX_MAX=1）；再判一次后无论对错都放行，errors/warnings 随产物醒目展示。
launch_plan 节点不做路由分支（无二元裁决），产出后直连下游。
"""
from __future__ import annotations

import re

from langgraph.types import interrupt

from kernel.spec import NodeSpec

# 结构错误后的最大自检重调次数（首轮错误 -> 最多再调 1 次） [C 2026-09-11]
SELF_FIX_MAX = 1

# 工作流矩阵条数告警阈值（Tier3 不该有这么多线）
TOO_MANY_WORKSTREAMS = 15

# go/no-go 模糊词黑名单：命中即告警（非二元判断） [C 2026-09-11]
_VAGUE_WORDS_RE = re.compile(r"基本|差不多|大概|大致|凑合|可能好|也许|或许")

# [C 2026-09-13 by codebuddy-ds41flash] AI 轨（ai_core=true）专属校验关键词。
# 质量类 / 接管率类各须至少一条命中，否则告警（不阻断）。
_AI_QUALITY_KEYWORDS: tuple[str, ...] = ("准确率", "满意度", "通过率", "正确率")
_AI_HANDOFF_KEYWORDS: tuple[str, ...] = ("接管率", "转人工率", "人工介入率")


def _contains_digit(text: str) -> bool:
    """字符串是否含数字字符（用于校验 rollback_trigger 必须是数字而非心情）。"""
    return bool(re.search(r"\d", str(text or "")))


def judge_launch_plan(plan: dict, ai_core: bool = False) -> tuple[list[str], list[str]]:
    """纯函数：硬判发布计划关键字段非空与 Tier1 扩展必填。

    Args:
        plan: 符合 LaunchPlanSchema 的 dict（测试中也可直接构造最小 dict）。
        ai_core: 是否 AI 核心需求。默认 False——普通轨（含所有不传第二参数的既有
            调用与测试）行为逐字不变；仅当为 True 时额外追加 AI 轨专属校验
            （ai_guardrails 在线 kill 阈值 / cohort_rollout cohort 晋级规则）。

    Returns:
        (errors, warnings)：errors 触发自检重调并在产物中醒目展示，
        warnings 仅展示不阻断。
    """
    errors: list[str] = []
    warnings: list[str] = []

    # [C 2026-09-12 by codebuddy-hy3] 第⑤项修复：plan 为 None 或非 dict 时一律按缺失计入
    # errors，不得抛异常；嵌套 dict/列表内字段为 None 时各取值链路均已用 `or 默认值` 兜底
    if not isinstance(plan, dict):
        plan = {}

    tier = str(plan.get("tier") or "")
    positioning = str(plan.get("positioning") or "")
    workstreams = plan.get("workstreams") or []
    timeline = plan.get("timeline") or []
    rollback = plan.get("rollback") if isinstance(plan.get("rollback"), dict) else {}
    checklist = plan.get("go_no_go_checklist") or []
    on_call = plan.get("on_call") if isinstance(plan.get("on_call"), dict) else {}
    risks = plan.get("risks") or []
    tier1_ext = plan.get("tier1_extension")

    # ── errors：关键字段非空 ──
    if not tier:
        errors.append("tier 为空：必须分层 1/2/3 并在 tier_rationale 给出理由")
    if not positioning.strip():
        errors.append("positioning 为空：定位一句话必须先于一切")
    if not workstreams:
        errors.append("workstreams 为空：工作流矩阵至少 1 行，每条线有且仅有一个具名责任人")
    if not timeline:
        errors.append("timeline 为空：T-minus 倒排时间线至少 1 行，关键路径需标注")
    if not rollback:
        errors.append("rollback 为空：灰度机制与回滚方案必填（回滚触发条件必须是数字）")
    if not checklist:
        errors.append("go_no_go_checklist 为空：go/no-go 二元判断项清单至少 1 项")
    if not on_call:
        errors.append("on_call 为空：Day1-7 值班安排与首次复盘必填")
    if not risks:
        errors.append("risks 为空：Top3 风险至少 1 条（每条带缓解措施与早期预警）")
    if tier == "1" and not tier1_ext:
        errors.append(
            "tier1_extension 为空：Tier1 大发布必填扩展检查"
            "（滩头/ICP/分受众/渠道排序）"
        )

    # ── warnings：不阻断 ──
    if len(workstreams) > TOO_MANY_WORKSTREAMS:
        warnings.append(
            f"工作流矩阵 {len(workstreams)} 条（>{TOO_MANY_WORKSTREAMS}），场面过重；"
            "确认是否与 Tier 匹配（Tier3 不该有这么多线）"
        )
    if timeline and not any(
        str((item.get("is_critical_path")) if isinstance(item, dict) else "").lower()
        in ("true", "1", "yes")
        for item in timeline
    ):
        warnings.append(
            "timeline 无关键路径标注：至少 1 行 is_critical_path=true"
            "（决定发布日能否成立的最长依赖链）"
        )
    if rollback:
        trigger = str(rollback.get("rollback_trigger") or "")
        if trigger and not _contains_digit(trigger):
            warnings.append(
                f"rollback_trigger「{trigger}」未含数字："
                "回滚触发条件必须是数字（如错误率>2%），不是心情"
            )
    for idx, item in enumerate(checklist, start=1):
        if isinstance(item, dict):
            text = str(item.get("item") or "")
            if _VAGUE_WORDS_RE.search(text):
                warnings.append(
                    f"go/no-go 第 {idx} 项「{text}」含模糊词："
                    "检查项必须二元判断（是/否），不允许'基本/差不多/大概'"
                )

    # ── AI 轨专属校验：仅 ai_core is True 生效（普通轨默认 False，逐字零变化）──
    # [C 2026-09-13 by codebuddy-ds41flash] 第 9 段 AI 轨增补：kill 阈值 + cohort 晋级
    if ai_core is True:
        guardrails = plan.get("ai_guardrails")
        if not isinstance(guardrails, list) or len(guardrails) < 2:
            errors.append(
                "AI 核心需求的在线 kill 阈值（ai_guardrails）至少 2 条："
                "质量类（如在线准确率）与人工接管率类必须各有量化阈值"
            )
            guardrail_rows = []
        else:
            guardrail_rows = [it if isinstance(it, dict) else {} for it in guardrails]
            for idx, row in enumerate(guardrail_rows, start=1):
                metric = str(row.get("metric") or f"第 {idx} 条")
                if not _contains_digit(row.get("threshold")):
                    errors.append(
                        f"ai_guardrails 第 {idx} 条「{metric}」的 threshold 未含数字："
                        "触发数值必须可执行（如准确率 90%、接管率 5%）"
                    )
                if not _contains_digit(row.get("window")):
                    errors.append(
                        f"ai_guardrails 第 {idx} 条「{metric}」的 window 未含数字："
                        "统计窗口必须可执行（如 连续 15 分钟）"
                    )

        cohorts = plan.get("cohort_rollout")
        if not isinstance(cohorts, list) or len(cohorts) < 2:
            errors.append(
                "AI 核心需求的 cohort 晋级规则（cohort_rollout）至少 2 批，"
                "每批写清放量比例/观察时长/晋级数值条件"
            )
            cohort_rows = []
        else:
            cohort_rows = [it if isinstance(it, dict) else {} for it in cohorts]
            for idx, row in enumerate(cohort_rows, start=1):
                name = str(row.get("cohort") or f"第 {idx} 批")
                if not _contains_digit(row.get("percent")):
                    errors.append(
                        f"cohort_rollout 第 {idx} 批「{name}」的 percent 未含数字："
                        "放量比例必须可执行（如 5%、100%）"
                    )
                if not _contains_digit(row.get("dwell_time")):
                    errors.append(
                        f"cohort_rollout 第 {idx} 批「{name}」的 dwell_time 未含数字："
                        "观察时长必须可执行（如 48 小时）"
                    )
                if not _contains_digit(row.get("promotion_criteria")):
                    errors.append(
                        f"cohort_rollout 第 {idx} 批「{name}」的 promotion_criteria 未含数字："
                        "晋级条件必须可执行（如 准确率≥92% 且接管率≤3%）"
                    )

        # AI 轨 warnings（不阻断）：质量类/接管率类覆盖 + cohort 末批全量
        guard_metrics = [str(row.get("metric") or "") for row in guardrail_rows]
        if not any(
            kw in m for m in guard_metrics for kw in _AI_QUALITY_KEYWORDS
        ):
            warnings.append(
                "ai_guardrails 无质量类指标：至少一条阈值须盯质量类指标"
                "（准确率/满意度/通过率/正确率）"
            )
        if not any(
            kw in m for m in guard_metrics for kw in _AI_HANDOFF_KEYWORDS
        ):
            warnings.append(
                "ai_guardrails 无接管率类指标：至少一条阈值须盯接管率类指标"
                "（接管率/转人工率/人工介入率）"
            )
        if cohort_rows:
            last = cohort_rows[-1]
            last_cohort = str(last.get("cohort") or "")
            last_percent = str(last.get("percent") or "")
            has_100 = "100" in last_cohort or "100" in last_percent
            is_ga = "GA" in last_cohort or "全量" in last_cohort
            if not (has_100 or is_ga):
                warnings.append("cohort 末批应为全量（100% 或 GA）")

    return errors, warnings
    # [C 2026-09-11] 发布计划关键字段非空硬判纯函数，便于零 API 单测
    # [C 2026-09-13 by codebuddy-ds41flash] 追加 ai_core=True 时的 AI 轨专属硬判


def build_launch_self_fix_feedback(
    errors: list[str], warnings: list[str]
) -> str:
    """把首轮硬判结果拼成中文可行动反馈，供重调时注入 launch_revision_feedback。"""
    lines: list[str] = [
        "【发布计划自检未通过】上一轮输出存在以下问题，请逐条修复后，"
        "重新输出完整的发布计划 JSON（不是只输出改动片段）。",
    ]
    if errors:
        lines.append("")
        lines.append(f"一、必须修复的字段缺失（{len(errors)} 条）：")
        for idx, msg in enumerate(errors, start=1):
            lines.append(f"{idx}. {msg}")
    if warnings:
        lines.append("")
        lines.append(f"二、建议一并处理的警告（{len(warnings)} 条，不强制）：")
        for idx, msg in enumerate(warnings, start=1):
            lines.append(f"{idx}. {msg}")
    lines.append("")
    lines.append("未被点名要求修改的部分保持稳定，不要借机扩大范围或重写无关章节。")
    return "\n".join(lines).strip()


def make_launch_plan(deps):
    """发布计划节点工厂：返回签名 (state: dict) -> dict 的节点函数。

    launch_plan 节点不做路由分支（无二元裁决）；产出后直连下游（块1 临时
    直连 artifact_persist，块2 会插 launch_confirm HITL 门）。
    """

    def launch_plan(state: dict) -> dict:
        prompt = deps.registry.read_prompt("launch_plan")
        schema = deps.registry.load_schema("launch_plan")
        spec = NodeSpec(
            name="launch_plan",
            prompt_template=prompt,
            output_schema=schema,
        )

        # 1. 首轮模型产出（runner 内部已含 schema 校验重试，关键字段非空在此判）
        # [C 2026-09-13 by codebuddy-ds41flash] 第 9 段：AI 核心需求追加 kill 阈值/cohort 校验
        is_ai_core = state.get("ai_core") is True
        result = deps.runner.run_raw(spec, state)
        errors, warnings = judge_launch_plan(result, ai_core=is_ai_core)
        first_had_errors = bool(errors)

        # 2. 关键字段缺失 -> 带中文反馈自检重调（最多 SELF_FIX_MAX 次）。
        #    launch_revision_feedback 只注入本轮局部 state，不写回全局 state。 [C 2026-09-11]
        if first_had_errors:
            for _ in range(SELF_FIX_MAX):
                feedback = build_launch_self_fix_feedback(errors, warnings)
                local_state = {**state, "launch_revision_feedback": feedback}
                result = deps.runner.run_raw(spec, local_state)
                errors, warnings = judge_launch_plan(result, ai_core=is_ai_core)

        # 3. 终判结果随产物落盘；两轮仍错也不抛异常，errors/warnings 醒目交人工
        plan = {
            **result,
            "shape_errors": errors,
            "shape_warnings": warnings,
            "self_fixed": first_had_errors,
        }
        return {
            "launch_plan": plan,
            "launch_plan_errors": errors,
            "launch_plan_warnings": warnings,
            # [C 2026-09-11] 块2：消费即清零。重调跑完后把意见字段写空，
            # 确认门条件边才不会把已消化的意见再次路由回 launch_plan
            # （同构 issue_splitting 返回时清零 issue_revision_feedback 的模式；
            #   不改 judge/自检重调逻辑，仅补一个清零字段）
            "launch_revision_feedback": "",
        }
        # [C 2026-09-11] self_fixed=首轮是否曾被字段判错（含重调后修好/未修好两种情形）

    return launch_plan


# [C 2026-09-11] 块2：发布计划确认门（launch_confirm，HITL，不调模型）


# 回工单关键词（统一小写匹配，命中即回 issue_splitting 重拆；优先级高于确认精确匹配）
# 语义：用户认为问题出在工单拆解层（而非发布计划层），要求回 issue_splitting 重拆工单
_REDO_ISSUES_KEYWORDS: tuple[str, ...] = (
    "回工单",
    "回到工单",
    "重拆工单",
    "重新拆单",
    "重拆单",
    "回拆单",
    "回工单拆",
    "重拆",
)

# 确认精确集合：归一化（strip + lower）后恰好属于其中才算确认。
# 空串=确认（与 requirement_confirm / issue_confirm 空答复放行一致）；
# "可以，但要改"不是精确匹配，不判确认。
# 在 issue_confirm 确认词基础上追加"发布""发布吧"（发布计划语境的天然确认语）。
_LAUNCH_CONFIRM_WORDS: frozenset[str] = frozenset(
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
        "没问题",
        "可以",
        "行",
        "就这样",
        "就这版",
        "落盘",
        "放行",
        "同意发布",
        "发布",
        "发布吧",
        "可以发布",
    }
)

# 发布计划自动重调保险丝（初版 + 2 次重调 = 共 3 版；
# 计数达到该值后仍有意见 -> 升级暂停，额外重调只能由人在升级中断里主动发起）
MAX_LAUNCH_REVISIONS = 2

# [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级暂停后继续喂意见的硬深度上限。
# 升级后再给意见最多 1 轮；超过则保持 escalated 暂停、不再自动重调，防理论无限递归。
MAX_ESCALATION_DEPTH = 1

# [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：已达升级深度硬上限的暂停说明。
_ESCALATION_LIMIT_REASON = (
    "已达人工介入上限（升级暂停后再给意见最多 1 轮），流水线保持升级暂停、不再自动重调；"
    "请与 Pi 线下核实后重新发起流水线，或回复「确认」按当前版落盘。"
)

# 回工单额度用尽后仍要求回工单的升级暂停说明 [C 2026-09-11]
_REDO_ESCALATION_REASON = (
    "已回工单重拆 1 次仍无法产出满意的发布计划，流水线升级暂停，不再自动空转。"
    "可与 Pi 重新讨论工单拆解后重新发起流水线；也可现在回复「确认」按当前版落盘，"
    "或给出新的具体决策意见再调一轮发布计划。"
)

# 第 3 版仍提修改意见的升级暂停说明（三个选项） [C 2026-09-11]
_REVISION_ESCALATION_REASON = (
    "发布计划已出到第 3 版（初版 + 2 轮重调），你仍有修改意见，"
    "流水线升级暂停，不自动空转。请三选一："
    "① 带着新决策给出具体意见，由你主动发起再调一轮；"
    "② 回复「回工单」回 issue_splitting 重拆（全程限 1 次）；"
    "③ 回复「确认」按当前版落盘。"
)

# 回工单关键词命中位置前 2~3 字内出现这些否定语时，视为"不想回工单"，落 feedback。
# 单字"不/别/勿"兜底，双字词优先在 endswith 判定中自然命中。 [C 2026-09-11]
_REDO_ISSUES_NEGATIONS: tuple[str, ...] = (
    "不用",
    "不要",
    "不必",
    "不会",
    "不想",
    "不需要",
    "无需",  # [C 2026-09-12 by pi-deepseek-flash] 第③项修复：补齐否定词（「无需回工单」）
    "别",
    "勿",
    "不",
)


def _has_negative_prefix(text: str, idx: int) -> bool:
    """关键词命中位置前 3 个字符内是否紧邻否定语（不/别/勿/不用/不要/不必 等）。"""
    window = text[max(0, idx - 3):idx]
    return any(window.endswith(neg) for neg in _REDO_ISSUES_NEGATIONS)


def _matches_redo_issues(compact: str) -> bool:
    """去空白文本上任一回工单关键词命中、且命中位置不紧邻否定前缀时返回 True。"""
    for keyword in _REDO_ISSUES_KEYWORDS:
        start = 0
        while True:
            idx = compact.find(keyword, start)
            if idx < 0:
                break
            if not _has_negative_prefix(compact, idx):
                return True
            start = idx + 1  # 同一关键词可能多次出现，继续找下一处
    return False


def classify_launch_answer(text: str) -> str:
    """纯函数：把发布计划确认门用户答复归一化三分类为 confirm / redo_issues / feedback。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做回工单包含判定**：先去除词内空白（"回 工单""重拆 工单" 也算回工单），
       含任一 ``_REDO_ISSUES_KEYWORDS`` 且命中位置前 3 字内无否定语
       （不/别/勿/不用/不要/不必/不需要 等，如"不用回工单，直接改计划""不要重拆工单"）
       即 redo_issues（故"回工单重拆，确认"仍判回工单，不被确认词截胡）；
    3. **再做确认精确集合判定**：归一化后恰好属于 ``_LAUNCH_CONFIRM_WORDS`` 才 confirm，
       空串=确认（与 requirement_confirm / issue_confirm 空答复放行一致）；
       "可以，但要改"不是精确匹配，落 feedback；
    4. 其余一律 feedback（打回重调 launch_plan）。
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    # [C 2026-09-11] 包含判定在去空白（含全角空格）文本上做，并排除紧邻否定前缀
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    if _matches_redo_issues(compact):
        return "redo_issues"
    if lowered in _LAUNCH_CONFIRM_WORDS:
        return "confirm"
    return "feedback"
    # [C 2026-09-11] 发布计划答复分类纯函数，确认门节点与零 API 单测共用


def route_after_launch_confirm(state: dict) -> str:
    """条件边路由：不新增 decision 字段，全凭既有状态推断下一步。

    - launch_plan 为空且 issue_revision_feedback 非空 -> ``issue_splitting``
      （确认门发起回工单：plan 已清空、回工单意见待 issue_splitting 消费）；
    - launch_revision_feedback 非空 -> ``launch_plan``（带人工意见重调）；
    - 其余 -> ``artifact_persist``（确认落盘；确认分支不写意见字段，plan 原样保留）。
    """
    plan = state.get("launch_plan") or {}
    issue_revision_feedback = str(state.get("issue_revision_feedback") or "")
    launch_revision_feedback = str(state.get("launch_revision_feedback") or "")
    if not plan and issue_revision_feedback:
        return "issue_splitting"
    if launch_revision_feedback:
        return "launch_plan"
    return "artifact_persist"
    # [C 2026-09-11] 发布计划确认门三分支条件边路由纯函数


def _normalize_answer(answer: object) -> tuple[str, str]:
    """resume 值归一化：返回 (分类, strip 后原文)；None/非字符串安全转空串。"""
    if isinstance(answer, str):
        text = answer.strip()
    elif answer is None:
        text = ""
    else:
        text = str(answer).strip()
    return classify_launch_answer(text), text


def _build_issue_redo_feedback(user_text: str) -> str:
    """拼确认门发起的回工单意见：含用户原话，说明重拆后会自动重回发布计划门。

    注入 issue_splitting 节点的 issue_revision_feedback 字段（与 issue_confirm 的
    "回PRD" 走 prd_rewrite_feedback 同构；issue_splitting 返回时消费即清零）。
    """
    return (
        "【发布计划阶段回工单意见】\n"
        "上一版发布计划已生成，但在发布计划确认环节被打回，"
        "暴露出工单拆解层面需要重新讨论的问题。用户原话如下：\n"
        f"「{user_text}」\n"
        "请针对上述问题重新输出完整的工单拆解方案（不是只输出改动片段）。"
        "重拆后会自动重新生成发布计划，并再次回到发布计划确认门请用户确认。"
    )


def _prior_launch_feedbacks(state: dict) -> list[dict]:
    """从 human_feedback 摘出此前在发布计划确认门留下的答复，供升级暂停载荷 recap。"""
    summary: list[dict] = []
    for item in state.get("human_feedback") or []:
        if isinstance(item, dict) and item.get("node") == "launch_confirm":
            summary.append(
                {
                    "kind": item.get("kind", ""),
                    "round": item.get("round", ""),
                    "feedback": item.get("feedback", ""),
                }
            )
    return summary


def make_launch_confirm(deps):  # noqa: ARG001 - 工厂签名与其他节点保持一致，本节点不调模型/不取依赖
    """发布计划确认门节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    交互协议（首次中断 status="draft"）：
    - confirm -> 只追加 human_feedback，plan 原样保留 -> 条件边落盘 launch_plan.md；
    - feedback（前 2 版）-> launch_revision_count+1、写 launch_revision_feedback
      -> 条件边回 launch_plan 重调 -> 重回本确认门；
    - feedback（第 3 版起）-> 先 interrupt 升级暂停（status="escalated"），
      二次答复：确认落盘 / 回工单走重拆判定 / 带新决策的具体意见再调一轮；
    - redo_issues（redo=0）-> 清空 launch_plan、redo 置 1、修订计数归零、
      写 issue_revision_feedback（注入 issue_splitting）-> 条件边回 issue_splitting；
      redo_issues（redo>=1）-> 升级暂停，二次答复不再回工单
      （再次要求回工单按"仍需回工单："前缀的发布计划意见处理）。
    - [C 2026-09-12 by pi-deepseek-flash] 第⑥项：升级深度硬上限（最多 1 轮），
      超限保持 escalated 暂停、不再自动重调，防理论无限递归。
    """

    def launch_confirm(state: dict) -> dict:
        plan = state.get("launch_plan") or {}
        requirement_name = state.get("requirement_name", "")
        feedback_log = [
            dict(item)
            for item in (state.get("human_feedback") or [])
            if isinstance(item, dict)
        ]
        # [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：入口读已批准的升级后重调轮数，
        # 供本次是否触达硬上限判断（升级后再给意见最多 MAX_ESCALATION_DEPTH 轮）
        escalation_depth = int(state.get("launch_escalation_depth") or 0)

        def append_log(kind: str, text: str, round_label: str) -> None:
            feedback_log.append(
                {
                    "node": "launch_confirm",
                    "kind": kind,
                    "round": round_label,
                    "feedback": text,
                }
            )

        def confirm_update(kind: str, text: str, round_label: str) -> dict:
            # 确认：仅留痕，不写任何意见/计数字段 -> plan 原样保留 -> 路由落盘
            append_log(kind, text, round_label)
            return {"human_feedback": feedback_log}

        def feedback_update(text: str, count: int, depth: int | None = None) -> dict:
            # 打回重调：计数 +1，意见按固定格式包装后供 launch_plan prompt 注入
            new_count = count + 1
            update = {
                "launch_revision_count": new_count,
                "launch_revision_feedback": (
                    f"【第{new_count}轮发布计划修改意见】{text}\n"
                    "请逐条针对性调整发布计划，未要求改的部分保持稳定"
                ),
                "human_feedback": feedback_log,
            }
            if depth is not None:
                # [C 2026-09-12 by pi-deepseek-flash] 第⑥项：记录已批准的升级后重调轮数
                update["launch_escalation_depth"] = depth
            return update

        def escalation_interrupt(reason: str):
            """升级暂停：抛出第二个 interrupt 请人主动决策（不自动空转）。"""
            # [C 2026-09-16] R11：载荷携带就绪度打分
            readiness = state.get("readiness_assessment") or {}
            return interrupt(
                {
                    "node": "launch_confirm",
                    "status": "escalated",
                    "reason": reason,
                    "requirement_name": requirement_name,
                    "launch_plan": plan,
                    "readiness_assessment": readiness,
                    "prior_feedbacks": _prior_launch_feedbacks(state),
                }
            )

        def escalation_limit_stop(round_label: str) -> dict:
            """已达人工介入上限：暂停循环，反复抛同一 escalated 中断等真人答复。

            只有「确认」才跳出循环、走 confirm_update 按当前版落盘；
            非确认答复（feedback / redo_issues 一律）不写任何意见、计数、深度字段，
            恰好留痕一条后继续抛中断等下一轮真人输入。
            人工驱动的反复暂停不是空转：空转指无人值守自动调模型重调，
            本循环每轮都在等真人输入、不调模型。

            [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：硬深度上限落点。
            [C 2026-09-12 by pi-deepseek-flash-r2] 第⑥项返工：非确认答复由
            「返回留痕 dict」改为「继续暂停」，杜绝被条件边当确认而静默落盘。
            """
            seq = 0
            while True:
                seq += 1
                answer = escalation_interrupt(_ESCALATION_LIMIT_REASON)
                kind2, text2 = _normalize_answer(answer)
                # round 标签带序号区分：首轮沿用原标签，其后追加 -2/-3…
                label = round_label if seq == 1 else f"{round_label}-{seq}"
                if kind2 == "confirm":
                    # 仅确认跳出循环落盘：confirm_update 恰好为该答复留痕一条
                    return confirm_update(kind2, text2, f"{label}-confirm")
                # 非确认答复：不写任何意见/计数/深度字段，仅留痕一条后继续暂停
                append_log(kind2, text2, label)

        def ask_then_route(
            text: str, count: int, round_label: str, depth: int = 0
        ) -> dict:
            """第 3 版仍有意见：升级暂停，按二次答复分流。"""
            if depth >= MAX_ESCALATION_DEPTH:
                # [C 2026-09-12 by pi-deepseek-flash] 第⑥项：超限保持 escalated，不自动重调
                return escalation_limit_stop(f"{round_label}-escalation-limit")
            second_answer = escalation_interrupt(_REVISION_ESCALATION_REASON)
            kind2, text2 = _normalize_answer(second_answer)
            if kind2 == "confirm":
                return confirm_update(kind2, text2, "escalation-confirm")
            if kind2 == "redo_issues":
                # 二次答复改选回工单：交回工单判定（redo=0 正常回工单 / redo=1 再升级）；
                # 留痕由 handle_redo_issues 按最终动作统一记录，避免重复
                return handle_redo_issues(text2, depth + 1)
            # 人带来新决策的具体意见：主动发起再调一轮（计数照常 +1，升级深度 +1）
            append_log(kind2, text2, "escalation-feedback")
            return feedback_update(text2, count, depth + 1)

        def handle_redo_issues(text: str, depth: int = 0) -> dict:
            redo = int(state.get("launch_issue_redo_count") or 0)
            if redo >= 1:
                if depth >= MAX_ESCALATION_DEPTH:
                    # [C 2026-09-12 by pi-deepseek-flash] 第⑥项：超限保持 escalated，不再递归
                    return escalation_limit_stop("redo-escalation-limit")
                # 回工单额度已用尽：升级暂停。二次答复只有确认/带意见再调，不再回工单。
                second_answer = escalation_interrupt(_REDO_ESCALATION_REASON)
                kind2, text2 = _normalize_answer(second_answer)
                if kind2 == "confirm":
                    return confirm_update(kind2, text2, "redo-escalation-confirm")
                count = int(state.get("launch_revision_count") or 0)
                if kind2 == "redo_issues":
                    # 仍坚持回工单：不再次回 issue_splitting，转为带"仍需回工单："前缀的发布计划意见
                    text2 = f"仍需回工单：{text2}"
                append_log("feedback", text2, "redo-escalation-feedback")
                if count >= MAX_LAUNCH_REVISIONS:
                    # 同时触达 3 版保险丝：再给一次升级选择，不自动空转
                    return ask_then_route(
                        text2, count, "redo-escalation-feedback", depth + 1
                    )
                return feedback_update(text2, count, depth + 1)

            # redo=0：发起全程唯一一次回工单——清空发布计划、回工单计数置 1、
            # 发布计划修订计数归零、写 issue_revision_feedback 注入 issue_splitting。
            # issue_revision_feedback 由 issue_splitting 返回时消费即清零。
            append_log("redo_issues", text, "redo-1")
            return {
                "launch_plan": {},
                "launch_issue_redo_count": 1,
                "launch_revision_count": 0,
                "launch_revision_feedback": "",
                "issue_revision_feedback": _build_issue_redo_feedback(text),
                # [C 2026-09-12 by pi-deepseek-flash] 第⑥项：回工单后重新计升级深度
                "launch_escalation_depth": 0,
                "human_feedback": feedback_log,
            }

        # ── 首次中断：请用户审阅发布计划草案 ──
        # [C 2026-09-16] R11：载荷携带就绪度打分（launch_plan 之后由 readiness_assessment 节点产出）
        readiness = state.get("readiness_assessment") or {}
        first_answer = interrupt(
            {
                "node": "launch_confirm",
                "status": "draft",
                "requirement_name": requirement_name,
                "launch_plan": plan,
                "readiness_assessment": readiness,
            }
        )
        kind, text = _normalize_answer(first_answer)

        if kind == "confirm":
            return confirm_update(kind, text, "draft-confirm")

        if kind == "redo_issues":
            # [C 2026-09-11] 不在 draft 分支留痕：redo=0 由 handle_redo_issues
            # 统一记 redo-1；redo>=1 进入升级暂停，留痕按二次答复的最终动作记录，
            # 与 ask_then_route 改选回工单路径保持"恰好一次"约定
            return handle_redo_issues(text, escalation_depth)

        # feedback：2 轮保险丝内直接重调；第 3 版起先升级暂停
        count = int(state.get("launch_revision_count") or 0)
        append_log(kind, text, f"draft-feedback-{count + 1}")
        if count >= MAX_LAUNCH_REVISIONS:
            return ask_then_route(
                text, count, f"draft-feedback-{count + 1}", escalation_depth
            )
        return feedback_update(text, count)

    return launch_confirm
    # [C 2026-09-11] 块2 发布计划确认门：分类协议/2 轮保险丝/回工单限 1 次/双升级暂停


# [C 2026-09-11] nodes/launch_plan.py 块2（launch_confirm 确认门）新增完成
