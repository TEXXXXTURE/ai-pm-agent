# CLI 前端 - HITL 交互模块
"""人机交互（Human-in-the-Loop）处理：打印决策材料、接收输入、恢复执行。"""
from __future__ import annotations

import json
from typing import Any

from langgraph.types import Command
from rich.console import Console
from rich.panel import Panel
from rich.pretty import Pretty

console = Console()

# 首版需要展示给用户的决策材料字段（对齐 workflow-design 第二节）
DECISION_MATERIAL_FIELDS: tuple[str, ...] = (
    "raw_requirement",
    "requirement_name",
    "info_completeness",
    "confirmed_requirement",
    "ai_triage",  # 确认门展示 AI 适用性分流建议
    "ai_core",  # 确认门后展示用户拍板的分流结果
    "ai_feasibility",
    # 判断需求与 AI 的边界：展示可行性报告与确认门结论
    "feasibility_report",
    "feasibility_confirm",
    "proceed_decision",
    # 第 5 段确认评测体系门：展示评测体系草案与确认结论
    "eval_system",
    "eval_confirm",
    "issue_plan",  # 工单确认门：展示工单清单草案供用户审阅
    "launch_plan",  # 发布计划确认门：展示发布计划草案供用户审阅
    "eval_report",  # 第 8 段评测执行门：展示评测报告
    "model_selection",  # 第 6 段对比选型：展示选型结论
    # 需求修订整合节点：展示草案/计数/结论
    "requirement_draft",
    "requirement_refine_count",
    "requirement_refine_result",
)


def _extract_interrupt_value(interrupt: Any) -> Any:
    """从 interrupt 对象中提取 value，兼容对象属性与字典两种形态。"""
    if hasattr(interrupt, "value"):
        return interrupt.value
    if isinstance(interrupt, dict):
        return interrupt.get("value")
    return interrupt


# dict 载荷中需要额外向用户 recap 的材料字段（跳过空值）
PAYLOAD_RECAP_FIELDS: tuple[str, ...] = (
    "requirement_name",
    "raw_requirement",
    "info_completeness",
    "ai_triage",  # 确认门中断载荷携带 AI 分流建议
    # 确认AI可行性门中断载荷携带可行性报告与结论
    "feasibility_report",
    "feasibility_confirm",
    # 确认AI可行性门中断载荷携带证据审计与缺口提示
    "evidence_audit",
    "evidence_audit_hint",
    # 确认评测体系门中断载荷携带评测体系草案
    "eval_system",
    # 出题质量：确认门载荷携带
    # 机械检查结果（哪道题的材料或评分方式可能有问题），只提示不阻断
    "eval_quality",
    "issue_plan",  # 工单确认门中断载荷携带工单草案
    "launch_plan",  # 发布计划确认门中断载荷携带计划草案
    # 第 8 段评测执行门中断载荷携带评测报告
    # （eval_failed 载荷 report/eval_report；await_prompt/tool_error 载荷无此键自动跳过）
    "eval_report",
    # await_prompt 载荷携带待放入的 system_prompt.txt 路径
    "prompt_path",
    # 第 6 段对比选型门中断载荷携带候选清单与选型结论
    "candidates",
    # 第二块：第 6 段 await_decision
    # 载荷携带第 2 段候选池（全字段：角色/理由/接入代价/已知限制/单价/取数来源/接入状态）
    # 与「本次可实跑的候选」一行（= 候选池 ∩ 本机已接入）
    "model_candidates",
    "runnable_candidates",
    "model_selection",
    # 整合节点中断载荷携带草案/结论/计数
    "requirement_draft",
    "requirement_refine_result",
    "requirement_refine_count",
    # 变化体检：整合节点载荷携带
    # 草案变化/缩水/意见重复提示（只提示不拦流程）
    "draft_progress",
    # 就绪度打分：launch_confirm 载荷携带打分结果
    "readiness_assessment",
    # 升级暂停信息透传：status=draft/escalated、暂停原因、历轮意见；
    # 仅 issue_confirm / launch_confirm 载荷携带这些键，其他节点载荷无键自动跳过，不污染其 recap
    "status",
    "reason",
    "prior_feedbacks",
)


