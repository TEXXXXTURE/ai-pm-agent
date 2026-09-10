# [C 2026-09-11] 拆研发工单节点（issue_splitting）
# [C 2026-09-11] 块2：追加工单确认门 issue_confirm（HITL）+ 三分支条件边路由
"""研发工单拆解：模型输出纵切工单方案 JSON，**跨工单结构正确性由 Python 硬判**。

图位置（块2 最终形态）：prd_review 通过类 -> issue_splitting -> issue_confirm（HITL）
-> 条件边三分支：
- 确认 -> artifact_persist（落盘 prd/insights/review/issues 四份）；
- 提修改意见（前 2 版）-> 回 issue_splitting 重拆，再回确认门；
- "回PRD"（全程限 1 次）-> prd_generation 回炉重写 -> prd_review 复审
  -> issue_splitting 重拆 -> issue_confirm；
- 第 3 版仍提意见 / 回炉额度用尽仍要求回炉 -> 升级暂停中断（status=escalated），
  摆明材料与选项，每次额外重拆都必须由人主动发起，不自动空转。

硬判内容（judge_issue_plan，纯函数，返回 errors/warnings）：
- errors（阻断结构错误，触发一次带反馈自检重调）：
  id 重复 / blocked_by 悬空或自引用 / 依赖图成环 /
  covered 项 covered_by 为空或引用悬空 / excluded 项 notes 为空 /
  readiness 非 blocked 但 issues 为空（blocked+空列表合法）。
- warnings（不阻断，仅随产物展示）：
  标题疑似横切票 / 工单超过 12 张 / HITL 单 decision_needed 与 open_questions 皆空 /
  单个需求点 covered_by 引用超过 3 张工单。

自检重调：首轮 errors 非空时，把 errors+warnings 拼成中文反馈注入
issue_revision_feedback（仅本轮调用的局部 state，不写回全局 state），重跑一次
（SELF_FIX_MAX=1）；再判一次后无论对错都放行交确认门，错误随 shape_errors 醒目展示。
issue_splitting 正常返回时把 issue_revision_feedback / prd_rewrite_feedback 一并清零
（消费即清零），保证确认门条件边按"本轮是否新产生意见"正确路由。
"""
from __future__ import annotations

import re

from langgraph.types import interrupt

from kernel.spec import NodeSpec

# 结构错误后的最大自检重调次数（首轮错误 -> 最多再调 1 次） [C 2026-09-11]
SELF_FIX_MAX = 1

# [C 2026-09-11] 块2：工单自动重拆保险丝（初版 + 2 次重拆 = 共 3 版；
# 计数达到该值后仍有意见 -> 升级暂停，额外重拆只能由人在升级中断里主动发起）
MAX_ISSUE_REVISIONS = 2

# "回PRD"包含判定关键词（统一小写匹配，命中即回炉；优先级高于确认精确匹配） [C 2026-09-11]
_BACK_TO_PRD_KEYWORDS: tuple[str, ...] = (
    "回prd",
    "回到prd",
    "重做prd",
    "重写prd",
    "重新讨论prd",
    "重新走prd",
    "prd重写",
    "回炉",
)

# 确认精确集合：归一化（strip + lower）后恰好属于其中才算确认。
# 空串=确认（与 requirement_confirm 空答复放行一致）；"可以，但要改"不是精确匹配，不判确认。
_CONFIRM_WORDS: frozenset[str] = frozenset(
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
        "同意拆",
    }
)

# 回炉额度用尽后仍要求回 PRD 的升级暂停说明 [C 2026-09-11]
_REDO_ESCALATION_REASON = (
    "已回炉重写 PRD 1 次仍无法拆出满意工单，流水线升级暂停，不再自动空转。"
    "可与 Pi 重新讨论 PRD 后重新发起流水线；也可现在回复「确认」按当前版落盘，"
    "或给出新的具体决策意见再拆一轮。"
)

# 第 3 版仍提修改意见的升级暂停说明（三个选项） [C 2026-09-11]
_REVISION_ESCALATION_REASON = (
    "工单清单已出到第 3 版（初版 + 2 轮重拆），你仍有修改意见，"
    "流水线升级暂停，不自动空转。请三选一："
    "① 带着新决策给出具体意见，由你主动发起再拆一轮；"
    "② 回复「回PRD」回炉重写 PRD（全程限 1 次）；"
    "③ 回复「确认」按当前版落盘。"
)

