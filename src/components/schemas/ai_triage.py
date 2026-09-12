# [C 2026-09-12 by MA] S033 块2a - AI 适用性分流判定 schema
"""AiTriageSchema：需求确认门的 AI 适用性分流判定（ai_core / non_ai / uncertain）。

输出由模型给出建议（{suggestion, reason, signals}），最终 ai_core 布尔值由用户在
确认门内拍板（确认/「非AI」/「AI核心」/自由文本修订），见 nodes/hitl.py。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AiTriageSchema(BaseModel):
    """AI 适用性分流判定结果。

    suggestion 取值受 Literal 约束，模型输出非枚举值会被 Pydantic 硬拒
    （NodeRunner 内部 schema 校验重试一次仍错则抛 NodeExecutionError）。
    """

    suggestion: Literal["ai_core", "non_ai", "uncertain"] = Field(
        ...,
        description=(
            "分流判定建议：ai_core=AI 核心需求（走全轨含可行性门+ai-native PRD+"
            "评测+选型+内循环+飞轮）；non_ai=普通需求（走原轨道，行为不变）；"
            "uncertain=存疑，确认门默认按 ai_core=true 走，探针实测后可改判回普通轨"
        ),
    )
    reason: str = Field(
        ...,
        description="判定理由，一两句话讲清为什么这样判（不要长篇）",
    )
    signals: list[str] = Field(
        default_factory=list,
        description=(
            "支持判定的具体信号，每条一句话、基于当前需求的具体内容"
            "（不要写放之四海皆准的空话）"
        ),
    )


# [C 2026-09-12 by MA] schemas/ai_triage.py 新增完成
