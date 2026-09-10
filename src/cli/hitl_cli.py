# [C 2026-09-08] M3 CLI 前端 - HITL 交互模块
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
    "capability_boundary",
    "ai_feasibility",
    "proceed_decision",
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
    "capability_boundary",
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
    console.rule(f"[bold yellow]⚠ HITL 中断节点: {node_name}[/bold yellow]")

    # 2. dict 载荷：节点尚未返回 state，capability_boundary 等材料只在载荷里，先打印
    if isinstance(interrupt_value, dict):
        _print_payload_recap(interrupt_value)

    # 3. 打印 state 中已有的决策材料
    state = graph.get_state(config).values
    _print_decision_materials(state, node_name)
    # [C 2026-09-09] M6 handle_hitl 兼容 dict 载荷并 Pretty 打印 recap 材料

    # 4. 等待用户输入
    console.print()
    user_input = console.input(
        f"[bold cyan]请输入对「{node_name}」的确认/反馈（直接回车表示确认通过）: [/bold cyan]"
    )
    if not user_input.strip():
        user_input = "confirmed"

    console.print(f"[dim]已提交输入: {user_input!r}[/dim]")

    # 5. 用 Command(resume=...) 恢复执行，并流式打印后续节点进度
    for chunk in graph.stream(Command(resume=user_input), config, stream_mode="updates"):
        for node_name in chunk:
            console.print(f"[bold cyan]▶ 执行节点:[/bold cyan] {node_name}")


def _print_payload_recap(payload: dict) -> None:
    """用 rich Pretty 打印 interrupt 载荷中携带的 recap 材料（跳过空值）。

    HITL 中断时节点尚未返回，capability_boundary 等材料只存在于载荷中，
    state 里还没有，因此需要单独打印载荷。
    """
    console.print(
        Panel.fit("[bold]需求确认材料（中断载荷）[/bold]", title="HITL recap", border_style="magenta")
    )
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
            "M4 接入真实节点后将展示结构化信息）[/dim]"
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


# [C 2026-09-08] hitl_cli.py 实现完成