# 工单数量粒度告警阈值
TOO_MANY_ISSUES = 12
# 单个需求点被多少张工单共同覆盖时告警（疑似切分过碎）
COVERAGE_FANOUT_WARN = 3

# 横切票标题黑名单正则：命中即告警（按技术层/活动切分而非端到端用户价值）
_HORIZONTAL_TITLE_RE = re.compile(r"前端|后端|接口|联调|测试|单测|重构|美化|样式|边缘|异常情况")


def judge_issue_plan(plan: dict) -> dict:
    """纯函数：硬判工单方案的跨工单结构正确性。

    Args:
        plan: 符合 IssueSplittingSchema 的 dict（测试中也可直接构造最小 dict）。

    Returns:
        {"errors": [str], "warnings": [str]}：errors 触发自检重调并在产物中醒目展示，
        warnings 仅展示不阻断。
    """
    errors: list[str] = []
    warnings: list[str] = []

    issues = plan.get("issues") or []
    coverage = plan.get("coverage") or []
    readiness = plan.get("readiness") or ""

    # ── 1. id 收集与重复检查 ──
    ids: list[str] = []
    for issue in issues:
        ids.append(str(issue.get("id", "")))
    id_set = set(ids)
    seen: set[str] = set()
    for iid in ids:
        if iid in seen:
            errors.append(f"工单 id 重复：{iid}（全清单 id 必须唯一）")
        seen.add(iid)

    # ── 2. blocked_by 悬空 / 自引用，并建依赖图 ──
    graph: dict[str, list[str]] = {}
    for issue in issues:
        iid = str(issue.get("id", ""))
        deps = list(issue.get("blocked_by") or [])
        graph[iid] = [str(d) for d in deps]
        for dep in deps:
            dep = str(dep)
            if dep == iid:
                errors.append(f"工单 {iid} 的 blocked_by 存在自引用（工单不能依赖自己）")
            elif dep not in id_set:
                errors.append(f"工单 {iid} 的 blocked_by 引用了不存在的工单 {dep}（悬空依赖）")

    # ── 3. 依赖图成环检查（DFS 三色标记，跳过悬空/自引用边——它们已单独报错）──
    if _has_dependency_cycle(graph, id_set):
        errors.append("工单依赖图（blocked_by）存在循环依赖，请先打断环再开工")

    # ── 4. 覆盖矩阵三态一致性 ──
    for idx, item in enumerate(coverage, start=1):
        prd_item = str(item.get("prd_item") or f"第 {idx} 项")
        status = str(item.get("status") or "")
        refs = [str(r) for r in (item.get("covered_by") or [])]
        notes = str(item.get("notes") or "")

        if status == "covered":
            if not refs:
                errors.append(
                    f"覆盖矩阵第 {idx} 项「{prd_item}」状态为 covered，但 covered_by 为空"
                )
            for ref in refs:
                if ref not in id_set:
                    errors.append(
                        f"覆盖矩阵第 {idx} 项「{prd_item}」covered_by 引用了不存在的工单 {ref}"
                    )
        elif status == "excluded":
            if not notes.strip():
                errors.append(
                    f"覆盖矩阵第 {idx} 项「{prd_item}」状态为 excluded，但 notes 未写明排除原因"
                )
        if len(refs) > COVERAGE_FANOUT_WARN:
            warnings.append(
                f"覆盖矩阵第 {idx} 项「{prd_item}」被 {len(refs)} 张工单共同覆盖（>"
                f"{COVERAGE_FANOUT_WARN}），可能切分过碎或存在重复覆盖，请确认"
            )

    # ── 5. readiness 与工单数量联动：仅 blocked 允许空工单 ──
    if readiness != "blocked" and not issues:
        errors.append(
            f"readiness 为 {readiness or '（空）'} 但 issues 为空；"
            "仅 readiness=blocked（整份方案硬阻断）允许零工单，且须在 readiness_notes 讲清阻断点"
        )

    # ── 6. 工单级告警（不阻断）──
    for issue in issues:
        iid = str(issue.get("id", "?"))
        title = str(issue.get("title") or "")
        if _HORIZONTAL_TITLE_RE.search(title):
            warnings.append(
                f"工单 {iid} 标题「{title}」疑似横切票（按技术层/活动而非端到端用户价值切分），"
                "请确认是否应纵切重组"
            )
        if str(issue.get("issue_type") or "") == "HITL":
            decision = str(issue.get("decision_needed") or "").strip()
            open_questions = list(issue.get("open_questions") or [])
            if not decision and not open_questions:
                warnings.append(
                    f"工单 {iid} 标为 HITL 但 decision_needed 与 open_questions 皆空，"
                    "未写清待谁决定什么"
                )

    if len(issues) > TOO_MANY_ISSUES:
        warnings.append(
            f"工单数量 {len(issues)} 张（>{TOO_MANY_ISSUES}），粒度过细；"
            "建议按用户旅程合并，或用 version_map 分期交付"
        )

    return {"errors": errors, "warnings": warnings}
    # [C 2026-09-11] 工单方案结构硬判纯函数，便于零 API 单测


