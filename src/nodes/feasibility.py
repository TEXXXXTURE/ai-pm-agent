# [C 2026-09-12 by codebuddy-ds41flash] 验证AI可行性节点（feasibility_check + feasibility_confirm HITL）
# [C 2026-09-14 by S043-b3] feasibility_check 重写：探针真跑（进程内 function calling ReAct）
"""验证AI可行性：流水线生成可行性报告（含探针方案）-> 自动跑探针采集证据 -> 人工在确认门录入结论。

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

import json
import re
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


def make_feasibility_check(deps):
    """可行性报告节点工厂：返回签名 (state: dict) -> dict 的节点函数。

    阶段 1：调模型生成可行性报告（三方对照表+探针方案+风险+成本+结论），写入
    state["feasibility_report"]。
    阶段 2：自动跑探针——进程内 function calling ReAct 循环执行 probe_plan，
    真调模型拿实际输出，让模型对照 expected 判定 pass/fail，采集 evidence 列表。
    阶段 3：证据回填报告——按 target_capability 匹配，绿能力点有对应探针但无证据
    时自动降级为黄，红能力点无证据保持红。
    """

    def feasibility_check(state: dict) -> dict:
        # [C 2026-09-14 by S043-b1] 注入工具能力清单供 prompt 渲染
        state = {**state, "tool_catalog": deps.tool_catalog or []}
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

        return {
            "feasibility_report": report,
            "feasibility_evidence": evidence,
        }
        # [C 2026-09-14 by S043-b3] 探针真跑：ReAct 循环 + 证据回填 + 绿色无证据降级

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
