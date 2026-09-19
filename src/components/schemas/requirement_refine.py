# 需求修订整合 schema
"""RequirementRefineSchema：需求修订整合节点的输出结构（整合后的完整需求 + 变更说明）。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class RequirementRefineSchema(BaseModel):
    """需求修订整合结果。

    模型把"当前需求 + 用户修订意见"整合成一版完整新需求文本，
    返回 refined_requirement（完整版本，可直接替换原 confirmed_requirement）
    与 change_summary（本次调整说明，每条一句话）。
    """

    refined_requirement: str = Field(
        ...,
        description="整合后的完整需求文本（不是 diff、不是片段，可直接替换原文）",
    )
    change_summary: list[str] = Field(
        default_factory=list,
        description="本次调整说明，每条一句话",
    )


