# [C 2026-09-12 by codebuddy-ds41flash] 设计评测体系节点（eval_design + eval_confirm HITL）
"""设计评测体系：流水线起草四层考题集 -> 人工在确认门拍板及格线并落 Promptfoo YAML 草案。

图位置（第 5 段，仅 AI 核心需求经过；插在 PRD 评审通过之后、拆研发工单之前）：
    prd_review ->（非 reject 且 ai_core=True）eval_design -> eval_confirm(HITL)
    -> 条件边两态：
        pass    -> issue_splitting（确认落盘：写 verdict、渲染 Promptfoo YAML 草案与评测档案进 state）
        redraft -> eval_design（提修改意见，前 2 轮自动；第 3 版起升级暂停）
普通需求（ai_core=False/None）route_after_review 直接去 issue_splitting，行为与现状逐字一致。

eval_design（make_eval_design）：
- 调模型起草四层考题集 + 每题评分方式 + 及格线建议值，写入 state["eval_system"]；
- 轮次>0 时 state["eval_revision_feedback"] 由 prompt 模板读取注入，节点返回时**消费即清零**
  （对齐 prd_generation 的复审反馈注入/清零方式）。

eval_confirm（make_eval_confirm，HITL，不调模型）：
- 交互协议照抄 launch_confirm 循环模式但更简（本门没有回退上游分支）：
  「确认」类 -> pass（写 verdict、渲染 YAML 草案与评测档案 dict 写入 state）-> 条件边去 issue_splitting；
  其余文本一律修改意见 -> eval_revision_count+1、写 eval_revision_feedback、verdict="redraft"
  -> 条件边回 eval_design；
  第 3 版（计数达 2）仍提意见 -> interrupt(status="escalated") 升级暂停，二次答复仍支持 确认/意见；
  升级后意见最多再重起草一轮（对齐 launch_escalation_depth 机制），达上限后保持 escalated 暂停、
  不自动空转。

render_promptfoo_yaml（纯函数）：把结构化评测体系渲染成合法 Promptfoo YAML 文本。
本任务只渲染 YAML，不跑真 Promptfoo；replay 占位层（exams=[]）不产生 test 条目。
"""
from __future__ import annotations

import re
from datetime import datetime

import yaml
from langgraph.types import interrupt

from kernel.spec import NodeSpec

# 评测体系自动重起草保险丝（初版 + 2 轮重起草 = 共 3 版；
# 计数达到该值后仍有意见 -> 升级暂停，额外重起草只能由人在升级中断里主动发起）
MAX_EVAL_REVISIONS = 2

# 升级暂停后继续喂意见的硬深度上限：升级后再给意见最多 1 轮重起草；
# 超过则保持 escalated 暂停、不再自动重起草，防理论无限递归（对齐 launch_escalation_depth 机制）。
MAX_EVAL_ESCALATED_REVISIONS = 1

# 总重起草轮数硬上限（自动 2 轮 + 升级后 1 轮 = 3）；
# 无需额外 state 字段，用 eval_revision_count 本身作深度界，达上限保持 escalated。
MAX_EVAL_TOTAL_REVISIONS = MAX_EVAL_REVISIONS + MAX_EVAL_ESCALATED_REVISIONS

# 第 3 版仍提修改意见的升级暂停说明（三选一） [C 2026-09-12 by codebuddy-ds41flash]
_REVISION_ESCALATION_REASON = (
    "评测体系已出到第 3 版（初版 + 2 轮重起草），你仍有修改意见，"
    "流水线升级暂停，不自动空转。请三选一："
    "① 带着新决策给出具体意见，由你主动发起再起草一轮（升级后限 1 轮）；"
    "② 回复「确认」按当前版落盘 Promptfoo YAML 草案与评测档案；"
    "③ 让 Pi 协助调查（如查参考库/竞品四层出题口径）后再给意见。"
)

