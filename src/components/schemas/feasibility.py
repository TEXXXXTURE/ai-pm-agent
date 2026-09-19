# 判断需求与 AI 的边界 schema（feasibility_check 节点）
"""FeasibilitySchema：可行性报告（能力三方对照表 + PoL 探针方案 + 风险扫描 + 成本粗估 + 初步结论）。

按 registry 命名约定：文件名 feasibility -> 类名 FeasibilitySchema。
模型只产出"可行性事实与探针方案"，**不产出通过/放弃结论**——四态走向由人工在
feasibility_confirm 确认门录入并拍板（见 nodes/feasibility.py）。
探针只产方案、由人工执行实测，流水线不自动调模型跑探针。

S043-b2：CapabilityItem 从单看模型三色扩为三方对照结构
（模型判定→工具补充→综合判定），为后续探针真跑打数据结构基础；
ProbeStep 加 target_capability 与第 1 步能力点建关联。

S048（2026-09-16）：新增 ModelCandidate 与 model_candidates（2–5 条）——
候选池提前到第 2 段产出，第 3 段 AI-native PRD「模型要求与切换条件」与
第 6 段对比选型引用；节点代码补实时单价与接入状态。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CapabilityItem(BaseModel):
    """关键能力点三方对照判断一行。"""

    capability: str = Field(description="关键能力点（一条一句话，基于本需求的具体能力）")
    model_status: Literal["绿", "黄", "红"] = Field(
        description="模型原生能力判定：绿=模型可稳定做到；黄=需配合兜底或人工；红=做不到"
    )
    model_note: str = Field(description="模型判定依据（为什么是这个颜色，一两句话）")
    tool_supplement: str = Field(
        default="",
        description="工具补充方案：模型做不到的能力点，查工具清单有没有能补的工具（写工具名+怎么补）；模型能做的不用写",
    )
    final_status: Literal["绿", "黄", "红"] = Field(
        description="综合判定：模型能做=绿；模型做不到但有工具可补=黄（需配合工具）；模型做不到且无工具可补=红"
    )
    final_note: str = Field(
        description="综合判定依据：为什么最终是这个颜色（如'模型可稳定做到'或'模型做不到但有XX工具可补，降级为黄'）"
    )


class ProbeStep(BaseModel):
    """PoL 探针一条：核心任务/边界/失败诱导样例之一。"""

    name: str = Field(description="探针名（如核心任务样例 / 边界样例 / 失败诱导样例）")
    target_capability: str = Field(
        description="本探针对准的 capability_matrix 中的能力点名称（建关联，须与上方某条 capability 字段匹配）"
    )
    prompts: list[str] = Field(
        min_length=1, description="探针 prompt 链（≤5 条中的一条，含具体 prompt 文本）"
    )
    steps: str = Field(description="调用模型与执行步骤说明（由人工照着执行实测）")
    expected: str = Field(description="期望观察到的结果（用来判定这条探针是否通过）")


class RiskItem(BaseModel):
    """风险扫描一行：四类风险逐项给等级与缓解方案。"""

    type: Literal["幻觉", "注入", "泄露", "监管"] = Field(
        description="风险类型：幻觉 / 注入 / 泄露 / 监管"
    )
    level: Literal["高", "中", "低"] = Field(description="风险等级：高 / 中 / 低")
    mitigation: str = Field(description="缓解措施（可执行的动作，不是'会注意'）")


class ModelCandidate(BaseModel):
    """候选池一条：本需求可用的模型候选（第 2 段产出）。

    由 feasibility_check 阶段 1 的模型从候选清单（references/模型候选清单.md）里挑出，
    2–5 条；节点随后补 price / price_source / price_fetched_at / price_note / access_status
    五个字段（见 nodes/feasibility.py），供第 3 段 AI-native PRD「模型要求与切换条件」
    与第 6 段对比选型引用。
    """

    provider_id: str = Field(
        description="litellm 调用格式，如 deepseek/deepseek-chat（须来自候选清单）"
    )
    label: str = Field(description="显示名（如 DeepSeek-Chat）")
    role: str = Field(
        description="角色：主模型 / 备选 / 专用档（长文、高并发、低成本等）"
    )
    why: str = Field(
        description="为什么适合本需求（一句话，必须挂到本需求的能力点）"
    )
    access_hint: str = Field(
        description="接入代价（需要什么密钥/账号，或\"本机已接入\"）"
    )
    notes: str = Field(description="已知限制与坑（取自候选清单）")


class CostEstimate(BaseModel):
    """成本粗估：按预估调用量、上下文长度、候选模型单价算月度区间。"""

    low: float = Field(description="月度成本下限（数字）")
    high: float = Field(description="月度成本上限（数字）")
    currency: str = Field(description="币种（如 CNY / USD）")
    assumption: str = Field(description="估算假设（调用量、上下文长度、候选模型单价口径）")


class FeasibilitySchema(BaseModel):
    """可行性报告完整输出（模型只给事实与探针方案，不放行；四态由人工确认门拍板）。"""

    capability_matrix: list[CapabilityItem] = Field(
        min_length=1, description="关键能力点三方对照表（逐点标模型判定+工具补充+综合判定）"
    )
    probe_plan: list[ProbeStep] = Field(
        min_length=1,
        max_length=5,
        description="PoL 探针方案（≤5 条 prompt 链：核心任务样例、边界样例、失败诱导样例），target_capability 对准上方能力点",
    )
    risks: list[RiskItem] = Field(
        min_length=1,
        description="风险扫描（幻觉/注入/泄露/监管四类逐项给风险等级与缓解措施）",
    )
    cost_estimate: CostEstimate = Field(
        description="月度成本区间粗估（单价口径必须写明按候选池里哪个候选算）"
    )
    conclusion: str = Field(
        description="初步结论（仅供参考，最终由人工在确认门拍板，不是放行结论）"
    )
    # 候选池前置（2–5 条）。
    # 缺省为空列表（老检查点/降级不报错，节点记 candidate_pool_note）；
    # 模型显式给出列表时受 2–5 条约束（pydantic 不校验 default，故缺省可放行）。
    model_candidates: list[ModelCandidate] = Field(
        default_factory=list,
        min_length=2,
        max_length=5,
        description="模型候选池（2–5 条，每个写清角色/适配理由/接入代价/已知限制）；节点补实时单价与接入状态",
    )


# CapabilityItem 扩三方对照（model_status/model_note/tool_supplement/final_status/final_note）
#     + ProbeStep 加 target_capability 关联第 1 步能力点
# ModelCandidate + FeasibilitySchema.model_candidates（2–5 条）
