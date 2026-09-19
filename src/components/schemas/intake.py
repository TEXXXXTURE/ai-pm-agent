# 组件框架 - 需求接收 schema
"""IntakeSchema：需求接收节点的输出结构（6 维度信息完整度评估）。"""
from __future__ import annotations

from pydantic import BaseModel, model_validator

# 6 个固定维度 key
DIMENSION_KEYS = (
    "target_user",
    "core_scenario",
    "pain_point",
    "current_solution",
    "success_criteria",
    "constraints",
)


class IntakeSchema(BaseModel):
    """需求信息完整度评估结果。

    dimensions: 6 维度，每个维度含 score(0-1) 和 missing(缺失说明)
    summary: 整体评估摘要
    """

    dimensions: dict[str, dict]
    summary: str

    @model_validator(mode="after")
    def _validate_dimensions(self) -> "IntakeSchema":
        keys = set(self.dimensions.keys())
        expected = set(DIMENSION_KEYS)
        if keys != expected:
            missing = expected - keys
            extra = keys - expected
            raise ValueError(
                f"dimensions 必须包含且仅包含 6 个固定 key。"
                f"缺失={sorted(missing)}, 多余={sorted(extra)}"
            )
        for k, v in self.dimensions.items():
            if not isinstance(v, dict):
                raise ValueError(f"维度 '{k}' 的值必须是 dict")
            if "score" not in v or "missing" not in v:
                raise ValueError(f"维度 '{k}' 必须包含 'score' 和 'missing' 字段")
            score = v["score"]
            if not isinstance(score, (int, float)) or isinstance(score, bool):
                raise ValueError(f"维度 '{k}' 的 score 必须是数字")
            if not (0 <= score <= 1):
                raise ValueError(f"维度 '{k}' 的 score 必须在 0-1 之间，当前={score}")
            if not isinstance(v["missing"], str):
                raise ValueError(f"维度 '{k}' 的 missing 必须是字符串")
        return self