# 已达升级深度硬上限的暂停说明（不再自动重起草，反复抛中断等真人决策）
_ESCALATION_LIMIT_REASON = (
    "已达人工介入上限（升级暂停后再给意见最多 1 轮），流水线保持升级暂停、不再自动重起草；"
    "请与 Pi 线下核实后重新发起流水线，或回复「确认」按当前版落盘。"
)

# 确认精确集合：归一化（strip + lower）后恰好属于其中才算确认。
# 空串=确认（与其他确认门空答复放行一致）；
# "可以，但要改"不是精确匹配，不判确认（落 feedback 打回重起草）。
_EVAL_CONFIRM_WORDS: frozenset[str] = frozenset(
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
        "同意评测",
    }
)

# Promptfoo prompts 段的 PRD 核心任务 prompt 模板占位（第 5 段只出草案，不接真调用）
_PROMPTFOO_PROMPT_PLACEHOLDER = "{{prd_core_task_prompt}}"

# Promptfoo provider 占位：项目自有 DeepSeek（config.yaml 的 litellm_model = deepseek/deepseek-v4-flash）。
# 第 5 段只出单 provider 草案，第 6 段对比选型才横向扩展候选 provider。
# 说明：Promptfoo 的 provider id 是 "<provider>:<model>" 格式，裸 "deepseek" 会被 validate 拒绝，
# 故默认参数 provider="deepseek" 需补成带模型的合法 id；调用方传已含 ":" 的完整 id 时原样使用。
_PROMPTFOO_DEFAULT_PROVIDERS: dict[str, str] = {
    "deepseek": "deepseek:deepseek-v4-flash",
}

# 四层考题的层名（评测档案 exam_summary 恒定包含这四键）
_ALL_LAYERS: tuple[str, ...] = ("typical", "boundary", "adversarial", "replay")


def classify_eval_answer(text: str) -> str:
    """纯函数：把确认评测体系门用户答复归一化二分类为 pass / feedback。

    判定顺序（顺序不可换）：
    1. strip；英文小写化后做精确匹配；
    2. **确认精确集合判定**：归一化后恰好属于 ``_EVAL_CONFIRM_WORDS`` 才 pass，
       空串=确认（与其他确认门空答复放行一致）；
    3. 其余一律 feedback（打回重新起草）——本门没有回退上游分支，
       否定式（"不确认""先放一放"）与任意自由文本自然落 feedback。

    Returns:
        ``pass`` / ``feedback``
    """
    stripped = str(text if text is not None else "").strip()
    lowered = stripped.lower()
    if lowered in _EVAL_CONFIRM_WORDS:
        return "pass"
    return "feedback"
    # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门答复分类纯函数


def route_after_eval_confirm(state: dict) -> str:
    """条件边路由：按 eval_confirm.verdict 两态映射。

    - redraft -> ``eval_design``（带人工意见重新起草）
    - 其余/缺失 -> ``issue_splitting``（确认落盘或保守放行，避免卡死）
    """
    confirm = state.get("eval_confirm") or {}
    verdict = str(confirm.get("verdict") or "")
    if verdict == "redraft":
        return "eval_design"
    return "issue_splitting"
    # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门两态条件边路由纯函数


def _assertion_to_assert(assertion: str) -> dict:
    """把 assertion 文本按前缀映射为 Promptfoo 断言条目。

    支持 `equals:` / `contains:` / `regex:` 三种前缀；无前缀时默认 contains。
    """
    text = str(assertion or "").strip()
    lowered = text.lower()
    for prefix, kind in (
        ("equals:", "equals"),
        ("regex:", "regex"),
        ("contains:", "contains"),
    ):
        if lowered.startswith(prefix):
            return {"type": kind, "value": text[len(prefix):].strip()}
    return {"type": "contains", "value": text}