def _has_dependency_cycle(graph: dict[str, list[str]], nodes: set[str]) -> bool:
    """DFS 三色标记检测依赖图是否有环。悬空/自引用边跳过（已在调用处单列错误）。"""
    white, gray, black = 0, 1, 2
    color = {node: white for node in nodes}

    def dfs(node: str) -> bool:
        color[node] = gray
        for nxt in graph.get(node, []):
            if nxt not in nodes or nxt == node:
                continue  # 悬空/自引用不在此重复判定
            if color[nxt] == gray:
                return True
            if color[nxt] == white and dfs(nxt):
                return True
        color[node] = black
        return False

    return any(color[node] == white and dfs(node) for node in nodes)


def build_issue_self_fix_feedback(judged: dict) -> str:
    """把首轮硬判结果拼成中文可行动反馈，供重调时注入 issue_revision_feedback。"""
    lines: list[str] = [
        "【工单方案结构自检未通过】上一轮输出存在以下问题，请逐条修复后，"
        "重新输出完整的工单方案 JSON（不是只输出改动片段）。",
    ]
    errors = judged.get("errors") or []
    if errors:
        lines.append("")
        lines.append(f"一、必须修复的结构错误（{len(errors)} 条）：")
        for idx, msg in enumerate(errors, start=1):
            lines.append(f"{idx}. {msg}")
    warnings = judged.get("warnings") or []
    if warnings:
        lines.append("")
        lines.append(f"二、建议一并处理的警告（{len(warnings)} 条，不强制）：")
        for idx, msg in enumerate(warnings, start=1):
            lines.append(f"{idx}. {msg}")
    lines.append("")
    lines.append("未被点名要求修改的部分保持稳定，不要借机扩大范围或重写无关工单。")
    return "\n".join(lines).strip()


def make_issue_splitting(deps):
    """拆研发工单节点工厂：返回签名 (state: dict) -> dict 的节点函数。"""

    def issue_splitting(state: dict) -> dict:
        prompt = deps.registry.read_prompt("issue_splitting")
        schema = deps.registry.load_schema("issue_splitting")
        spec = NodeSpec(
            name="issue_splitting",
            prompt_template=prompt,
            output_schema=schema,
        )

        # 1. 首轮模型产出（runner 内部已含 schema 校验重试，跨工单结构在此判）
        result = deps.runner.run_raw(spec, state)
        judged = judge_issue_plan(result)
        first_had_errors = bool(judged["errors"])

        # 2. 结构错误 -> 带中文反馈自检重调（最多 SELF_FIX_MAX 次）。
        #    issue_revision_feedback 只注入本轮局部 state，不写回全局 state。 [C 2026-09-11]
        if first_had_errors:
            for _ in range(SELF_FIX_MAX):
                feedback = build_issue_self_fix_feedback(judged)
                local_state = {**state, "issue_revision_feedback": feedback}
                result = deps.runner.run_raw(spec, local_state)
                judged = judge_issue_plan(result)

        # 3. 终判结果随产物落盘；两轮仍错也不抛异常，shape_errors 醒目交人工
        plan = {
            **result,
            "shape_errors": judged["errors"],
            "shape_warnings": judged["warnings"],
            "self_fixed": first_had_errors,
        }
        return {
            "issue_plan": plan,
            # [C 2026-09-11] 块2：消费即清零。重拆跑完后把两个意见字段写空，
            # 确认门条件边才不会把已消化的意见再次路由回 issue_splitting / prd_generation
            "issue_revision_feedback": "",
            "prd_rewrite_feedback": "",
        }
        # [C 2026-09-11] self_fixed=首轮是否曾被结构判错（含重调后修好/未修好两种情形）

    return issue_splitting


