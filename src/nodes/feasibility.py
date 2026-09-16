# [C 2026-09-12 by codebuddy-ds41flash] 判断需求与 AI 的边界节点（feasibility_check + feasibility_confirm HITL）
# [C 2026-09-14 by S043-b3] feasibility_check 重写：探针真跑（进程内 function calling ReAct）
"""判断需求与 AI 的边界：流水线生成可行性报告（含探针方案）-> 自动跑探针采集证据 -> 人工在确认门录入结论。

图位置（第 2 段，仅 AI 核心需求经过）：
    requirement_confirm -> needs_discovery ->（ai_core=True）feasibility_check -> feasibility_confirm(HITL)
    -> 条件边四态：
        pass        -> prd_generation（进 ai-native PRD）
        reclassify  -> prd_generation（ai_core 已改为 False，prd_generation 自动选普通模板）
        reshape     -> requirement_confirm（回第 1 段调整范围后重过判定；全程限 1 次）
        abandon     -> END（放弃，产出可行性结论留档）
普通需求（ai_core=False）不经过本模块，挖完需求后 needs_discovery 直接进 prd_generation。

feasibility_check（make_feasibility_check）：
- 阶段 1：调模型生成可行性报告（三方对照表+探针方案+风险+成本+结论），写入 state["feasibility_report"]；
- 阶段 2：**自动跑探针**——用进程内 function calling ReAct 循环（硬上限 8 轮）执行 probe_plan：
  - build_chat() 构建裸 ChatLiteLLM，bind_tools([RunProbeTool]) 绑定探针工具；
  - 系统消息要求模型对每条探针调用 run_probe 工具，根据实际输出与 expected 对比判定 pass/fail；
  - 模型返回 tool_calls 时，用 build_llm()（as_text=True 文本通道）真调模型跑探针 prompt，拿实际输出；
  - 把工具调用结果作为 ToolMessage 发回模型，让模型下一轮判定 pass/fail；
  - 模型返回最终判定 JSON（含 results 列表）或达到 8 轮上限后结束；
  - 探针真调失败（如 DeepSeek 网关断）标"执行失败"，不中断整条流水线；
  - build_chat/build_llm 报错走 interrupt（status="tool_error"），等用户修复后重跑。
- 阶段 3：证据回填报告——按 target_capability 匹配，绿能力点有对应探针但无证据时自动降级为黄，
  红能力点无证据保持红；证据列表写入 state["feasibility_evidence"]。
- 阶段 4（S048 候选池前置）：把报告的 model_candidates（2–5 条）拆出模型名后调
  config model_catalog.price_script 取实时单价，逐条补 price / price_source /
  price_fetched_at / price_note / access_status，写 state["model_candidates"]；
  清单缺失 / 价格脚本失败 / 候选为空三条降级路径都只记原因（candidate_pool_note），不阻断。

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

import datetime
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any, Callable

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langgraph.graph import END
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field

from kernel.exceptions import NodeExecutionError
from kernel.model import build_chat, build_llm, extract_json
from kernel.spec import NodeSpec

# 重塑额度：全程限 1 次；计数达到该值后再要求重塑 -> 先升级暂停，由人重新拍板 [C 2026-09-12]
MAX_FEASIBILITY_RESHAPE = 1

# ReAct 循环硬上限：探针执行最多 8 轮模型调用，防止死循环 [C 2026-09-14 by S043-b3]
MAX_REACT_ROUNDS = 8


class RunProbeTool(BaseModel):
    """执行一条探针：用指定 prompt 调用模型，返回实际输出。

    用于 feasibility_check 节点的 ReAct 循环：模型通过 function calling 调用本工具，
    节点解析参数后用 build_llm()（as_text=True 文本通道）真调模型跑探针 prompt，
    把实际输出作为 ToolMessage 发回模型，让模型下一轮判定 pass/fail。
    """

    # bind_tools 注册名=run_probe，与 _run_react_probes 的 tc_name 判定一致
    # [C 2026-09-14 by codebuddy-ds41flash] S043 块3 真机修复：显式 title 修工具名不匹配
    model_config = ConfigDict(title="run_probe")

    probe_name: str = Field(description="探针名称")
    prompt: str = Field(description="要发给模型的探针 prompt 文本")
    expected: str = Field(description="期望观察到的结果，用于判定是否通过")
    # [C 2026-09-14 by S043-b3] RunProbeTool 工具定义（pydantic BaseModel，供 bind_tools 绑定）

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

# 证据不齐时二次确认的强制放行词（归一化后精确匹配）
# 与原 _PASS_WORDS 区分：原词如「通过」「确认」不足以在证据不齐时放行，
# 必须用更显式的「仍进PRD」类表态，避免用户无意确认即放行。 [C 2026-09-15]
_EVIDENCE_PASS_WORDS: frozenset[str] = frozenset(
    {
        "仍进prd",
        "仍然进prd",
        "确认放行",
        "强制放行",
    }
)

# 证据缺口二次 interrupt 的 reason 模板 [C 2026-09-15]
_EVIDENCE_GAP_REASON_TEMPLATE = (
    "探针证据不齐，未自动放行。请明确答复：「仍进PRD」强制放行；"
    "或「改判普通」「重塑」「放弃」走对应分支；"
    "或补充意见后再答复。空答复不视为放行"
    "（最多 3 轮，仍不明确则保持中断等待，不调模型、不空转升级）。"
)


def _is_evidence_pass_answer(text: str) -> bool:
    """归一化（strip + 去空白 + lower）后精确匹配二次确认的强制放行词。

    与首次 interrupt 的 _PASS_WORDS 区分：二次确认必须显式「仍进PRD」类词才放行，
    「通过」「确认」等普通通过词不再触发放行（与原来「空答复即放行」彻底断开）。
    """
    stripped = str(text if text is not None else "").strip()
    compact = re.sub(r"[\s\u3000]+", "", stripped.lower())
    return compact in _EVIDENCE_PASS_WORDS
    # [C 2026-09-15 by codebuddy-glm-5.2] S045 块4：二次确认强制放行词判定


def audit_probe_evidence(evidence: list) -> dict:
    """纯函数：审计探针证据列表的完整性，返回缺口字典（零 API 可测）。

    有效证据判据（沿用 ``_backfill_evidence_to_report`` 454–459 行口径）：
    ``actual_output`` 是非空字符串且不以 ``[执行失败]`` 开头。

    Returns:
        ``{"total", "valid", "judged", "unjudged", "failed", "invalid",
           "complete", "guidance"}``
        - ``total``: 证据条数；
        - ``valid``: 有效证据数（actual_output 非空字符串且不以 ``[执行失败]`` 开头）；
        - ``judged``: passed 非 None 的条数（含 True/False）；
        - ``unjudged``: passed is None 的探针名列表（口径 C：None 不阻断、转人工）；
        - ``failed``: passed is False 的探针名列表（判定已给出、交人判，不算"缺口"，
          只进 guidance 引导）；
        - ``invalid``: 实际输出无效（空/非字符串/以 ``[执行失败]`` 开头）的探针名列表；
        - ``complete``: total > 0 且 unjudged 为空 且 invalid 为空
          （failed 不计入 complete 的 True/False 判定，只进 guidance）；
        - ``guidance``: 缺口提示文案，由代码生成。
    """
    if not evidence:
        return {
            "total": 0,
            "valid": 0,
            "judged": 0,
            "unjudged": [],
            "failed": [],
            "invalid": [],
            "complete": False,
            "guidance": "本条需求未产生探针证据",
        }

    total = len(evidence)
    valid = 0
    judged = 0
    unjudged: list[str] = []
    failed: list[str] = []
    invalid: list[str] = []

    for ev in evidence:
        if not isinstance(ev, dict):
            continue
        pname = str(ev.get("probe_name") or "")
        actual_output = ev.get("actual_output")
        is_valid = (
            isinstance(actual_output, str)
            and bool(actual_output)
            and not actual_output.startswith("[执行失败]")
        )
        if is_valid:
            valid += 1
        else:
            if pname:
                invalid.append(pname)

        passed = ev.get("passed")
        if passed is None:
            if pname:
                unjudged.append(pname)
        elif passed is False:
            judged += 1
            if pname:
                failed.append(pname)
        elif passed is True:
            judged += 1
        # 非 bool 值不归入 judged（容错）

    complete = total > 0 and not unjudged and not invalid

    if failed:
        guidance = (
            f"探针 {', '.join(failed)} 实测未通过，"
            "建议重塑需求范围（回复「重塑」）"
        )
    elif unjudged:
        guidance = f"探针 {', '.join(unjudged)} 未给出判定，请人工确认"
    elif invalid:
        guidance = f"探针 {', '.join(invalid)} 实测输出无效，请人工确认"
    else:
        guidance = ""

    return {
        "total": total,
        "valid": valid,
        "judged": judged,
        "unjudged": unjudged,
        "failed": failed,
        "invalid": invalid,
        "complete": complete,
        "guidance": guidance,
    }
    # [C 2026-09-15 by codebuddy-glm-5.2] S045 块4：探针证据审计纯函数


def _evidence_audit_hint(audit: dict) -> str:
    """生成首次 interrupt 载荷里的一行显著提示文案。

    证据齐全且无 failed 时返回空串（不展示提示）；
    否则返回 ``⚠ 证据不齐：<guidance>`` 一行显著提示。
    """
    if audit["complete"] and not audit["failed"]:
        return ""
    return f"⚠ 证据不齐：{audit['guidance']}"
    # [C 2026-09-15 by codebuddy-glm-5.2] S045 块4：证据缺口显著提示


def _evidence_gap_reason(audit: dict) -> str:
    """生成二次 interrupt（status=evidence_gap）的 reason 文案。"""
    return f"{_EVIDENCE_GAP_REASON_TEMPLATE}（缺口：{audit['guidance']}）"
    # [C 2026-09-15 by codebuddy-glm-5.2] S045 块4：证据缺口 reason 文案


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


# ────────────────────────── 探针真跑 ReAct 循环（S043-b3）──────────────────────────


def _parse_react_results(content: object) -> list[dict] | None:
    """从模型 content 解析 ``{"results": [...]}`` JSON。失败返回 None。

    容错策略：复用 kernel.model.extract_json 做围栏剥离与 JSON 提取；
    解析失败、非 dict、缺 results 或 results 非 list 时返回 None。

    content 形态归一化：DeepSeek 思考模式经 ChatLiteLLM 返回的 AIMessage.content
    是分段列表（content blocks），形如
    ``[{"type": "thinking", "thinking": "..."}, {"type": "text", "text": "{...}"}]``，
    最终判定 JSON 在 text 块里；直接 str(content) 得到的是 Python repr（单引号、
    含 type/thinking 键），extract_json 必失败。故 content 为 list 时只拼接
    type=="text" 段（多段用 "\\n" 连接），thinking 段跳过；str 等其他形态维持原样。
    """
    if content is None:
        return None
    # [C 2026-09-14 by codebuddy-ds41flash] S043 块3 真机修复2：
    # DeepSeek 思考模式返回 content blocks，判定 JSON 在 text 块，thinking 段必须跳过
    if isinstance(content, list):
        text = "\n".join(
            seg.get("text", "")
            for seg in content
            if isinstance(seg, dict) and seg.get("type") == "text"
        ).strip()
    else:
        text = str(content).strip()
    if not text:
        return None
    try:
        data = extract_json(text)
    except NodeExecutionError:
        return None
    if not isinstance(data, dict):
        return None
    results = data.get("results")
    if not isinstance(results, list):
        return None
    return results
    # [C 2026-09-14 by S043-b3] ReAct 最终判定 JSON 解析（容错，失败返回 None）


def _merge_react_results(evidence: list[dict], results: list[dict]) -> None:
    """把模型最终判定（results 列表）合并到 evidence：按 probe_name 匹配，更新 passed/reason。

    只填补 ``passed is None`` 的项；模型已判定（passed 非 None）或执行失败项不动。
    """
    by_name = {e.get("probe_name", ""): e for e in evidence if e.get("probe_name")}
    for item in results:
        if not isinstance(item, dict):
            continue
        pname = item.get("probe_name") or ""
        if not pname or pname not in by_name:
            continue
        target = by_name[pname]
        if target.get("passed") is not None:
            # 已有判定（含执行失败的 False），不覆盖
            continue
        passed = item.get("passed")
        if isinstance(passed, bool):
            target["passed"] = passed
        reason = item.get("reason") or ""
        if reason and not target.get("reason"):
            target["reason"] = reason
    # [C 2026-09-14 by S043-b3] 合并模型最终判定到 evidence


def _run_react_probes(
    probe_plan: list[dict],
    chat_with_tools: Any,
    llm_text: Callable[..., str],
) -> list[dict]:
    """ReAct 循环执行探针，返回 evidence 列表。

    每条 evidence: ``{probe_name, prompt, actual_output, expected, passed, reason}``。
    - ``passed=True/False``：模型明确判定（或执行失败时直接 False）；
    - ``passed=None``：模型已调用 run_probe 但未给出最终判定（留在 evidence 待下游处理）；
    - 未执行的探针（probe_plan 中有但 ReAct 循环未调用）：``passed=False``，
      ``reason="探针未执行（ReAct 循环结束，模型未调用此探针）"``。

    Args:
        probe_plan: 报告里的探针列表（dict 格式，含 name/target_capability/prompts/steps/expected）。
        chat_with_tools: 已 ``bind_tools([RunProbeTool])`` 的 ChatLiteLLM（或测试替身），
            支持 ``invoke(messages) -> AIMessage``。
        llm_text: ``build_llm()`` 返回的闭包，``as_text=True`` 拿原文（用于真调模型跑探针 prompt）。
    """
    # 构造探针清单 JSON 供模型查阅
    probes_payload = [
        {
            "probe_name": p.get("name", ""),
            "target_capability": p.get("target_capability", ""),
            "prompts": p.get("prompts") or [],
            "steps": p.get("steps", ""),
            "expected": p.get("expected", ""),
        }
        for p in probe_plan
    ]
    probes_json = json.dumps(probes_payload, ensure_ascii=False, indent=2)

    system = SystemMessage(
        content=(
            "你是探针执行器。对每条探针，调用 run_probe 工具执行："
            "传入 probe_name、prompt（取探针 prompts[0]）、expected。"
            "工具会返回模型实际输出。你根据实际输出与 expected 对比，判定 pass/fail。"
            "所有探针执行完后，输出一段 JSON 汇总每条探针的判定结果，"
            '格式：{"results": [{"probe_name": "...", "passed": true/false, "reason": "..."}]}'
        )
    )
    human = HumanMessage(content=f"请执行以下探针：\n{probes_json}")

    messages: list = [system, human]
    evidence: list[dict] = []

    for _round_idx in range(MAX_REACT_ROUNDS):
        ai_msg = chat_with_tools.invoke(messages)
        messages.append(ai_msg)

        tool_calls = getattr(ai_msg, "tool_calls", None) or []
        if not tool_calls:
            # 模型不再调用工具：看 content 是否含最终判定 JSON
            content = getattr(ai_msg, "content", "")
            results = _parse_react_results(content)
            if results is not None:
                _merge_react_results(evidence, results)
            break

        # 处理本轮每个 tool_call
        for tc in tool_calls:
            tc_name = tc.get("name", "")
            tc_id = tc.get("id", "")
            if tc_name != "run_probe":
                # 不识别的工具：回错误消息让模型自行修正
                tool_msg = ToolMessage(
                    content=f"未知工具：{tc_name}",
                    tool_call_id=tc_id,
                )
                messages.append(tool_msg)
                continue

            args = tc.get("args") or {}
            probe_name = args.get("probe_name", "")
            probe_prompt = args.get("prompt", "")
            expected = args.get("expected", "")

            # 真调模型跑探针（用 llm_text，as_text=True 拿原文，不走绑了工具的 chat）
            try:
                actual_output = llm_text(probe_prompt, as_text=True)
            except Exception as exc:
                # 探针真调失败：标"执行失败"，不中断整条流水线
                actual_output = f"[执行失败] {exc}"
                evidence.append(
                    {
                        "probe_name": probe_name,
                        "prompt": probe_prompt,
                        "actual_output": actual_output,
                        "expected": expected,
                        "passed": False,
                        "reason": f"探针执行失败: {exc}",
                    }
                )
            else:
                # 暂存实际输出，待模型下一轮或最终 JSON 给出 pass/fail
                evidence.append(
                    {
                        "probe_name": probe_name,
                        "prompt": probe_prompt,
                        "actual_output": actual_output,
                        "expected": expected,
                        "passed": None,
                        "reason": "",
                    }
                )

            # 把工具调用结果作为 ToolMessage 发回模型
            tool_msg = ToolMessage(
                content=str(actual_output),
                tool_call_id=tc_id,
            )
            messages.append(tool_msg)
        # end for tc in tool_calls
    # end for round_idx in range(MAX_REACT_ROUNDS)

    # 未在 evidence 中出现的探针标记"未执行"
    executed_names = {
        e.get("probe_name", "") for e in evidence if e.get("probe_name")
    }
    for p in probe_plan:
        name = p.get("name", "")
        if name and name not in executed_names:
            evidence.append(
                {
                    "probe_name": name,
                    "prompt": "",
                    "actual_output": "",
                    "expected": p.get("expected", ""),
                    "passed": False,
                    "reason": "探针未执行（ReAct 循环结束，模型未调用此探针）",
                }
            )

    return evidence
    # [C 2026-09-14 by S043-b3] ReAct 循环执行探针（进程内 function calling，硬上限 8 轮）


def _backfill_evidence_to_report(report: dict, evidence: list[dict]) -> dict:
    """把探针证据回填到报告的 capability_matrix，并按规则降级绿色无证据项。

    匹配规则：通过 ``probe_plan.target_capability`` 关联 ``capability_matrix.capability``。
    降级规则：
    - ``final_status=绿`` 且 有 probe targeting it 但 无 evidence → 降级为黄，
      ``final_note`` 追加"无探针证据，自动降级为黄"；
    - ``final_status=红`` 且 无 evidence → 保持红（红本身不需要探针验证）；
    - 其他情况不降级（包括无 probe targeting 的绿能力点——探针方案本就不覆盖它）。
    """
    probe_plan = report.get("probe_plan") or []
    capability_matrix = report.get("capability_matrix") or []

    # capability -> 是否有 probe targeting it
    cap_has_probe: dict[str, bool] = {
        cap.get("capability", ""): False for cap in capability_matrix
    }
    for probe in probe_plan:
        target = probe.get("target_capability") or ""
        if target in cap_has_probe:
            cap_has_probe[target] = True

    # capability -> 是否有有效证据：只有真实调过模型且拿到有效输出的证据才算数。
    # 探针未执行（actual_output=""）或真调失败（actual_output="[执行失败] …"）都拿不到
    # 有效验证，绿点必须降黄；真跑成功无论模型后判 pass/fail 均算有证据（本函数不按
    # pass/fail 改色，现有行为不动）。
    # [C 2026-09-14 by codebuddy-ds41flash] S043 块3 真机修复：证据判据收紧
    cap_has_evidence: dict[str, bool] = {
        cap.get("capability", ""): False for cap in capability_matrix
    }
    # 先建 probe_name -> target_capability 映射
    probe_to_cap: dict[str, str] = {}
    for probe in probe_plan:
        name = probe.get("name") or ""
        target = probe.get("target_capability") or ""
        if name and target:
            probe_to_cap[name] = target
    for ev in evidence:
        actual_output = ev.get("actual_output")
        # actual_output 必须为非空字符串、且不以"[执行失败]"开头，才算有效证据
        if not isinstance(actual_output, str) or not actual_output:
            continue
        if actual_output.startswith("[执行失败]"):
            continue
        pname = ev.get("probe_name") or ""
        target = probe_to_cap.get(pname, "")
        if target in cap_has_evidence:
            cap_has_evidence[target] = True

    new_matrix = []
    for cap in capability_matrix:
        cap_name = cap.get("capability", "")
        final_status = cap.get("final_status", "")
        if (
            final_status == "绿"
            and cap_has_probe.get(cap_name, False)
            and not cap_has_evidence.get(cap_name, False)
        ):
            # 绿能力点有对应探针但无证据：降级为黄
            new_cap = dict(cap)
            new_cap["final_status"] = "黄"
            existing_note = cap.get("final_note", "")
            downgrade_note = "无探针证据，自动降级为黄"
            if existing_note:
                new_cap["final_note"] = f"{existing_note}；{downgrade_note}"
            else:
                new_cap["final_note"] = downgrade_note
            new_matrix.append(new_cap)
        else:
            new_matrix.append(dict(cap))

    new_report = dict(report)
    new_report["capability_matrix"] = new_matrix
    return new_report
    # [C 2026-09-14 by S043-b3] 证据回填 + 绿色无证据降级规则


# ────────────────────────── 候选池前置（S048）──────────────────────────
# 第 2 段在产可行性报告的同时产出候选池（2–5 个候选），由本节点代码补齐实时单价与
# 「本机已接入 / 需接入后验证」，供第 3 段 AI-native PRD「模型要求与切换条件」引用。
# 三条降级路径（清单缺失 / 价格脚本失败 / 候选为空）都不阻断流程，只在报告里记原因。
# [C 2026-09-16 by codebuddy-deepseek-v4.1-flash]

# 价格脚本硬超时（秒）：脚本自身远端取数默认 20 秒，留 10 秒余量 [C 2026-09-16]
PRICE_SCRIPT_TIMEOUT = 30

# price_source 三个取值（口径见任务书 3.1 第 3 条）
_PRICE_SOURCE_REMOTE = "远端实时"
_PRICE_SOURCE_BACKUP = "本地备份并标注可能已过期"
_PRICE_SOURCE_MISSING = "未取到"

# 接入状态两个取值
_ACCESS_READY = "本机已接入"
_ACCESS_PENDING = "需接入后验证"


def _now_text() -> str:
    """本机当前时间（与 scripts/model_catalog.py 的取数时间格式一致）。"""
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def _split_provider_id(provider_id: object) -> str:
    """litellm 调用格式拆出模型名：``deepseek/deepseek-chat`` -> ``deepseek-chat``。

    兼容配置侧的冒号写法（``deepseek:deepseek-chat``）；无分隔符时原样返回。
    """
    pid = str(provider_id or "").strip()
    if "/" in pid:
        return pid.rsplit("/", 1)[-1].strip()
    if ":" in pid:
        return pid.rsplit(":", 1)[-1].strip()
    return pid
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 provider_id 拆名


def _read_model_catalog(deps) -> tuple[str, str]:
    """读候选清单整份文本（第 2 段 prompt 的 ``model_catalog`` 输入）。

    Returns:
        ``(文本, 降级原因)``：读到时原因为空串；读不到时文本为空串并给出原因
        （配置缺失 / 文件不存在 / 读取异常 / 内容为空），由节点写进
        ``feasibility_report["candidate_pool_note"]``，不阻断流程。
    """
    cfg = getattr(deps, "model_catalog", None)
    path = cfg.get("path") if isinstance(cfg, dict) else None
    if not path:
        return "", "未配置候选清单路径（config model_catalog.path 缺失）"
    target = Path(str(path))
    if not target.is_file():
        return "", f"候选清单文件不存在：{target}"
    try:
        text = target.read_text(encoding="utf-8-sig")
    except Exception as exc:  # 权限/编码等一律降级，不阻断
        return "", f"候选清单读取失败：{type(exc).__name__}: {exc}"
    if not text.strip():
        return "", f"候选清单内容为空：{target}"
    return text, ""
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 候选清单整份读取（失败降级）


def _configured_candidates(deps) -> list[dict]:
    """取本机已接入的候选（config ``bake_off.candidates`` 的 id / label / kind）。

    未配置该段或字段缺失时返回空列表（prompt 里该节不渲染）。
    """
    cfg = getattr(deps, "bake_off_config", None)
    items = cfg.get("candidates") if isinstance(cfg, dict) else None
    result: list[dict] = []
    for item in items or []:
        if not isinstance(item, dict) or not item.get("id"):
            continue
        result.append(
            {
                "id": str(item.get("id", "")),
                "label": str(item.get("label", "")),
                "kind": str(item.get("kind", "")),
            }
        )
    return result
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 已接入候选清单（config bake_off）


def _configured_id_keys(deps) -> set[str]:
    """把 config ``bake_off.candidates`` 的 id 归一化成可比集合。

    配置用小写冒号（``deepseek:deepseek-chat``），候选池用 litellm 斜杠格式
    （``deepseek/deepseek-chat``），两侧都归一到「小写 + 冒号转斜杠」，并额外收
    模型名（斜杠后一段）与冒号后一段，避免因分隔符不同误判「需接入后验证」。
    """
    keys: set[str] = set()
    for item in _configured_candidates(deps):
        cid = item["id"].strip().lower()
        if not cid:
            continue
        slashed = cid.replace(":", "/")
        keys.add(cid)
        keys.add(slashed)
        keys.add(slashed.rsplit("/", 1)[-1])
        keys.add(cid.rsplit(":", 1)[-1])
    return keys
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 接入判定用 id 归一化


def _is_configured(provider_id: str, deps) -> bool:
    """候选是否命中本机已接入清单（config ``bake_off.candidates``）。"""
    keys = _configured_id_keys(deps)
    if not keys:
        return False
    return (
        provider_id.strip().lower() in keys
        or _split_provider_id(provider_id).lower() in keys
    )
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 已接入判定


def _fetch_candidate_prices(lookup_keys: list[str], price_script: str | None) -> dict:
    """调价格脚本取实时单价，返回取数结果（任何失败都不抛异常，转成原因字段）。

    调用形态：``sys.executable <price_script> price <取价键...> --json``，硬超时 30 秒。

    Returns:
        ``{"ok", "error", "source", "source_label", "fetched_at",
           "stale_warning", "rows"}``
        - ``rows``: ``{取价键: 该行 dict}``（键为传入的取价键，脚本按同序回行）；
        - ``error`` 非空表示整次取数失败，此时 ``rows`` 为空、逐条记「未取到」。
    """
    meta: dict = {
        "ok": False,
        "error": "",
        "source": "",
        "source_label": "",
        "fetched_at": "",
        "stale_warning": "",
        "rows": {},
    }
    if not lookup_keys:
        return meta
    if not price_script:
        meta["error"] = "未配置价格脚本路径（config model_catalog.price_script 缺失）"
        return meta

    # 调用形态：sys.executable <price_script> price <取价键...> --json（price 是取价子命令）
    cmd = [sys.executable, str(price_script), "price", *lookup_keys, "--json"]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=PRICE_SCRIPT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        meta["error"] = f"价格脚本执行超时（{PRICE_SCRIPT_TIMEOUT} 秒）"
        return meta
    except Exception as exc:  # 找不到解释器/脚本等
        meta["error"] = f"价格脚本执行异常：{type(exc).__name__}: {exc}"
        return meta

    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()
        detail = tail[-1] if tail else ""
        meta["error"] = f"价格脚本退出码 {proc.returncode}"
        if detail:
            meta["error"] = f"{meta['error']}：{detail[:200]}"
        return meta

    try:
        data = json.loads(proc.stdout or "")
    except (TypeError, ValueError) as exc:
        meta["error"] = f"价格脚本输出不是合法 JSON：{exc}"
        return meta
    if not isinstance(data, dict):
        meta["error"] = "价格脚本输出不是 JSON 对象"
        return meta
    rows = data.get("models")
    if not isinstance(rows, list):
        meta["error"] = "价格脚本输出缺少 models 列表"
        return meta

    meta["ok"] = True
    meta["source"] = str(data.get("source", ""))
    meta["source_label"] = (
        _PRICE_SOURCE_REMOTE
        if meta["source"] == "remote"
        else _PRICE_SOURCE_BACKUP
        if meta["source"] == "local_backup"
        else ""
    )
    meta["fetched_at"] = str(data.get("fetched_at", "") or "")
    meta["stale_warning"] = str(data.get("stale_warning", "") or "")
    for row in rows:
        if isinstance(row, dict) and row.get("model_id"):
            meta["rows"][str(row["model_id"])] = row
    return meta
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 价格脚本调用（超时/退出码/JSON 全降级）


def _row_price_missing(row: dict) -> bool:
    """价格行是否「表内收录但没给单价」（脚本缺价时字段值是 ``"-"``）。"""
    for key in ("input_per_million", "output_per_million"):
        value = row.get(key)
        if value is None:
            continue
        if isinstance(value, str) and value.strip() in ("", "-"):
            continue
        return False
    return True
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 缺价行判定（缺价≠免费，须另行核实）


def _enrich_candidate(candidate: dict, price_meta: dict, configured: bool) -> dict:
    """给一条候选补五个字段：price / price_source / price_fetched_at / price_note / access_status。

    取价按两个键依次找：先拆名后的模型名（``deepseek-chat``），再完整的 provider_id
    （``zai/glm-5.3-flash``）——litellm 表两种键形态并存，只用一个键会把有价的型号
    误报成「表内未收录」。
    """
    enriched = dict(candidate)
    provider_id = str(candidate.get("provider_id", "") or "").strip()
    name = _split_provider_id(provider_id)
    rows = price_meta.get("rows") or {}
    row: dict | None = None
    for key in (name, provider_id):
        if not key:
            continue
        candidate_row = rows.get(key)
        if isinstance(candidate_row, dict) and candidate_row.get("found"):
            row = candidate_row
            break
        if row is None and isinstance(candidate_row, dict):
            # 记下「表内未收录」那行，用于写价格说明
            row = candidate_row
    lookup_key = name or provider_id
    error = str(price_meta.get("error", "") or "")
    fetched_at = str(price_meta.get("fetched_at", "") or "")

    if isinstance(row, dict) and row.get("found"):
        enriched["price"] = {
            "input_per_million": row.get("input_per_million"),
            "output_per_million": row.get("output_per_million"),
            "found": True,
        }
        enriched["price_source"] = str(price_meta.get("source_label", "") or "")
        enriched["price_fetched_at"] = fetched_at
        # 本地备份取数时把「可能已过期」原样带给下游，远端取数无提示；
        # 表内收录但缺单价的（脚本给 "-"）另记一行，缺价不等于免费。
        note = str(price_meta.get("stale_warning", "") or "")
        if _row_price_missing(row):
            missing = "表内无单价，需另行核实（不得用同类模型价格替估）"
            note = f"{note}；{missing}" if note else missing
        enriched["price_note"] = note
    else:
        enriched["price"] = {
            "input_per_million": None,
            "output_per_million": None,
            "found": False,
        }
        enriched["price_source"] = _PRICE_SOURCE_MISSING
        enriched["price_fetched_at"] = fetched_at or _now_text()
        if error:
            enriched["price_note"] = f"价格未取到：{error}"
        elif isinstance(row, dict):
            enriched["price_note"] = (
                f"表内未收录 {lookup_key}，需另行核实（不得用同类模型价格替估）"
            )
        else:
            enriched["price_note"] = f"价格脚本未返回 {lookup_key} 的取数结果"

    enriched["access_status"] = _ACCESS_READY if configured else _ACCESS_PENDING
    return enriched
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 候选逐条补字段


def build_model_candidates(report: dict, deps) -> tuple[list[dict], str]:
    """产 state["model_candidates"]：拆分 provider_id 取价 + 补接入状态。

    取价键为每个候选的「拆名后的模型名」与「完整 provider_id」两种（去重、保序），
    因为 litellm 表的键两种形态并存（``deepseek-chat`` 是裸键、``zai/glm-5.3-flash``
    带 provider 前缀），只传一种会把有价的型号误报成未收录。

    Returns:
        ``(候选列表, 降级原因)``：原因为空串表示一切正常；非空表示候选池为空
        （模型未产出或产出为空），由节点记进 ``feasibility_report["candidate_pool_note"]``。
    """
    raw = report.get("model_candidates") or []
    candidates = [c for c in raw if isinstance(c, dict)]
    if not candidates:
        return [], "模型未产出候选池（或产出为空），本次无候选模型"

    # 取价键：每个候选都试「拆名后的模型名」与「完整 provider_id」两种键（去重、保序）——
    # litellm 表两种键形态并存（deepseek-chat 是裸键，zai/glm-5.3-flash 带 provider 前缀）
    lookup_keys: list[str] = []
    for cand in candidates:
        provider_id = str(cand.get("provider_id", "") or "").strip()
        for key in (_split_provider_id(provider_id), provider_id):
            if key and key not in lookup_keys:
                lookup_keys.append(key)

    catalog_cfg = getattr(deps, "model_catalog", None)
    price_meta = _fetch_candidate_prices(
        lookup_keys,
        catalog_cfg.get("price_script") if isinstance(catalog_cfg, dict) else None,
    )
    return [
        _enrich_candidate(c, price_meta, _is_configured(str(c.get("provider_id", "")), deps))
        for c in candidates
    ], ""
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 候选池合并（取价 + 接入判定）


def make_feasibility_check(deps):
    """可行性报告节点工厂：返回签名 (state: dict) -> dict 的节点函数。

    阶段 1：调模型生成可行性报告（三方对照表+探针方案+风险+成本+候选池），写入
    state["feasibility_report"]。
    阶段 2：自动跑探针——进程内 function calling ReAct 循环执行 probe_plan，
    真调模型拿实际输出，让模型对照 expected 判定 pass/fail，采集 evidence 列表。
    阶段 3：证据回填报告——按 target_capability 匹配，绿能力点有对应探针但无证据
    时自动降级为黄，红能力点无证据保持红。
    阶段 4：候选池前置——给报告的 model_candidates 补实时单价与接入状态，写
    state["model_candidates"]（S048，第 3 段 PRD 与第 6 段对比选型引用）。
    """

    def feasibility_check(state: dict) -> dict:
        # [C 2026-09-14 by S043-b1] 注入工具能力清单供 prompt 渲染
        state = {**state, "tool_catalog": deps.tool_catalog or []}
        # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048：注入候选清单整份文本 +
        # 本机已接入候选（config bake_off.candidates），供模型产出候选池；清单读不到时
        # 该节整块不渲染（空串），原因在后面写进 feasibility_report["candidate_pool_note"]
        catalog_text, catalog_note = _read_model_catalog(deps)
        state = {
            **state,
            "model_catalog": catalog_text,
            "configured_candidates": _configured_candidates(deps),
        }
        prompt = deps.registry.read_prompt("feasibility_check")
        schema = deps.registry.load_schema("feasibility")
        spec = NodeSpec(
            name="feasibility_check",
            prompt_template=prompt,
            output_schema=schema,
        )
        # 阶段 1：调模型产报告
        report = deps.runner.run_raw(spec, state)

        # 阶段 2：自动跑探针（ReAct 循环）
        # build_chat/build_llm 报错走 interrupt，等用户修复后重跑本节点
        while True:
            try:
                chat = build_chat()
                llm_text = build_llm()
                chat_with_tools = chat.bind_tools([RunProbeTool])
            except Exception as exc:
                interrupt(
                    {
                        "node": "feasibility_check",
                        "status": "tool_error",
                        "reason": f"模型工厂或工具绑定失败: {exc}",
                        "requirement_name": state.get("requirement_name", ""),
                    }
                )
                # 用户修复后恢复，重试 build_chat/build_llm/bind_tools
                continue

            probe_plan = report.get("probe_plan") or []
            try:
                # 编排模型 chat.invoke 异常（网络/网关错误）同样走 interrupt，不冒泡崩节点。
                # 探针级 llm_text 异常已在 _run_react_probes 内部标"执行失败"，不冒泡到这里。
                # [C 2026-09-14 by codebuddy-ds41flash] S043 块3 缺口 B：编排模型 invoke 异常走 interrupt
                evidence = _run_react_probes(probe_plan, chat_with_tools, llm_text)
            except Exception as exc:
                interrupt(
                    {
                        "node": "feasibility_check",
                        "status": "tool_error",
                        "reason": f"探针执行循环失败（编排模型调用异常）: {exc}",
                        "requirement_name": state.get("requirement_name", ""),
                    }
                )
                # 用户修复后恢复，重试 build_chat/build_llm/bind_tools + 探针循环
                continue
            break

        # 阶段 3：证据回填 + 降级规则
        report = _backfill_evidence_to_report(report, evidence)

        # 阶段 4（S048）：候选池前置——补实时单价与接入状态，写 state["model_candidates"]；
        # 候选清单读不到 / 价格脚本失败 / 候选为空三条降级路径都只记原因，不阻断流程。
        model_candidates, candidate_note = build_model_candidates(report, deps)
        pool_notes = [n for n in (catalog_note, candidate_note) if n]
        if pool_notes:
            report = {**report, "candidate_pool_note": "；".join(pool_notes)}

        return {
            "feasibility_report": report,
            "feasibility_evidence": evidence,
            "model_candidates": model_candidates,
        }
        # [C 2026-09-14 by S043-b3] 探针真跑：ReAct 循环 + 证据回填 + 绿色无证据降级
        # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048：候选池前置（取价 + 接入状态 + 降级记原因）

    return feasibility_check


def make_feasibility_confirm(deps):  # noqa: ARG001 - 工厂签名与其他节点保持一致，本节点不调模型/不取依赖
    """确认AI可行性门节点工厂：返回签名 (state: dict) -> dict 的节点函数（不调模型）。

    交互协议（首次中断 status="draft"）：
    - pass（含自由文本补充意见）-> 先看 ``evidence_audit["complete"]``：
        证据齐全（complete=True）-> 只追加 human_feedback、写 verdict=pass
          -> 条件边去 prd_generation（[r2]：含 failed 不再阻断，只靠 guidance/hint 提示）；
        证据不齐（complete=False）-> **不放行**，进二次 interrupt（status="evidence_gap"）；
    - reclassify -> 写 verdict=reclassify、ai_core=False -> 条件边去 prd_generation；
    - reshape（feasibility_reshape_count=0）-> 计数 +1、写 verdict=reshape
      -> 条件边回 requirement_confirm 调整范围（限 1 次）；
    - reshape（计数已达上限）-> 先 interrupt 升级暂停（status="escalated"），
      二次答复按 通过/改判普通/放弃 分流；再次要求重塑则按通过处理（不再回第 1 段）；
    - abandon -> 写 verdict=abandon -> 条件边到 END，流程结束。

    二次 interrupt（status="evidence_gap"）协议（[r2] 放宽）：
    - 命中 ``_EVIDENCE_PASS_WORDS``（仍进PRD/仍然进PRD/确认放行/强制放行）-> verdict=pass，留痕；
    - 普通通过词（classify_feasibility_answer 判为 "pass" 且非空，如「通过」「确认」）
      -> verdict=pass，留痕；
    - 其他显式文本（feedback）-> verdict=pass，留痕（"只要用户给出显式答复就放行"）；
    - reclassify/reshape/abandon -> 走对应分支（reshape 仍受 ``MAX_FEASIBILITY_RESHAPE`` 约束，
      超限进 escalation_stop）；
    - 空答复 -> 再问一轮（保持中断，不调模型、不空转升级）。
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
        # S045 块4：审计探针证据完整性，缺口随首次 interrupt 载荷透传给用户
        evidence = state.get("feasibility_evidence") or []
        audit = audit_probe_evidence(evidence)
        audit_hint = _evidence_audit_hint(audit)

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

        def handle_reshape(text: str, round_label: str) -> dict:
            """处理 reshape 答复：超限进 escalation_stop，否则计数 +1 写 verdict=reshape。

            统一覆盖首次 draft 与二次 evidence_gap 两处 reshape 分支，保证重塑额度
            约束在两条路径上一致（超限都进 escalation_stop）。
            """
            if reshape_count >= MAX_FEASIBILITY_RESHAPE:
                return escalation_stop(text)
            append_log("reshape", text, f"{round_label}-reshape")
            return {
                "feasibility_confirm": {"verdict": "reshape", "user_feedback": text},
                "feasibility_reshape_count": reshape_count + 1,
                "human_feedback": feedback_log,
            }

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

        def evidence_gap_loop(first_text: str) -> dict:
            """证据不齐二次确认循环（[r2] 放宽放行口径）。

            人工驱动的暂停不是空转：每轮都在等真人输入、不调模型、不自动升级。

            放行口径（[r2] 放宽）：
            - 命中 ``_EVIDENCE_PASS_WORDS``（仍进PRD 等）-> verdict=pass，留痕；
            - 普通通过词（classify_feasibility_answer 判为 "pass" 且非空，如「通过」「确认」）
              -> verdict=pass，留痕；
            - 其他显式文本（feedback 类）-> verdict=pass，留痕
              （"只要用户给出显式答复就放行"）；
            - reclassify/reshape/abandon -> 走对应分支
              （reshape 仍受 ``MAX_FEASIBILITY_RESHAPE`` 约束，超限进 escalation_stop）；
            - **空答复** -> 不放行，再问一轮（保持中断，不调模型、不空转升级）。
            """
            round_idx = 0
            while True:
                round_idx += 1
                answer = interrupt(
                    {
                        "node": "feasibility_confirm",
                        "status": "evidence_gap",
                        "requirement_name": requirement_name,
                        "feasibility_report": report,
                        "feasibility_reshape_count": reshape_count,
                        "evidence_audit": audit,
                        "evidence_audit_hint": audit_hint,
                        "reason": _evidence_gap_reason(audit),
                        "prior_feedbacks": _prior_feasibility_feedbacks(state),
                        "first_answer": first_text,
                        "round": round_idx,
                    }
                )

                # 命中强制放行词（仍进PRD 等）→ verdict=pass，留痕注明人工放行
                if _is_evidence_pass_answer(answer):
                    note = (
                        f"证据不齐，经人工放行；原答复：{first_text}；二次答复：{answer}"
                    )
                    return decision_update("pass", note, "evidence-gap-pass")

                kind2, text2 = _normalize_answer(answer)

                if kind2 == "reclassify":
                    return decision_update(
                        "reclassify",
                        text2,
                        "evidence-gap-reclassify",
                        {"ai_core": False},
                    )
                if kind2 == "abandon":
                    return decision_update("abandon", text2, "evidence-gap-abandon")
                if kind2 == "reshape":
                    # reshape 仍受 MAX_FEASIBILITY_RESHAPE 约束，超限进 escalation_stop
                    return handle_reshape(text2, "evidence-gap")

                # [r2] 放宽：只要用户给出显式答复就放行，只有空答复才继续等待。
                # - kind2 == "pass" 且 text2 非空（普通通过词如「通过」「确认」）→ 放行；
                # - kind2 == "feedback"（其他显式文本）→ 放行；
                # - 空答复（text2 为空）→ 再问一轮（保持中断，不调模型、不空转升级）。
                if not text2.strip():
                    continue

                note = (
                    f"证据不齐，经人工放行；原答复：{first_text}；二次答复：{text2}"
                )
                return decision_update("pass", note, "evidence-gap-pass")
            # [C 2026-09-15 by codebuddy-glm-5.2 r2] S045 块4 r2：二次确认放行口径放宽

        # ── 首次中断：请用户审阅可行性报告并录入探针实测结论 ──
        first_answer = interrupt(
            {
                "node": "feasibility_confirm",
                "status": "draft",
                "requirement_name": requirement_name,
                "feasibility_report": report,
                "feasibility_reshape_count": reshape_count,
                "evidence_audit": audit,
                "evidence_audit_hint": audit_hint,
            }
        )
        kind, text = _normalize_answer(first_answer)

        if kind == "reshape":
            return handle_reshape(text, "draft")

        if kind == "reclassify":
            return decision_update(
                "reclassify", text, "draft-reclassify", {"ai_core": False}
            )

        if kind == "abandon":
            return decision_update("abandon", text, "draft-abandon")

        # pass / feedback：先看证据完整性
        # [r2] 放宽闸门：只看 complete，不看 failed。
        # 证据齐全（所有探针有有效 actual_output 且 passed 非 None）→ 直接 pass；
        # 有 failed 探针不再阻断，只靠 evidence_audit.guidance / evidence_audit_hint
        # 提示用户「建议重塑」，代码不拦、不改判。
        if audit["complete"]:
            return decision_update("pass", text, "draft-pass")
        return evidence_gap_loop(text)

    return feasibility_confirm
    # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门：四态分类/重塑限 1 次/升级暂停
    # [C 2026-09-15 by codebuddy-glm-5.2] S045 块4：证据必填二次确认 + 段名落地
    # [C 2026-09-15 by codebuddy-glm-5.2 r2] S045 块4 r2：闸门放宽（complete-only）+ 二次确认放行口径放宽


# [C 2026-09-12 by codebuddy-ds41flash] nodes/feasibility.py 新增完成
