# [C 2026-09-11] 拆研发工单 schema（issue_splitting 节点）
"""IssueSplittingSchema：研发工单拆解的模型输出结构。

按 registry 命名约定：文件名 issue_splitting -> 类名 IssueSplittingSchema。
模型只产出"工单方案事实"（纵切工单/覆盖矩阵/版本地图/readiness 状态），
**不产出通过/放行结论**；跨工单的结构完整性（id 唯一、依赖无悬空/无环、
覆盖矩阵一致性）由 nodes/issues.py 的 judge_issue_plan 纯函数硬判。
"""
from __future__ import annotations

from typing import Annotated, Literal, Optional

from pydantic import BaseModel, Field, StringConstraints, model_validator

# 工单 id 硬格式：I 开头 + 纯数字（I1 / I2 / I10），blocked_by、covered_by 引用同此格式
IssueId = Annotated[str, StringConstraints(pattern=r"^I\d+$")]

# 工单类型：AFK=研发可直接动手；HITL=有待人工拍板的决策
IssueType = Literal["AFK", "HITL"]

# 整份方案的可开工状态：pass / needs_clarification / blocked
Readiness = Literal["pass", "needs_clarification", "blocked"]

# 覆盖矩阵三态：covered=已被工单覆盖 / excluded=明确不做（须给原因）/ clarify=信息不足待澄清
CoverageStatus = Literal["covered", "excluded", "clarify"]

# [C 2026-09-13 by codebuddy-ds41flash] AI 特殊项类别：仅 AI 核心需求（ai_core=true）填写。
# trace=调用链埋点（trace 可回看）/ fallback=兜底与转人工 /
# eval_integration=评测接入（Promptfoo 配置与 CI）/ risk_mitigation=风险册缓解措施承接。
AISpecialCategory = Literal["trace", "fallback", "eval_integration", "risk_mitigation"]


class IssueItem(BaseModel):
    """一张研发工单：一个端到端可演示的用户价值薄片（纵切，禁横切）。"""

    id: IssueId = Field(description="工单 id，I 开头加纯数字（如 I1、I10），全清单唯一且按序编号")
    title: str = Field(description="一句话用户价值标题（动词开头、端到端可演示），禁『做前端/做后端/补测试/重构/美化』等横切措辞")
    issue_type: IssueType = Field(description="AFK=研发可直接动手；HITL=有待人工拍板的决策")
    decision_needed: str = Field(
        default="",
        description="待谁决定什么（业务语言：待谁、决定什么、不定挡住什么）；AFK 留空，HITL 必须非空",
    )  # [C 2026-09-11] HITL 非空由 model_validator 强制
    priority: str = Field(description="优先级（如 P0/P1/P2 或 高/中/低，口径在 readiness_notes 说明）")
    labels: list[str] = Field(default_factory=list, description="标签（模块/端等），没有给空数组")
    source_sections: list[str] = Field(
        min_length=1, description="来源 PRD 章节（章节标题原文，至少 1 个），保证可溯源"
    )
    user_value: str = Field(description="做完后谁得到什么好处（用户/业务语言，不写实现）")
    what_to_build: str = Field(description="做什么：用户可见行为为主，必要的技术约束点到为止")
    acceptance_criteria: list[str] = Field(
        min_length=2,
        max_length=6,
        description="验收标准 2-6 条，每条通过/失败边界清楚，测试能直接据此写用例（含本薄片关键异常路径）",
    )
    verification: str = Field(description="验证方式：怎么演示/怎么测、需要什么数据或环境")
    blocked_by: list[IssueId] = Field(
        default_factory=list, description="依赖的其他工单 id；只能引用本清单内已存在工单，无依赖给 []"
    )
    open_questions: list[str] = Field(
        default_factory=list, description="开工前待澄清问题；HITL 单须与 decision_needed 呼应，没有给 []"
    )

    @model_validator(mode="after")
    def _hitl_requires_decision(self) -> "IssueItem":
        """HITL 工单必须写清待决策事项；AFK 不允许误填决策（填空串即可）。"""
        if self.issue_type == "HITL" and not (self.decision_needed or "").strip():
            raise ValueError("issue_type=HITL 时 decision_needed 必须非空（写清待谁决定什么）")
        return self
        # [C 2026-09-11] HITL 决策非空校验落点