# [C 2026-09-11] 块2：工单确认门（issue_confirm，HITL，不调模型）


# 回PRD关键词命中位置前 2~3 字内出现这些否定语时，视为"不想回炉"，落 feedback。
# 单字"不/别/勿"兜底，双字词优先在 endswith 判定中自然命中。 [C 2026-09-11]
_BACK_TO_PRD_NEGATIONS: tuple[str, ...] = (
    "不用",
    "不要",
    "不必",
    "不会",
    "不想",
    "别",
    "勿",
    "不",
)


def _has_negative_prefix(text: str, idx: int) -> bool:
    """关键词命中位置前 3 个字符内是否紧邻否定语（不/别/勿/不用/不要/不必 等）。"""
    window = text[max(0, idx - 3):idx]
    return any(window.endswith(neg) for neg in _BACK_TO_PRD_NEGATIONS)


def _matches_back_to_prd(compact: str) -> bool:
    """去空白文本上任一回PRD关键词命中、且命中位置不紧邻否定前缀时返回 True。"""
    for keyword in _BACK_TO_PRD_KEYWORDS:
        start = 0
        while True:
            idx = compact.find(keyword, start)
            if idx < 0:
                break
            if not _has_negative_prefix(compact, idx):
                return True
            start = idx + 1  # 同一关键词可能多次出现，继续找下一处
    return False