def handle_hitl(graph: Any, config: dict, interrupt_value: Any) -> None:
    """处理单次 HITL 中断：打印材料 -> 接收输入 -> 恢复执行。

    Args:
        graph: 已编译的 LangGraph 实例
        config: 含 configurable.thread_id 的运行配置
        interrupt_value: interrupt() 传出的值；dict 载荷（M6 起）含
            node / requirement_name / raw_requirement / info_completeness /
            capability_boundary；字符串载荷（旧行为）为节点名。
    """
    if isinstance(interrupt_value, dict):
        node_name = str(interrupt_value.get("node", "hitl"))
    else:
        node_name = str(interrupt_value) if interrupt_value is not None else "unknown"

    # 1. 打印分隔线与节点名
    console.print()
    console.rule(f"[bold yellow]⚠ 需要你确认的地方: {node_name}[/bold yellow]")

    # 2. dict 载荷：节点尚未返回 state，capability_boundary 等材料只在载荷里，先打印
    if isinstance(interrupt_value, dict):
        _print_payload_recap(interrupt_value)

    # 3. 打印 state 中已有的决策材料
    state = graph.get_state(config).values
    _print_decision_materials(state, node_name)
    # handle_hitl 兼容 dict 载荷并 Pretty 打印 recap 材料

    # 4. 等待用户输入
    # 空答复不当作确认——不 resume，停在本节点等下一句
    console.print()
    while True:
        user_input = console.input(
            f"[bold cyan]请输入对「{node_name}」的确认/反馈: [/bold cyan]"
        )
        if user_input.strip():
            break
        console.print("[dim]没收到答复，仍在这里等你的决定[/dim]")

    console.print(f"[dim]已提交输入: {user_input!r}[/dim]")

    # 5. 用 Command(resume=...) 恢复执行，并流式打印后续节点进度
    for chunk in graph.stream(Command(resume=user_input), config, stream_mode="updates"):
        for node_name in chunk:
            console.print(f"[bold cyan]▶ 执行节点:[/bold cyan] {node_name}")


def _print_payload_recap(payload: dict) -> None:
    """用 rich Pretty 打印 interrupt 载荷中携带的 recap 材料（跳过空值）。

    HITL 中断时节点尚未返回，ai_triage 等材料只存在于载荷中，
    state 里还没有，因此需要单独打印载荷。
    """
    console.print(
        Panel.fit("[bold]需求确认材料[/bold]", title="HITL recap", border_style="magenta")
    )
    # 标题保留"需求确认材料"措辞，
    # 因 ai_triage 与旧 capability_boundary 都是需求确认门的 recap 材料
    for field in PAYLOAD_RECAP_FIELDS:
        value = payload.get(field)
        if value in (None, "", {}, []):
            continue
        console.print(f"[bold cyan]{field}[/bold cyan]:")
        if isinstance(value, (dict, list)):
            try:
                console.print(Pretty(value, expand_all=True))
            except Exception:
                console.print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            console.print(f"  {value}")


def _print_decision_materials(state: dict, node_name: str) -> None:
    """打印当前 state 中的决策材料关键字段。"""
    console.print(
        Panel.fit(
            f"[bold]节点[/bold]: {node_name}\n"
            f"[bold]initiative_id[/bold]: {state.get('initiative_id', '-')}",
            title="当前上下文",
            border_style="yellow",
        )
    )

    has_material = False
    for field in DECISION_MATERIAL_FIELDS:
        value = state.get(field)
        # 跳过空值/默认值，只打印有意义的材料
        if value in (None, "", {}, []):
            continue
        has_material = True
        console.print(f"[bold cyan]{field}[/bold cyan]:")
        if isinstance(value, (dict, list)):
            try:
                console.print(Pretty(value, expand_all=True))
            except Exception:
                console.print(json.dumps(value, ensure_ascii=False, indent=2))
        else:
            console.print(f"  {value}")

    if not has_material:
        console.print(
            "[dim]（首版节点为占位实现，决策材料字段暂为空；"
            "接入完整流程后会展示结构化信息）[/dim]"
        )


def collect_interrupts(graph: Any, config: dict) -> list[Any]:
    """从 graph 状态中收集所有未处理的 interrupt 值。

    返回 interrupt value 列表；若无中断返回空列表。
    """
    state_snapshot = graph.get_state(config)
    interrupts: list[Any] = []
    tasks = getattr(state_snapshot, "tasks", None) or []
    for task in tasks:
        task_interrupts = getattr(task, "interrupts", None) or []
        for intr in task_interrupts:
            interrupts.append(_extract_interrupt_value(intr))
    return interrupts


