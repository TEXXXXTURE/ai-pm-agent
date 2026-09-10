# [C 2026-09-08] M3 CLI 前端 - 命令行入口
"""AI PM Agent CLI 入口：启动 Agent、多轮对话、HITL 交互、断点恢复。"""
from __future__ import annotations

import argparse
import sys
import uuid
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.panel import Panel

from components.registry import ComponentRegistry
from kernel.artifact import ArtifactManager
from kernel.config import PROJECT_ROOT, load_config
from kernel.graph import build_graph
from kernel.model import build_llm
from kernel.runner import NodeRunner
from kernel.state import default_state
from kb.store import KBStore
from nodes import NodeDeps

from cli.hitl_cli import collect_interrupts, handle_hitl
from cli.output import print_artifact_paths

console = Console()


def _resolve_path(path: str) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p
    # [C 2026-09-09] M6 路径统一以 kernel.config.PROJECT_ROOT 为基准


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="ai-pm-agent",
        description="AI PM Agent — 从需求到 PRD 的智能产品经理助手",
    )
    parser.add_argument(
        "--requirement",
        type=str,
        help="用户原始需求描述（与 --resume 互斥）",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="需求名（用作产物文件夹名）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="config.yaml 路径（默认项目根目录 config.yaml）",
    )
    parser.add_argument(
        "--resume",
        type=str,
        metavar="THREAD_ID",
        help="断点恢复：传入 thread_id（与 --requirement 互斥）",
    )
    parser.add_argument(
        "--stage",
        type=str,
        default=None,
        help="指定起始阶段（可选，默认从 kb_lookup 开始；首版仅记录到 state）",
    )

    args = parser.parse_args(argv)

    # 互斥校验：--requirement 与 --resume 二选一
    if args.resume and args.requirement:
        parser.error("--resume 与 --requirement 互斥，只能指定其一")
    if not args.resume and not args.requirement:
        parser.error("必须指定 --requirement（新需求）或 --resume（断点恢复）")
    if not args.resume and not args.name:
        parser.error("新建需求时必须指定 --name")

    return args


def stream_with_progress(graph: Any, input_value: Any, config: dict) -> None:
    """流式执行 graph 并打印每个节点名作为进度。"""
    for chunk in graph.stream(input_value, config, stream_mode="updates"):
        for node_name in chunk:
            console.print(f"[bold cyan]▶ 执行节点:[/bold cyan] {node_name}")


def run_fresh(graph: Any, config: dict, args: argparse.Namespace) -> None:
    """新建需求流程：构造初始 state -> 流式执行 -> 处理中断。"""
    thread_id = str(uuid.uuid4())
    config["configurable"]["thread_id"] = thread_id

    state = default_state()
    state["raw_requirement"] = args.requirement
    state["requirement_name"] = args.name
    state["initiative_id"] = thread_id
    if args.stage:
        state["current_stage"] = args.stage

    console.print(
        Panel.fit(
            f"[bold]需求名[/bold]: {args.name}\n"
            f"[bold]thread_id[/bold]: {thread_id}\n"
            f"[bold]需求[/bold]: {args.requirement}",
            title="启动新需求",
            border_style="green",
        )
    )

    # 首轮流式执行（运行到第一个 HITL 中断或 END）
    stream_with_progress(graph, state, config)

    # 循环处理所有中断
    _process_interrupts(graph, config)


def run_resume(graph: Any, config: dict, args: argparse.Namespace) -> None:
    """断点恢复流程：从已有 thread_id 继续执行。"""
    thread_id = args.resume
    config["configurable"]["thread_id"] = thread_id

    console.print(
        Panel.fit(
            f"[bold]thread_id[/bold]: {thread_id}",
            title="断点恢复",
            border_style="blue",
        )
    )

    # 先检查是否有待处理的中断
    interrupts = collect_interrupts(graph, config)
    if interrupts:
        console.print(f"[dim]检测到 {len(interrupts)} 个待处理中断，进入 HITL...[/dim]")
        _process_interrupts(graph, config)
    else:
        # 无中断：从检查点继续执行（可能上次正常结束或未到中断点）
        console.print("[dim]未检测到中断，从检查点继续执行...[/dim]")
        stream_with_progress(graph, None, config)
        _process_interrupts(graph, config)


def _process_interrupts(graph: Any, config: dict) -> None:
    """循环检测并处理 HITL 中断，直到无中断（到达 END）。"""
    while True:
        interrupts = collect_interrupts(graph, config)
        if not interrupts:
            break
        handle_hitl(graph, config, interrupts[0])


def main(argv: list[str] | None = None) -> int:
    """CLI 主流程。"""
    args = parse_args(argv)

    # 1. 加载配置
    try:
        cfg = load_config(args.config)
    except Exception as exc:
        console.print(f"[red]配置加载失败: {exc}[/red]")
        return 1

    # 2. 初始化真实依赖：LLM / 组件注册表 / 产物管理器 / 知识库 / 节点执行器
    try:
        llm = build_llm(cfg)
    except Exception as exc:
        console.print(f"[red]LLM 初始化失败: {exc}[/red]")
        return 1

    kb_store = _resolve_path(cfg["kb"]["store_path"])
    output_root = _resolve_path(cfg["artifacts"]["output_root"])
    template_dir = _resolve_path(cfg["artifacts"]["template_dir"])
    assets_dir = PROJECT_ROOT / "artifacts" / "assets"
    components_dir = PROJECT_ROOT / "src" / "components"
    db_path = _resolve_path(cfg["persistence"]["db_path"])

    registry = ComponentRegistry(str(components_dir))
    artifacts_mgr = ArtifactManager(
        str(output_root), str(template_dir), str(assets_dir)
    )
    kb = KBStore(str(kb_store))
    runner = NodeRunner(llm=llm)
    deps = NodeDeps(runner=runner, registry=registry, artifacts=artifacts_mgr, kb=kb)

    console.print(
        f"[dim]配置已加载 | Provider: "
        f"{cfg.get('llm', {}).get('default_provider', '-')} | "
        f"DB: {db_path} | 产物根: {output_root} | 知识库: {kb_store}[/dim]"
    )

    # 3. 构建 graph（真实节点接线 + SQLite 断点持久化）
    graph = build_graph(deps, db_path=str(db_path))
    config: dict = {"configurable": {}}
    # [C 2026-09-09] M6 真实依赖接线：build_llm + ComponentRegistry + ArtifactManager + KBStore

    try:
        # 4/5. 分支：新建 or 恢复
        if args.resume:
            run_resume(graph, config, args)
        else:
            run_fresh(graph, config, args)
    except KeyboardInterrupt:
        thread_id = config["configurable"].get("thread_id", "-")
        console.print()
        console.print(
            f"[yellow]用户中断 (Ctrl+C) | thread_id: {thread_id} | "
            f"可用 --resume {thread_id} 恢复[/yellow]"
        )
        return 130

    # 6. 打印产物路径
    final_state = graph.get_state(config).values
    print_artifact_paths(final_state)

    console.print()
    console.print("[bold green]✓ 执行结束[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# [C 2026-09-08] main.py 实现完成