def _build_promptfoo_test(exam: dict, layer: str) -> dict:
    """把一道考题映射成一条 Promptfoo test 条目（description + vars + assert）。"""
    scorer = str(exam.get("scorer") or "")
    # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：vars.critical 供评测达标硬判筛关键题；
    # 对抗层题本就视为关键（无论 exam.critical 值如何一律 true），其他层取 exam.critical。
    critical = True if layer == "adversarial" else bool(exam.get("critical", False))
    test: dict = {
        "description": f"[{layer}] {exam.get('id', '')} {exam.get('description', '')}".strip(),
        "vars": {
            "input": str(exam.get("prompt_hint") or ""),
            "exam_id": str(exam.get("id") or ""),
            "layer": layer,
            "critical": critical,
        },
    }
    if scorer == "llm_judge":
        test["assert"] = [
            {"type": "llm-rubric", "value": str(exam.get("judge_rubric") or "")}
        ]
        ratio = exam.get("manual_review_ratio")
        if ratio is not None:
            test["vars"]["manual_review_ratio"] = ratio
    else:  # assertion（默认）
        test["assert"] = [_assertion_to_assert(str(exam.get("assertion") or ""))]
    return test


def render_promptfoo_yaml(eval_system: dict, provider: str = "deepseek") -> str:
    """纯函数：把结构化评测体系渲染成合法 Promptfoo YAML 文本。

    Args:
        eval_system: EvalDesignSchema 同构 dict（四层 exam_sets + pass_lines）。
        provider: providers 段占位（默认 "deepseek"，第 5 段只出单 provider 占位，
            第 6 段才横向扩展）；裸 provider 名按 ``_PROMPTFOO_DEFAULT_PROVIDERS`` 补成
            Promptfoo 合法的 "<provider>:<model>" id，已含 ":" 的完整 id 原样使用。

    Returns:
        含 prompts / providers / tests 三个顶层键的 YAML 文本（``yaml.safe_load`` 可解析）：
        - prompts：PRD 核心任务的 prompt 模板占位（单条）；
        - providers：单条 provider 占位（裸名补成合法的 "<provider>:<model>" id）；
        - tests：每题一条（description + vars + assert）；
          assertion 题 -> equals/contains/regex；llm_judge 题 -> llm-rubric 带 rubric 文本；
          replay 占位层（exams=[]）不产生 test 条目。

    Note:
        本函数只渲染 YAML，**不跑真 Promptfoo**（执行器外置，第 8 段才接）。
    """
    system = eval_system if isinstance(eval_system, dict) else {}
    tests: list[dict] = []
    for exam_set in system.get("exam_sets") or []:
        if not isinstance(exam_set, dict):
            continue
        layer = str(exam_set.get("layer") or "")
        exams = exam_set.get("exams") or []
        if not isinstance(exams, list):
            continue
        for exam in exams:
            if isinstance(exam, dict):
                tests.append(_build_promptfoo_test(exam, layer))

    provider_text = str(provider or "")
    provider_id = (
        provider_text
        if ":" in provider_text
        else _PROMPTFOO_DEFAULT_PROVIDERS.get(provider_text, provider_text)
    )
    document = {
        "prompts": [_PROMPTFOO_PROMPT_PLACEHOLDER],
        "providers": [provider_id],
        "tests": tests,
    }
    return yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
    # [C 2026-09-12 by codebuddy-ds41flash] Promptfoo YAML 渲染纯函数（只出草案，不跑真工具）


def _normalize_answer(answer: object) -> tuple[str, str]:
    """resume 值归一化：返回 (分类, strip 后原文)；None/非字符串安全转空串。"""
    if isinstance(answer, str):
        text = answer.strip()
    elif answer is None:
        text = ""
    else:
        text = str(answer).strip()
    return classify_eval_answer(text), text


def _prior_eval_feedbacks(state: dict) -> list[dict]:
    """从 human_feedback 摘出此前在确认评测体系门留下的答复，供升级暂停载荷 recap。"""
    summary: list[dict] = []
    for item in state.get("human_feedback") or []:
        if isinstance(item, dict) and item.get("node") == "eval_confirm":
            summary.append(
                {
                    "kind": item.get("kind", ""),
                    "round": item.get("round", ""),
                    "feedback": item.get("feedback", ""),
                }
            )
    return summary


