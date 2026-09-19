# 发布计划 schema（launch_plan 节点）
"""LaunchPlanSchema：发布计划（GTM launch plan）模型输出结构。

按 registry 命名约定：文件名 launch_plan -> 类名 LaunchPlanSchema。
模型只产出"发布计划事实"（分层/定位/工作流矩阵/倒排时间线/灰度回滚/
go-no-go/Day1-7 值班/Top3 风险/Tier1 扩展），**不产出通过/放行结论**；
关键字段非空与 Tier1 扩展必填由 nodes/launch_plan.py 的 judge_launch_plan
纯函数硬判（返回 (errors, warnings) tuple）。
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# 发布分层：1=大发布（公司级叙事+全套 GTM）；2=中等发布（定向宣告）；3=小发布（说明+文档）
Tier = Literal["1", "2", "3"]


class SuccessMetrics(BaseModel):
    """发布成功定义：D7 与 D30 的数字化目标（Tier1/2 无数字目标不发布）。"""

    d7: str = Field(description="第 7 天的数字化成功目标（如日活破 X、核心转化率 Y%）")
    d30: str = Field(description="第 30 天的数字化成功目标")
    # 证据分级：D7/D30 目标数字的关键来源等级 [T1]-[T5]，可空。
    evidence_tier: Literal["T1", "T2", "T3", "T4", "T5"] | None = Field(
        default=None,
        description=(
            "这些目标数字最有依据的那条证据来源等级，取 [T1]~[T5] 之一（T1 实测数据 / "
            "T2 直接用户证据 / T3 结构化分析 / T4 口述意见 / T5 直觉）；"
            "依据分散或无法归级时留 null 不硬标"
        ),
    )


class Workstream(BaseModel):
    """工作流矩阵一行：一条工作线有且仅有一个具名责任人。"""

    workstream: str = Field(
        description="工作线名称（产品就绪度/文档/客服赋能/销售赋能/定价/市场物料/对外沟通/法务/数据埋点/灰度发布等）"
    )
    owner: str = Field(
        description="具名责任人（必须落到一个人名；'某团队''客服那边'不算责任人）"
    )
    deliverable: str = Field(description="关键交付物")
    deadline: str = Field(description="截止时间（T-minus 或具体日期）")
    status: str = Field(default="未开始", description="当前状态（未开始/进行中/完成）")


class TimelineItem(BaseModel):
    """倒排时间线一行：T-minus 里程碑 + 责任人，关键路径需标注。"""

    t_minus: str = Field(description="T-minus 时间点（如 T-30、T-14、T-7、T0、T+1）")
    milestone: str = Field(description="里程碑事件")
    owner: str = Field(description="责任人")
    is_critical_path: bool = Field(
        default=False,
        description="是否关键路径（决定发布日能否成立的最长依赖链上的节点标 true）",
    )


class RollbackStage(BaseModel):
    """灰度放量阶段：internal → beta → X% → GA，每阶段带观测指标与阈值。"""

    stage: str = Field(description="阶段名（internal/beta/百分比放量/GA）")
    timing: str = Field(description="日期或进入条件")
    metric: str = Field(description="本阶段观测指标")
    threshold: str = Field(description="通过阈值（达标才进下一阶段；'我们会盯着的'不是计划）")


class Rollback(BaseModel):
    """灰度机制与回滚方案：回滚触发条件必须是数字不是心情。"""

    stages: list[RollbackStage] = Field(
        min_length=1, description="灰度放量阶段列表，至少 1 行（internal→beta→%→GA）"
    )
    rollback_trigger: str = Field(
        description="回滚触发条件（必须是数字，如错误率>2%、核心转化率下降>10%；'感觉不对'不合格）"
    )
    rollback_steps: list[str] = Field(
        min_length=1, description="回滚操作步骤（可照着执行的有序步骤）"
    )
    rollback_owner: str = Field(description="回滚责任人（具名）")


class ChecklistItem(BaseModel):
    """go/no-go 检查项：只允许二元判断，每项有 owner。"""

    item: str = Field(
        description="二元判断项（能明确回答是/否，如'帮助文章已发布并完成评审'；'基本好了'不合格）"
    )
    owner: str = Field(description="该项 owner")


class OncallRosterEntry(BaseModel):
    """Day1-7 值班表一行。"""

    day: str = Field(description="值班日（Day1/Day2/.../Day7）")
    owner: str = Field(description="值班人（具名）")
    focus: str = Field(description="当日盯守重点")


class OnCall(BaseModel):
    """发布后 Day1-7 值班安排与首次复盘。"""

    dashboard_owner: str = Field(description="盯哪个数据看板的责任人（具名）")
    feedback_channels: list[str] = Field(
        min_length=1, description="反馈从哪几个渠道汇到哪里（至少 1 个渠道）"
    )
    oncall_roster: list[OncallRosterEntry] = Field(
        min_length=1, description="Day1-7 值班表（至少 1 行）"
    )
    first_retro_date: str = Field(
        description="首次复盘日期（只写日期本身，如 T+7 或具体日期；括注说明由模板统一追加，值里不带括注）"
    )


class Risk(BaseModel):
    """Top3 风险之一：带缓解措施与可观察的早期预警信号。"""

    risk: str = Field(description="风险描述")
    mitigation: str = Field(description="缓解措施")
    early_warning: str = Field(
        description="早期预警信号（可观察的数字或事件，不是'感觉不对'）"
    )
    # 证据分级：本风险判断依据的关键来源等级 [T1]-[T5]，可空。
    evidence_tier: Literal["T1", "T2", "T3", "T4", "T5"] | None = Field(
        default=None,
        description=(
            "本风险判断最有依据的那条证据来源等级，取 [T1]~[T5] 之一（T1 实测数据 / "
            "T2 直接用户证据 / T3 结构化分析 / T4 口述意见 / T5 直觉）；"
            "依据分散或无法归级时留 null 不硬标"
        ),
    )


class Beachhead(BaseModel):
    """滩头市场：不面向所有人，选一个最该先拿下的细分人群。"""

    segment: str = Field(description="细分人群")
    rationale: str = Field(
        description="选择理由（痛点够痛/愿付费/打得赢/会转介绍）"
    )
    adjacent_expansion: str = Field(description="拿下滩头后的相邻扩张人群")


class ICP(BaseModel):
    """理想客户画像（Ideal Customer Profile）。"""

    attributes: str = Field(description="公司规模/行业/地域等关键属性")
    decision_maker: str = Field(description="决策人角色")
    jtbd: str = Field(description="他们雇佣本产品完成的具体任务（JTBD）")
    current_alternative: str = Field(description="今天用什么替代方案")
    qualifying_signal: str = Field(description="30 秒内可识别的资格信号")


class AudienceMessage(BaseModel):
    """分受众信息：购买者/使用者/影响者各看什么。"""

    role: str = Field(description="受众角色（购买者/使用者/影响者）")
    message: str = Field(description="该角色看的信息（用客户语言，非内部术语）")
    proof_point: str = Field(description="证据点")


class Channel(BaseModel):
    """渠道按预期 ROI 排序。"""

    channel: str = Field(description="渠道名")
    reach: str = Field(description="可达量")
    cost: str = Field(description="成本")
    priority: str = Field(description="优先级")
    pre_launch_action: str = Field(
        default="",
        description="pre-launch 动作（等待名单/beta/抢先体验等）；无则留空",
    )


class Tier1Extension(BaseModel):
    """Tier1 扩展检查：仅 Tier1 大发布必做，Tier2/3 不填。"""

    beachhead: Beachhead = Field(description="滩头市场")
    icp: ICP = Field(description="理想客户画像")
    audience_messages: list[AudienceMessage] = Field(
        min_length=1, description="分受众信息（至少 1 条）"
    )
    channel_ranking: list[Channel] = Field(
        min_length=1, description="渠道按 ROI 排序（至少 1 条，含 pre-launch 动作）"
    )


class KillThreshold(BaseModel):
    """AI 在线 kill 阈值一条：在线监控指标触发某方向数值后执行的动作。

    仅 AI 核心需求（ai_core=true）填写，普通需求不产出。语义示例：
    在线答复准确率 below 90%（连续 15 分钟）→ rollback。
    """

    metric: str = Field(
        description="监控指标名（如 在线答复准确率 / 人工接管率 / P95 延迟 / 单均成本）"
    )
    trigger_direction: Literal["above", "below"] = Field(
        description="触发方向：above=高于阈值触发，below=低于阈值触发（如准确率 below、接管率 above）"
    )
    threshold: str = Field(
        description="触发数值（必须含数字，如 90%、5%、800ms、2 元）"
    )
    window: str = Field(
        description="统计窗口（必须含数字，如 连续 15 分钟、近 1 小时）"
    )
    action: Literal["degrade", "rollback", "disable", "alert"] = Field(
        description="触发动作：degrade=降级 / rollback=回滚 / disable=停用 / alert=告警"
    )


class CohortStage(BaseModel):
    """AI 轨 cohort 分批晋级一批：放量多少、观察多久、达到什么数值才晋级。"""

    cohort: str = Field(description="批次名（如 internal / beta / 5% / GA）")
    percent: str = Field(description="本批放量比例（必须含数字，如 0% / 5% / 100%）")
    dwell_time: str = Field(description="观察时长（必须含数字，如 48 小时、3 天）")
    promotion_criteria: str = Field(
        description="晋级下一批的观测指标与通过值（必须含数字，如 准确率≥92% 且接管率≤3%）"
    )


class LaunchPlanSchema(BaseModel):
    """发布计划完整输出（模型只给计划内容，不放行；关键字段非空由代码硬判）。"""

    tier: Tier = Field(
        description="分层：1=大发布（公司级叙事+全套 GTM 机器）；2=中等发布（定向宣告）；3=小发布（只出说明+文档）"
    )
    tier_rationale: str = Field(description="分层理由（为什么是这个 Tier 而非更高/更低）")
    positioning: str = Field(
        description="定位一句话：对于【受众】中饱受【痛点】的人，【产品】能带来【结果】，与【替代方案】不同的是【差异点】"
    )
    success_metrics: SuccessMetrics = Field(
        description="发布成功定义：D7 与 D30 数字化目标（Tier1/2 无数字目标不发布）"
    )
    workstreams: list[Workstream] = Field(
        min_length=1, description="工作流矩阵：每条工作线有且仅有一个具名责任人"
    )
    timeline: list[TimelineItem] = Field(
        min_length=1,
        description="T-minus 倒排时间线，关键路径节点 is_critical_path=true；Tier1-2 须排入发布演练/Bug Bash",
    )
    rollback: Rollback = Field(description="灰度机制与回滚方案（回滚触发条件必须是数字）")
    go_no_go_checklist: list[ChecklistItem] = Field(
        min_length=1, description="go/no-go 二元判断项清单（会议安排在 T-2 或 T-3）"
    )
    on_call: OnCall = Field(description="Day1-7 值班安排与首次复盘")
    risks: list[Risk] = Field(
        min_length=1, max_length=3, description="Top3 风险（每条带缓解措施与早期预警）"
    )
    tier1_extension: Optional[Tier1Extension] = Field(
        default=None,
        description="Tier1 扩展检查；仅 Tier1 必填，Tier2/3 给 null",
    )
    # 两组 AI 专属字段，仅 AI 核心需求（ai_core=true）
    # 必填并由 judge 纯函数硬判；普通需求给 null（不产出、不校验）。
    ai_guardrails: Optional[list[KillThreshold]] = Field(
        default=None,
        description=(
            "AI 在线 kill 阈值列表（至少 2 条，须含质量类与人工接管率类指标）；"
            "仅 AI 核心需求必填，普通需求给 null"
        ),
    )
    cohort_rollout: Optional[list[CohortStage]] = Field(
        default=None,
        description=(
            "AI 轨 cohort 分批晋级规则（至少 2 批，末批全量）；"
            "仅 AI 核心需求必填，普通需求给 null"
        ),
    )


# KillThreshold/CohortStage 与两组 AI 专属 Optional 字段
