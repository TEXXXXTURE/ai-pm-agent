# [C 2026-09-16] 就绪度打分 schema（readiness_assessment 节点）
"""ReadinessSchema：发布前就绪度打分（R11）。

模型读全部上游产物（PRD / 评测报告 / 选型报告 / 发布计划 / 评审报告 / 工单清单），
对 11 个维度各打 0-5 分并给证据/风险/责任人/下一步；代码硬算加权均分与三级阻断。

按 registry 命名约定：文件名 readiness_assessment -> 类名 ReadinessAssessmentSchema。
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class DimensionScore(BaseModel):
    """一个维度的打分：分数 + 证据 + 风险 + 责任人 + 下一步。"""

    score: int = Field(
        ge=0, le=5,
        description=(
            "0=缺失 / 1=提及未定义 / 2=已起草未验证 / "
            "3=有部分证据 / 4=可发布强证据 / 5=上线且有人在改进"
        ),
    )
    evidence: str = Field(
        description="打分依据：引用具体产物里的内容（如'PRD 第 3 节定义了 AI 工作语句'），标注证据等级 [T1]-[T5]"
    )
    risk: str = Field(
        description="本维度当前最大风险（一句话；无风险写'无'）"
    )
    owner: str = Field(
        description="本维度责任人（具名；'待定'只在确实没人时用）"
    )
    next_action: str = Field(
        description="提升到下一档需要做什么（一句话；已达 5 分写'维持'）"
    )


class ReadinessAssessmentSchema(BaseModel):
    """11 维度就绪度打分完整输出。"""

    problem_fit: DimensionScore = Field(
        description="问题契合度：AI 是否解决了一个真实用户问题，且比现有方案好"
    )
    workflow_fit: DimensionScore = Field(
        description="工作流契合度：AI 是否嵌入到带审核/纠正/兜底的工作流中"
    )
    ai_job_definition: DimensionScore = Field(
        description="AI 工作定义：AI 任务是否具体到可评测可交付"
    )
    data_readiness: DimensionScore = Field(
        description="数据就绪度：所需数据是否可用、有权限、新鲜、可靠"
    )
    eval_readiness: DimensionScore = Field(
        description="评测就绪度：团队能否在上线前后测量质量"
    )
    system_behavior: DimensionScore = Field(
        description="系统行为：模型/检索/工具/延迟/兜底行为是否已定义"
    )
    risk_and_safety: DimensionScore = Field(
        description="风险与安全：潜在危害/误用/缓解措施是否到人"
    )
    regulatory_readiness: DimensionScore = Field(
        description="监管就绪：风险分类/数据溯源/透明度/合规要求是否明确"
    )
    cost_and_business_case: DimensionScore = Field(
        description="成本与商业价值：单位经济学是否可信"
    )
    observability: DimensionScore = Field(
        description="可观测性：团队能否看到 AI 在生产环境中是否正常工作"
    )
    launch_and_operations: DimensionScore = Field(
        description="发布与运营：是否有分阶段放量与上线后运营节奏"
    )


# [C 2026-09-16] schemas/readiness_assessment.py 新增
