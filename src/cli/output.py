# CLI 前端 - 产物输出通知
"""产物路径打印与打开工具。"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table

console = Console()


def print_artifact_paths(state: dict) -> None:
    """用 rich 打印 state 中 artifacts 字段的文件路径。

    首版 artifacts 通常为空字典，此时打印"暂无产物"。
    """
    artifacts: dict = state.get("artifacts", {}) or {}

    console.print()
    console.rule("[bold magenta]产物输出[/bold magenta]")

    if not artifacts:
        console.print("[dim]暂无产物（首版节点为占位实现，未生成文件）[/dim]")
        return

    table = Table(title="产物清单", show_header=True, header_style="bold cyan")
    table.add_column("产物名", style="cyan", no_wrap=True)
    table.add_column("文件路径", style="green")

    for name, path in artifacts.items():
        table.add_row(str(name), str(path))

    console.print(table)


def open_artifact(path: str) -> None:
    """调用系统默认程序打开文件。

    - Windows: os.startfile
    - macOS: open
    - Linux: xdg-open
    """
    p = Path(path)
    if not p.exists():
        console.print(f"[red]文件不存在: {path}[/red]")
        return

    try:
        if sys.platform.startswith("win"):
            os.startfile(str(p))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(p)], check=True)
        else:
            subprocess.run(["xdg-open", str(p)], check=True)
        console.print(f"[green]已打开: {path}[/green]")
    except Exception as exc:
        console.print(f"[red]打开失败: {exc}[/red]")