class CoverageItem(BaseModel):
    """覆盖矩阵一行：PRD 的一个需求点的去向（covered/excluded/clarify）。"""

    prd_item: str = Field(description="PRD 需求点（可定位的简述或章节锚点），不允许任何需求点在矩阵里消失")
    status: CoverageStatus = Field(description="covered=已被工单覆盖；excluded=明确不做；clarify=信息不足待澄清")
    covered_by: list[IssueId] = Field(
        default_factory=list,
        description="覆盖该需求点的工单 id 列表；covered 时必须非空且引用真实工单，其余状态给 []",
    )
    notes: str = Field(
        default="",
        description="备注：excluded 时必须写排除原因；clarify 时写待澄清问题；covered 可留空",
    )  # [C 2026-09-11] excluded 必填原因由 judge 二次硬判（schema 层不按 status 条件联动）


class VersionItem(BaseModel):
    """版本地图一行：仅大需求分期时填写（小需求 version_map 给空数组）。"""

    version_id: str = Field(description="版本标识，如 V1、V2")
    outcome: str = Field(description="该版本交付后用户能完成什么（端到端结果，不是功能清单）")
    in_scope: str = Field(description="本版范围：包含的工单 id 或范围说明")
    out_of_scope: str = Field(default="", description="本版明确不做什么；确无排除项可留空")
    dependencies: str = Field(description="外部依赖（团队/系统/数据），无则写明无")
    acceptance: str = Field(description="整版验收口径：怎么判断这个版本交付达标")


class AISpecialItem(BaseModel):
    """AI 核心需求的一条特殊项承接声明：哪几张工单承接哪一类 AI 特殊项。

    仅 AI 核心需求（ai_core=true）填写，普通需求不产出。校验采用「模型声明 +
    结构硬判」——真实性（covered_by 是否引用本清单真实工单）与完整性（四类是否齐全）
    由 nodes/issues.py 的 judge_issue_plan 纯函数硬判。
    """

    category: AISpecialCategory = Field(
        description=(
            "四选一：trace=调用链埋点（使线上调用可回看）/ fallback=兜底与转人工 / "
            "eval_integration=评测接入（Promptfoo 配置维护与 CI 接入）/ "
            "risk_mitigation=AI 风险登记册每条的缓解措施承接"
        )
    )
    covered_by: list[IssueId] = Field(
        min_length=1,
        description="承接该类特殊项的工单 id 列表（至少 1 个，必须引用本清单真实存在的工单 id）",
    )
    note: str = Field(
        default="",
        description=(
            "说明：fallback 类必须写清对应 PRD 哪一部分的失败/接管设计；其余类别可空串"
        ),
    )
    # [C 2026-09-13 by codebuddy-ds41flash] 第 7 段 AI 特殊项声明（仅 AI 轨必填）


class IssueSplittingSchema(BaseModel):
    """拆研发工单完整输出（模型只给方案内容，不放行；结构一致性由代码硬判）。"""

    readiness: Readiness = Field(
        description="整份方案可开工状态：pass（默认，PRD 已过评审门）/ needs_clarification（有澄清项）/ blocked（极度谨慎，硬阻断）"
    )
    readiness_notes: str = Field(
        description="状态说明：优先级口径、阻塞/澄清项、评审门遗留 blockers/warnings 如何消化"
    )
    assumptions: list[str] = Field(
        default_factory=list, description="拆单时替团队做的合理假设（每条可被一眼推翻），不把猜测藏进工单正文"
    )
    version_map: list[VersionItem] = Field(
        default_factory=list, description="版本地图，仅大需求分期时填；小需求给 []"
    )
    issues: list[IssueItem] = Field(
        description="纵切工单列表；仅 readiness=blocked 且整份方案无法动手时允许空数组"
    )  # [C 2026-09-11] 刻意不设 min_length：blocked+空列表合法，由 judge 联动 readiness 硬判
    coverage: list[CoverageItem] = Field(
        min_length=1, description="覆盖矩阵至少 1 行：PRD 每项需求都要有去向"
    )
    summary: str = Field(description="一句话总览：几张 AFK、几张 HITL、整体能否开工")
    # [C 2026-09-13 by codebuddy-ds41flash] 第 7 段 AI 特殊项承接声明，仅 AI 核心需求
    # （ai_core=true）必填并由 judge 纯函数硬判；普通需求给 null（不产出、不校验）。
    ai_special_items: Optional[list[AISpecialItem]] = Field(
        default=None,
        description=(
            "AI 特殊项承接声明：trace/fallback/eval_integration/risk_mitigation 四类各至少一条，"
            "covered_by 必须引用本清单真实工单 id；仅 AI 核心需求必填，普通需求给 null"
        ),
    )


# [C 2026-09-11] schemas/issue_splitting.py 新增完成
# [C 2026-09-13 by codebuddy-ds41flash] 新增 AISpecialItem 与 ai_special_items（仅 AI 轨）
