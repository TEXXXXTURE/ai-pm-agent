# [C 2026-09-12 by codebuddy-ds41flash] 设计评测体系 schema（eval_design 节点）
"""EvalDesignSchema：四层考题集 + 每题评分方式 + 及格线建议值。

按 registry 命名约定：文件名 eval_design -> 类名 EvalDesignSchema。

模型只起草"考什么题、怎么判、多少分算及格"（结构化 JSON），写回 state["eval_system"]；
及格线是**建议值**，最终由用户在 eval_confirm 确认门拍板（见 nodes/eval_design.py）。

四层考题（对齐 docs/workflow-design.md 第 5 段）：
- typical      典型题：核心用户任务的正常路径；
- boundary     边界题：长尾、歧义、超长或异常输入；
- adversarial  对抗题（红队）：提示注入、诱导泄露、诱导幻觉；
- replay       线上回放题：上线后由第 11 段飞轮回收的真实坏例，本期为空占位。

评分方式（每题标注 scorer 类型）：
- assertion   L1 确定性断言（规则/程序可判），必填 assertion；
- llm_judge   L2 模型裁判（LLM-as-judge），必填 judge_rubric + 人工抽检比例。
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

# 四层考题的层名（顺序即从确定性到对抗性的递进）
ExamLayer = Literal["typical", "boundary", "adversarial", "replay"]

# 本期只做前三层出题，replay 层为空占位（由第 11 段运营数据回流填充）
_REQUIRED_LAYERS: tuple[str, ...] = ("typical", "boundary", "adversarial", "replay")

# 各层题目数量下限（对齐 prompt 引导：典型≥3 边界≥3 对抗≥2；replay 本期为 0 占位）
_MIN_EXAMS_BY_LAYER: dict[str, int] = {
    "typical": 3,
    "boundary": 3,
    "adversarial": 2,
    "replay": 0,
}


class ExamItem(BaseModel):
    """一道考题：分层 + 题目说明 + 触发输入提示 + 评分器类型与判定依据。"""

    id: str = Field(description="考题编号（层内唯一，如 T1/B1/A1/R1）")
    layer: ExamLayer = Field(description="所属层：typical/boundary/adversarial/replay")
    description: str = Field(
        description="这道题考什么（一句话，指向 PRD 的具体功能或风险点）"
    )
    prompt_hint: str = Field(
        description="出题输入提示（喂给被测模型的具体输入或输入构造方式）"
    )
    scorer: Literal["assertion", "llm_judge"] = Field(
        description="评分器类型：assertion=L1 确定性断言；llm_judge=L2 模型裁判"
    )
    assertion: str | None = Field(
        default=None,
        description="scorer=assertion 时必填：期望结果，用 equals:/contains:/regex: 前缀标注判定方式",
    )
    judge_rubric: str | None = Field(
        default=None, description="scorer=llm_judge 时必填：裁判 rubric 文本（评分标准）"
    )
    manual_review_ratio: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="人工抽检比例（scorer=llm_judge 时必须 >0，如 0.2 表示抽检 20%）",
    )

    @model_validator(mode="after")
    def _check_scorer_fields(self) -> "ExamItem":
        """按 scorer 类型校验必填字段：assertion 题要 assertion，裁判题要 rubric + 抽检比例。"""
        if self.scorer == "assertion":
            if not (self.assertion or "").strip():
                raise ValueError("scorer=assertion 时 assertion 必填")
        else:  # llm_judge
            if not (self.judge_rubric or "").strip():
                raise ValueError("scorer=llm_judge 时 judge_rubric 必填")
            if self.manual_review_ratio <= 0:
                raise ValueError("scorer=llm_judge 时 manual_review_ratio 必须 >0")
        return self


class ExamSet(BaseModel):
    """一层考题集：层名 + 该层全部题目 + 占位说明（replay 层用）。"""

    layer: ExamLayer = Field(description="层名：typical/boundary/adversarial/replay")
    exams: list[ExamItem] = Field(
        default_factory=list, description="该层考题列表（replay 层本期为空数组）"
    )
    placeholder_note: str = Field(
        default="",
        description="占位说明（replay 层：线上坏例由第 11 段运营数据回流填充）",
    )


class PassLines(BaseModel):
    """及格线建议值：整体通过率 + 关键题单项阈值（最终由用户确认门拍板）。"""

    overall_pass_rate: float = Field(
        ge=0.0, le=1.0, description="整体通过率阈值建议值（0-1，如 0.85）"
    )
    critical_pass_rate: float = Field(
        ge=0.0,
        le=1.0,
        description="关键题（对抗题与核心典型题）单项通过率阈值建议值（0-1）",
    )
    note: str = Field(
        description="推导依据（从 PRD 的可接受通过率与 kill 阈值推导，并说明这是建议值）"
    )


class EvalDesignSchema(BaseModel):
    """评测体系完整输出（四层考题 + 评分方式 + 及格线建议值）。"""

    purpose: str = Field(description="一句话：这套评测要证明什么")
    exam_sets: list[ExamSet] = Field(
        min_length=4, max_length=4, description="四层考题集，恰好四层各一条"
    )
    pass_lines: PassLines = Field(description="及格线建议值（整体 + 关键题两套数值）")

    @model_validator(mode="after")
    def _check_layers_and_counts(self) -> "EvalDesignSchema":
        """硬校验：四层齐全各一条 + 各层题量达到下限 + 题内 layer 与所属层一致。"""
        seen: list[str] = [item.layer for item in self.exam_sets]
        if sorted(seen) != sorted(_REQUIRED_LAYERS):
            raise ValueError(
                f"exam_sets 必须恰好覆盖四层各一条，实际为 {seen}"
            )
        for item in self.exam_sets:
            minimum = _MIN_EXAMS_BY_LAYER.get(item.layer, 0)
            actual = len(item.exams)
            if actual < minimum:
                raise ValueError(
                    f"{item.layer} 层考题至少 {minimum} 条，实际 {actual} 条"
                )
            for exam in item.exams:
                if exam.layer != item.layer:
                    raise ValueError(
                        f"考题 {exam.id} 的 layer={exam.layer} 与所属 {item.layer} 层不一致"
                    )
        return self


# [C 2026-09-12 by codebuddy-ds41flash] schemas/eval_design.py 新增完成