def _build_eval_archive(state: dict) -> dict:
    """构造评测档案 dict（预留第 11 段接口）。

    本任务只构造 dict 写入 state["eval_archive"]，**不写知识库、不落文件**
    （落盘统一走 artifact_persist，第 8 段再接）。
    """
    eval_system = state.get("eval_system") or {}
    # 恒定包含四层键，缺失层计 0（便于第 11 段对账）
    exam_summary: dict[str, int] = {layer: 0 for layer in _ALL_LAYERS}
    for item in eval_system.get("exam_sets") or []:
        if not isinstance(item, dict):
            continue
        layer = str(item.get("layer") or "")
        exams = item.get("exams") or []
        exam_summary[layer] = len(exams) if isinstance(exams, list) else 0
    return {
        "requirement_name": state.get("requirement_name", ""),
        "created_at": datetime.now().strftime("%Y-%m-%d"),
        "exam_summary": exam_summary,
        "pass_lines": eval_system.get("pass_lines") or {},
        "archive_version": 1,
        "replay_badcases": [],
        "regression_trigger": {"enabled": False, "note": "二期第 11 段接入"},
    }


def make_eval_design(deps):
    """评测体系起草节点工厂：返回签名 (state: dict) -> dict 的节点函数。

    调模型起草四层考题集 + 每题评分方式 + 及格线建议值，写入 state["eval_system"]。
    轮次>0 时 state["eval_revision_feedback"] 由 prompt 模板读取注入，返回时消费即清零
    （对齐 prd_generation 的复审反馈注入/清零方式）。
    """

    def eval_design(state: dict) -> dict:
        prompt = deps.registry.read_prompt("eval_design")
        schema = deps.registry.load_schema("eval_design")
        spec = NodeSpec(
            name="eval_design",
            prompt_template=prompt,
            output_schema=schema,
        )
        result = deps.runner.run_raw(spec, state)
        # 消费即清零：确认门写入的重起草意见只注入本轮一次，避免陈旧意见被反复注入
        return {"eval_system": result, "eval_revision_feedback": ""}
        # [C 2026-09-12 by codebuddy-ds41flash] 起草即清零意见，确认门条件边据此正确路由

    return eval_design


