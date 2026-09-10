# [C 2026-09-10] PRD 评审门 schema（prd_review 节点）
"""PrdReviewSchema：PRD 评审门模型输出结构（只审不改，结论由代码硬判）。

按 registry 命名约定：文件名 prd_review -> 类名 PrdReviewSchema。
模型只产出"评审事实"（五维评分/blockers/warnings/红队假设等），
**不产出通过/打回结论**——三档结论由 nodes/review.py 的纯函数按分数硬判。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

# 严重程度三档：阻断 / 重要 / 建议（blockers 与 warnings 共用同一结构）
Severity = Literal["阻断", "重要", "建议"]


class ScoreItem(BaseModel):
    """五维评分中的一维：维度名 + 1-5 分 + 必须引用 PRD 具体章节的锚点理由。"""

    dimension: str = Field(description="评分维度名（五维之一，按 prompt 指定的名称）")
    score: int = Field(ge=1, le=5, description="1-5 整数评分")  # [C 2026-09-10] 分数硬边界
    rationale: str = Field(description="锚点理由：必须引用 PRD 具体章节/原文，不许只写空泛形容词")


class FindingItem(BaseModel):
    """一条评审发现（blockers / warnings 同构）：严重程度 + 位置 + 问题 + 改法方向。"""

    severity: Severity = Field(description="严重程度：阻断 / 重要 / 建议")
    location: str = Field(description="问题所在章节/位置")
    issue: str = Field(description="问题描述（具体、可复现）")
    suggestion: str = Field(description="改法方向（只指方向，不替作者改正文）")


class HypothesisItem(BaseModel):
    """一条承重墙假设的红队质询（它错了整个方案就死）。"""

    hypothesis: str = Field(description="承重墙假设（按最强版本钢人化表述）")
    fail_if: str = Field(description="若……则失败（攻击条件）")
    evidence: str = Field(description="本周能拿到的证据")
    kill_criterion: str = Field(description="杀掉标准（阈值），达到即放弃/转向")
    cheapest_test: str = Field(description="最便宜的验证动作")


class PrdReviewSchema(BaseModel):
    """PRD 评审门完整输出（模型只给评审内容，verdict 不在模型侧）。"""

    restatement: str = Field(description="一句话复述：这份 PRD 要解决什么问题、支撑什么决策")
    scores: list[ScoreItem] = Field(
        min_length=5,
        max_length=5,
        description="五维评分，恰好 5 条：结构完整/需求可验证SMART/逻辑一致/真人感/信息缺口显式标注",
    )  # [C 2026-09-10] 恰好 5 条由 schema 强制
    blockers: list[FindingItem] = Field(
        default_factory=list, description="打回项：必须修后复审的问题"
    )
    warnings: list[FindingItem] = Field(
        default_factory=list, description="警告项：建议修、不阻断放行的问题"
    )
    hypotheses: list[HypothesisItem] = Field(
        min_length=3,
        max_length=5,
        description="承重墙假设红队质询，3-5 条，按 影响×可能性×验证便宜度 排序",
    )  # [C 2026-09-10] 3-5 条由 schema 强制
    strengths: str = Field(description="论证扎实的地方（如实写出，不为挑毛病制造怀疑）")
    unassessable: list[str] = Field(
        default_factory=list, description="PRD 没给够信息、评审无法下结论的点"
    )
    summary: str = Field(description="总评：PRD 当前状态、最关键问题、放行/打回的核心理由")


# [C 2026-09-10] schemas/prd_review.py 新增完成
