# 组件框架 - 需求挖掘 schema
"""UserInsightsSchema：需求挖掘节点的输出结构（用户洞察）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UserInsightsSchema(BaseModel):
    """从用户脑中挖掘的需求洞察。"""

    target_users: list[str] = Field(default_factory=list, description="目标用户画像（谁在用）")
    scenarios: list[str] = Field(default_factory=list, description="使用场景（什么时候用、什么环境下用）")
    pain_points: list[str] = Field(default_factory=list, description="核心痛点（当前方案的不足）")
    current_solutions: list[str] = Field(default_factory=list, description="现有方案（用户现在怎么解决）")
    gaps: list[str] = Field(default_factory=list, description="期望差距（理想与现实的 gap）")
    success_criteria: list[str] = Field(default_factory=list, description="成功标准（用户怎么判断这个功能做好了）")


