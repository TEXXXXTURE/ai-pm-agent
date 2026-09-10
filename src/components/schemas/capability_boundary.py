# [C 2026-09-09] M6 纵切联调 - 能力边界三色表 schema
"""CapabilityBoundarySchema：需求确认门的能力边界三色表（auto/tool/manual）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class CapabilityBoundarySchema(BaseModel):
    """能力边界分类结果：Agent 自动 / 需工具 / 必须人工。"""

    auto: list[str] = Field(default_factory=list, description="Agent 可全自动完成的事项")
    tool: list[str] = Field(default_factory=list, description="需外部工具/MCP 协助的事项")
    manual: list[str] = Field(default_factory=list, description="必须人类决策的事项")


# [C 2026-09-09] schemas/capability_boundary.py 新增完成