def make_eval_confirm(deps):  # noqa: ARG001 - 工厂签名与其他节点保持一致，本节点不调模型/不取依赖
    """确认评测体系门节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    交互协议（首次中断 status="draft"）：
    - pass -> 写 verdict=pass、渲染 Promptfoo YAML 草案与评测档案 dict 写入 state
      -> 条件边去 issue_splitting；
    - feedback（前 2 版）-> eval_revision_count+1、写 eval_revision_feedback、verdict=redraft
      -> 条件边回 eval_design 重起草 -> 重回本确认门；
    - feedback（第 3 版起）-> 先 interrupt 升级暂停（status="escalated"），
      二次答复：确认落盘 / 带新决策的具体意见再起草一轮（升级后限 1 轮）；
    - 升级后再给意见达硬上限（eval_revision_count 触顶）-> 保持 escalated 暂停，
      反复抛中断等真人答复，非确认不写任何意见/计数字段，不自动空转。
    """

    def eval_confirm(state: dict) -> dict:
        eval_system = state.get("eval_system") or {}
        requirement_name = state.get("requirement_name", "")
        count = int(state.get("eval_revision_count") or 0)
        feedback_log = [
            dict(item)
            for item in (state.get("human_feedback") or [])
            if isinstance(item, dict)
        ]

        def append_log(kind: str, text: str, round_label: str) -> None:
            feedback_log.append(
                {
                    "node": "eval_confirm",
                    "kind": kind,
                    "round": round_label,
                    "feedback": text,
                }
            )

        def pass_update(text: str, round_label: str) -> dict:
            # 确认：写 verdict + 渲染 YAML 草案与评测档案 dict 进 state -> 路由去 issue_splitting
            append_log("pass", text, round_label)
            return {
                "eval_confirm": {"verdict": "pass", "user_feedback": text},
                "eval_yaml_draft": render_promptfoo_yaml(eval_system),
                "eval_archive": _build_eval_archive(state),
                "human_feedback": feedback_log,
            }

        def feedback_update(text: str, current: int) -> dict:
            # 打回重起草：计数 +1，意见按固定格式包装后供 eval_design prompt 注入
            new_count = current + 1
            return {
                "eval_confirm": {"verdict": "redraft", "user_feedback": text},
                "eval_revision_count": new_count,
                "eval_revision_feedback": (
                    f"【第{new_count}轮评测体系修改意见】{text}\n"
                    "请逐条针对性调整四层考题、评分方式与及格线建议值，未要求改的部分保持稳定"
                ),
                "human_feedback": feedback_log,
            }

        def escalation_interrupt(reason: str):
            """升级暂停：抛出第二个 interrupt 请人主动决策（不自动空转）。"""
            return interrupt(
                {
                    "node": "eval_confirm",
                    "status": "escalated",
                    "reason": reason,
                    "requirement_name": requirement_name,
                    "eval_system": eval_system,
                    "prior_feedbacks": _prior_eval_feedbacks(state),
                }
            )

        def escalation_limit_stop(round_label: str) -> dict:
            """已达人工介入上限：暂停循环，反复抛同一 escalated 中断等真人答复。

            只有「确认」才跳出循环、写 verdict=pass 落盘；
            非确认答复不写任何意见/计数字段，恰好留痕一条后继续抛中断等下一轮真人输入。
            人工驱动的反复暂停不是空转：每轮都在等真人输入、不调模型。
            """
            seq = 0
            while True:
                seq += 1
                answer = escalation_interrupt(_ESCALATION_LIMIT_REASON)
                kind2, text2 = _normalize_answer(answer)
                label = round_label if seq == 1 else f"{round_label}-{seq}"
                if kind2 == "pass":
                    return pass_update(text2, f"{label}-confirm")
                append_log(kind2, text2, label)

        def ask_then_route(text: str, current: int, round_label: str) -> dict:
            """第 3 版仍有意见：升级暂停，按二次答复分流。"""
            if current >= MAX_EVAL_TOTAL_REVISIONS:
                # 升级额度已用尽：保持 escalated，不再自动重起草
                return escalation_limit_stop(f"{round_label}-escalation-limit")
            second_answer = escalation_interrupt(_REVISION_ESCALATION_REASON)
            kind2, text2 = _normalize_answer(second_answer)
            if kind2 == "pass":
                return pass_update(text2, "escalation-confirm")
            # 人带来新决策的具体意见：主动发起再起草一轮（计数照常 +1）
            append_log(kind2, text2, "escalation-feedback")
            return feedback_update(text2, current)

        # ── 首次中断：请用户审阅评测体系草案（四层考题 + 及格线建议值）──
        first_answer = interrupt(
            {
                "node": "eval_confirm",
                "status": "draft",
                "requirement_name": requirement_name,
                "eval_system": eval_system,
                "eval_revision_count": count,
            }
        )
        kind, text = _normalize_answer(first_answer)

        if kind == "pass":
            return pass_update(text, "draft-confirm")

        # feedback：2 轮保险丝内直接重起草；第 3 版起先升级暂停
        append_log(kind, text, f"draft-feedback-{count + 1}")
        if count >= MAX_EVAL_TOTAL_REVISIONS:
            # 升级额度已用尽：保持 escalated 暂停，不自动空转
            return escalation_limit_stop(f"draft-feedback-{count + 1}-escalation-limit")
        if count >= MAX_EVAL_REVISIONS:
            return ask_then_route(text, count, f"draft-feedback-{count + 1}")
        return feedback_update(text, count)

    return eval_confirm
    # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门：二分类/2 轮保险丝/升级暂停
    # [C 2026-09-12 by codebuddy-ds41flash] 用 eval_revision_count 自身作升级深度界（不新增 state 字段）


# [C 2026-09-12 by codebuddy-ds41flash] nodes/eval_design.py 新增完成
