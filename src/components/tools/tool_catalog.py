# 工具能力清单加载器
"""tool_catalog：读外置工具路由.md，解析出已装工具的能力清单。

纯函数、不调模型、不依赖网络；feasibility_check 节点据此把工具能力注入 prompt，
让模型判断"模型做不到的能力点是否有工具可补"。

解析策略：按行扫描 Markdown 表格，找到"能力说明"列中含"能做："前缀的行，
提取三要素（能做/不能做/调用前提）。缺文件/空表/无"能做"前缀的行跳过，不报错。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _parse_capability(cell: str) -> tuple[str, str, str]:
    """从能力说明单元格解析三要素：能做/不能做/调用前提。

    格式：``能做：XXX；不能做：XXX；调用前提：XXX``
    用正则按标签切分，标签之间的内容取到下一个标签前。
    缺失的要素返回空串。
    """
    can_do = ""
    cannot_do = ""
    prerequisite = ""

    can_match = re.search(r"能做[：:]\s*(.*?)(?=\s*不能做[：:])", cell)
    if can_match:
        can_do = can_match.group(1).strip().rstrip("；;")

    cannot_match = re.search(r"不能做[：:]\s*(.*?)(?=\s*调用前提[：:])", cell)
    if cannot_match:
        cannot_do = cannot_match.group(1).strip().rstrip("；;")

    prereq_match = re.search(r"调用前提[：:]\s*(.*)", cell)
    if prereq_match:
        prerequisite = prereq_match.group(1).strip()

    return can_do, cannot_do, prerequisite


def load_tool_catalog(routing_md_path: str | Path) -> list[dict[str, Any]]:
    """读外置工具路由.md，解析出已装工具的能力清单。

    Returns:
        list[dict]，每项：{name, can_do, cannot_do, prerequisite, category}。
        category 为 "产物能力"（第二节）或 "执行器"（二·补节）。
        文件不存在/无法解析时安全降级返回空列表（不抛错）。
    """
    path = Path(routing_md_path)
    if not path.exists():
        return []

    try:
        text = path.read_text(encoding="utf-8-sig")
    except Exception:
        return []

    if not text.strip():
        return []

    catalog: list[dict[str, Any]] = []
    current_category = ""

    for line in text.splitlines():
        stripped = line.strip()

        # 追踪章节切换，决定 category
        if stripped.startswith("## 二、工具目录"):
            current_category = "产物能力"
            continue
        if stripped.startswith("## 二·补"):
            current_category = "执行器"
            continue
        # 第三节起不再有工具表格
        if stripped.startswith("## 三"):
            break

        # 跳过非表格行
        if not stripped.startswith("|"):
            continue
        # 跳过分隔行（|---|---|...）
        if re.match(r"^\|[\s\-:|]+\|$", stripped):
            continue

        # 按 | 切分，去掉首尾空元素（由行首/行尾的 | 产生）
        cells = [c.strip() for c in stripped.split("|")]
        if cells and cells[0] == "":
            cells = cells[1:]
        if cells and cells[-1] == "":
            cells = cells[:-1]

        # 至少 3 列（期望形式/工具名/能力说明）
        if len(cells) < 3:
            continue
        # 跳过表头行
        if cells[2] == "能力说明":
            continue
        # 未在已知章节内（如第四节文字改写表）跳过
        if not current_category:
            continue

        name = cells[1]
        capability_cell = cells[2]

        # 只处理含"能做："前缀的行（备选未收录的行自然跳过）
        if "能做：" not in capability_cell and "能做:" not in capability_cell:
            continue

        can_do, cannot_do, prerequisite = _parse_capability(capability_cell)

        catalog.append(
            {
                "name": name,
                "can_do": can_do,
                "cannot_do": cannot_do,
                "prerequisite": prerequisite,
                "category": current_category,
            }
        )

    return catalog


