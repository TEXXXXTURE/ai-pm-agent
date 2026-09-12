# [C 2026-09-12 by codebuddy-ds41flash] 验证AI可行性 schema（feasibility_check 节点）
"""FeasibilitySchema：可行性报告（能力三色表 + PoL 探针方案 + 风险扫描 + 成本粗估 + 初步结论）。

按 registry 命名约定：文件名 feasibility -> 类名 FeasibilitySchema。
模型只产出"可行性事实与探针方案"，**不产出通过/放弃结论**——四态走向由人工在
feasibility_confirm 确认门录入并拍板（见 nodes/feasibility.py）。
探针只产方案、由人工执行实测，流水线不自动调模型跑探针。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class CapabilityItem(BaseModel):
    """关键能力点三色判断一行。"""

    capability: str = Field(description="关键能力点（一条一句话，基于本需求的具体能力）")
    status: Literal["绿", "黄", "红"] = Field(
        description="三色：绿=模型可稳定做到；黄=需配合兜底或人工；红=做不到"
    )
    note: str = Field(description="判断依据（为什么是这个颜色，一两句话）")


class ProbeStep(BaseModel):
    """PoL 探针一条：核心任务/边界/失败诱导样例之一。"""

    name: str = Field(description="探针名（如核心任务样例 / 边界样例 / 失败诱导样例）")
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


class CostEstimate(BaseModel):
    """成本粗估：按预估调用量、上下文长度、候选模型单价算月度区间。"""

    low: float = Field(description="月度成本下限（数字）")
    high: float = Field(description="月度成本上限（数字）")
    currency: str = Field(description="币种（如 CNY / USD）")
    assumption: str = Field(description="估算假设（调用量、上下文长度、候选模型单价口径）")


class FeasibilitySchema(BaseModel):
    """可行性报告完整输出（模型只给事实与探针方案，不放行；四态由人工确认门拍板）。"""

    capability_matrix: list[CapabilityItem] = Field(
        min_length=1, description="关键能力点三色表（逐点标绿/黄/红并给依据）"
    )
    probe_plan: list[ProbeStep] = Field(
        min_length=1,
        max_length=5,
        description="PoL 探针方案（≤5 条 prompt 链：核心任务样例、边界样例、失败诱导样例）",
    )
    risks: list[RiskItem] = Field(
        min_length=1,
        description="风险扫描（幻觉/注入/泄露/监管四类逐项给风险等级与缓解措施）",
    )
    cost_estimate: CostEstimate = Field(description="月度成本区间粗估")
    conclusion: str = Field(
        description="初步结论（仅供参考，最终由人工在确认门拍板，不是放行结论）"
    )


# [C 2026-09-12 by codebuddy-ds41flash] schemas/feasibility.py 新增完成