def classify_confirm_answer(text: str) -> str:
    """纯函数：把确认门用户答复归一化三分类为 confirm / back_to_prd / feedback。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做包含/精确匹配；
    2. **先做回 PRD 包含判定**：先去除词内空白（"回 PRD""重写 PRD" 也算回炉），
       含任一 ``_BACK_TO_PRD_KEYWORDS`` 且命中位置前 3 字内无否定语
       （不/别/勿/不用/不要/不必 等，如"不用回炉，直接改工单""不要重做PRD"）
       即 back_to_prd（故"回PRD重写，确认"仍判回炉，不被确认词截胡）；
    3. **再做确认精确集合判定**：归一化后恰好属于 ``_CONFIRM_WORDS`` 才 confirm，
       空串=确认（与 requirement_confirm 空答复放行一致）；
       "可以，但要改"不是精确匹配，落 feedback；
    4. 其余一律 feedback（打回重拆）。
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    # [C 2026-09-11] 包含判定在去空白（含全角空格）文本上做，并排除紧邻否定前缀
    compact = re.sub(r"[\s\u3000]+", "", lowered)
    if _matches_back_to_prd(compact):
        return "back_to_prd"
    if lowered in _CONFIRM_WORDS:
        return "confirm"
    return "feedback"
    # [C 2026-09-11] 答复分类纯函数，确认门节点与零 API 单测共用


def route_after_issue_confirm(state: dict) -> str:
    """条件边路由：不新增 decision 字段，全凭既有状态推断下一步。

    - issue_plan 为空且 prd_rewrite_feedback 非空 -> ``prd_generation``
      （确认门发起回炉：plan 已清空、回炉意见待 PRD 重写消费）；
    - issue_revision_feedback 非空 -> ``issue_splitting``（带人工意见重拆）；
    - 其余 -> ``artifact_persist``（确认落盘；确认分支不写意见字段，plan 原样保留）。
    """
    plan = state.get("issue_plan") or {}
    prd_rewrite_feedback = str(state.get("prd_rewrite_feedback") or "")
    revision_feedback = str(state.get("issue_revision_feedback") or "")
    if not plan and prd_rewrite_feedback:
        return "prd_generation"
    if revision_feedback:
        return "issue_splitting"
    return "artifact_persist"
    # [C 2026-09-11] 工单确认门三分支条件边路由纯函数


def _normalize_answer(answer: object) -> tuple[str, str]:
    """resume 值归一化：返回 (分类, strip 后原文)；None/非字符串安全转空串。"""
    if isinstance(answer, str):
        text = answer.strip()
    elif answer is None:
        text = ""
    else:
        text = str(answer).strip()
    return classify_confirm_answer(text), text


def _build_prd_redo_feedback(user_text: str) -> str:
    """拼确认门发起的 PRD 回炉意见：含用户原话，说明修订后自动重过评审门并重拆。"""
    return (
        "【工单拆解阶段回炉意见】\n"
        "上一版 PRD 已通过评审，但在拆成研发工单后的人工确认环节被打回，"
        "暴露出 PRD 层面需要重新讨论的问题。用户原话如下：\n"
        f"「{user_text}」\n"
        "请针对上述问题输出完整修订版 PRD（不是只输出改动片段）。"
        "修订版会自动重新通过 PRD 评审门；评审通过后会自动重新拆单，"
        "并再次回到工单确认门请用户确认。"
    )


def _prior_issue_feedbacks(state: dict) -> list[dict]:
    """从 human_feedback 摘出此前在工单确认门留下的答复，供升级暂停载荷 recap。"""
    summary: list[dict] = []
    for item in state.get("human_feedback") or []:
        if isinstance(item, dict) and item.get("node") == "issue_confirm":
            summary.append(
                {
                    "kind": item.get("kind", ""),
                    "round": item.get("round", ""),
                    "feedback": item.get("feedback", ""),
                }
            )
    return summary


def make_issue_confirm(deps):  # noqa: ARG001 - 工厂签名与其他节点保持一致，本节点不调模型/不取依赖
    """工单确认门节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    交互协议（首次中断 status="draft"）：
    - confirm -> 只追加 human_feedback，plan 原样保留 -> 条件边落盘；
    - feedback（前 2 版）-> issue_revision_count+1、写 issue_revision_feedback
      -> 条件边回 issue_splitting 重拆 -> 重回本确认门；
    - feedback（第 3 版起）-> 先 interrupt 升级暂停（status="escalated"），
      二次答复：确认落盘 / 回PRD走回炉判定 / 带新决策的具体意见再拆一轮；
    - back_to_prd（redo=0）-> 清空 plan、redo 置 1、修订计数归零、写 prd_rewrite_feedback
      -> 条件边回 prd_generation；back_to_prd（redo>=1）-> 升级暂停，
      二次答复不再回 PRD（再次要求回炉按"仍需回炉PRD："前缀的工单意见处理）。
    """

    def issue_confirm(state: dict) -> dict:
        plan = state.get("issue_plan") or {}
        requirement_name = state.get("requirement_name", "")
        feedback_log = [
            dict(item)
            for item in (state.get("human_feedback") or [])
            if isinstance(item, dict)
        ]

        def append_log(kind: str, text: str, round_label: str) -> None:
            feedback_log.append(
                {
                    "node": "issue_confirm",
                    "kind": kind,
                    "round": round_label,
                    "feedback": text,
                }
            )

        def confirm_update(kind: str, text: str, round_label: str) -> dict:
            # 确认：仅留痕，不写任何意见/计数字段 -> plan 原样保留 -> 路由落盘
            append_log(kind, text, round_label)
            return {"human_feedback": feedback_log}

        def feedback_update(text: str, count: int) -> dict:
            # 打回重拆：计数 +1，意见按固定格式包装后供 issue_splitting prompt 注入
            new_count = count + 1
            return {
                "issue_revision_count": new_count,
                "issue_revision_feedback": (
                    f"【第{new_count}轮工单修改意见】{text}\n"
                    "请逐条针对性调整工单清单，未要求改的部分保持稳定"
                ),
                "human_feedback": feedback_log,
            }

        def escalation_interrupt(reason: str):
            """升级暂停：抛出第二个 interrupt 请人主动决策（不自动空转）。"""
            return interrupt(
                {
                    "node": "issue_confirm",
                    "status": "escalated",
                    "reason": reason,
                    "requirement_name": requirement_name,
                    "issue_plan": plan,
                    "prior_feedbacks": _prior_issue_feedbacks(state),
                }
            )

        def ask_then_route(text: str, count: int, round_label: str) -> dict:
            """第 3 版仍有意见：升级暂停，按二次答复分流。"""
            second_answer = escalation_interrupt(_REVISION_ESCALATION_REASON)
            kind2, text2 = _normalize_answer(second_answer)
            if kind2 == "confirm":
                return confirm_update(kind2, text2, "escalation-confirm")
            if kind2 == "back_to_prd":
                # 二次答复改选回 PRD：交回炉判定（redo=0 正常回炉 / redo=1 再升级）；
                # 留痕由 handle_back_to_prd 按最终动作统一记录，避免重复
                return handle_back_to_prd(text2)
            # 人带来新决策的具体意见：主动发起再拆一轮（计数照常 +1）
            append_log(kind2, text2, "escalation-feedback")
            return feedback_update(text2, count)

        def handle_back_to_prd(text: str) -> dict:
            redo = int(state.get("issue_prd_redo_count") or 0)
            if redo >= 1:
                # 回炉额度已用尽：升级暂停。二次答复只有确认/带意见再拆，不再回 PRD。
                second_answer = escalation_interrupt(_REDO_ESCALATION_REASON)
                kind2, text2 = _normalize_answer(second_answer)
                if kind2 == "confirm":
                    return confirm_update(kind2, text2, "redo-escalation-confirm")
                count = int(state.get("issue_revision_count") or 0)
                if kind2 == "back_to_prd":
                    # 仍坚持回炉：不再次回 PRD，转为带"仍需回炉PRD："前缀的工单意见
                    text2 = f"仍需回炉PRD：{text2}"
                append_log("feedback", text2, "redo-escalation-feedback")
                if count >= MAX_ISSUE_REVISIONS:
                    # 同时触达 3 版保险丝：再给一次升级选择，不自动空转
                    return ask_then_route(text2, count, "redo-escalation-feedback")
                return feedback_update(text2, count)

            # redo=0：发起全程唯一一次 PRD 回炉——清空工单、回炉计数置 1、
            # 工单修订计数归零（回炉后等同新一轮拆单）、评审打回计数不在此动。
            append_log("back_to_prd", text, "redo-1")
            return {
                "issue_plan": {},
                "issue_prd_redo_count": 1,
                "issue_revision_count": 0,
                "issue_revision_feedback": "",
                "prd_rewrite_feedback": _build_prd_redo_feedback(text),
                "human_feedback": feedback_log,
            }

        # ── 首次中断：请用户审阅工单草案 ──
        first_answer = interrupt(
            {
                "node": "issue_confirm",
                "status": "draft",
                "requirement_name": requirement_name,
                "issue_plan": plan,
            }
        )
        kind, text = _normalize_answer(first_answer)

        if kind == "confirm":
            return confirm_update(kind, text, "draft-confirm")

        if kind == "back_to_prd":
            # [C 2026-09-11] 不在 draft 分支留痕：redo=0 由 handle_back_to_prd
            # 统一记 redo-1；redo>=1 进入升级暂停，留痕按二次答复的最终动作记录，
            # 与 ask_then_route 改选回PRD 路径保持"恰好一次"约定
            return handle_back_to_prd(text)

        # feedback：2 轮保险丝内直接重拆；第 3 版起先升级暂停
        count = int(state.get("issue_revision_count") or 0)
        append_log(kind, text, f"draft-feedback-{count + 1}")
        if count >= MAX_ISSUE_REVISIONS:
            return ask_then_route(text, count, f"draft-feedback-{count + 1}")
        return feedback_update(text, count)

    return issue_confirm
    # [C 2026-09-11] 块2 工单确认门：分类协议/2 轮保险丝/回炉限 1 次/双升级暂停


# [C 2026-09-11] nodes/issues.py 块2（issue_confirm 确认门）新增完成
