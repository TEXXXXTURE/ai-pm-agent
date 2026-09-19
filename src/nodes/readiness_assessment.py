# 就绪度打分节点（readiness_assessment）
"""发布前就绪度打分（R11）：模型对 11 维度各打 0-5 分，代码硬算加权均分与三级阻断。

图位置（第 9 段，launch_plan 与 launch_confirm 之间）：
    launch_plan -> readiness_assessment -> launch_confirm

节点不调模型做判定——模型只产出 11 个维度的分数与证据；
加权均分、6 档结论、三级阻断条件全部由 ``score_readiness`` 纯函数硬判。
节点不做路由分支（无二元裁决），产出后直连下游 launch_confirm。
"""
from __future__ import annotations

from kernel.spec import NodeSpec

# ── 11 维度权重（来自 AI PM Playbook，eval readiness 权重最高） ──
DIMENSION_WEIGHTS: dict[str, float] = {
    "problem_fit": 1.1,
    "workflow_fit": 1.1,
    "ai_job_definition": 1.2,
    "data_readiness": 1.2,
    "eval_readiness": 1.5,
    "system_behavior": 1.0,
    "risk_and_safety": 1.4,
    "regulatory_readiness": 1.3,
    "cost_and_business_case": 1.0,
    "observability": 1.3,
    "launch_and_operations": 1.2,
}

# 11 个维度字段名（用于遍历与校验）
DIMENSIONS: tuple[str, ...] = tuple(DIMENSION_WEIGHTS.keys())


def _readiness_level(weighted_avg: float) -> tuple[str, str]:
    """按加权均分返回 (档位标签, 含义说明)。

    6 档（来自 AI PM Playbook）：
    - <2.0 未就绪
    - 2.0-2.69 仅原型
    - 2.7-3.29 试点候选
    - 3.3-3.69 试点就绪（有条件）
    - 3.7-4.49 限量生产就绪
    - 4.5-5.0 可规模化
    """
    if weighted_avg < 2.0:
        return ("not_ready", "未就绪：不应构建或发布")
    if weighted_avg < 2.7:
        return ("prototype_only", "仅原型：只做内部探索")
    if weighted_avg < 3.3:
        return ("pilot_candidate", "试点候选：值得准备试点，但先消除阻断项")
    if weighted_avg < 3.7:
        return ("pilot_ready", "试点就绪（有条件）：限量开放并密切观察")
    if weighted_avg < 4.5:
        return ("limited_production", "限量生产就绪：受控的面向客户发布")
    return ("scale_ready", "可规模化：可在监控下扩大")


def _blocker_reasons(scores: dict[str, int]) -> list[str]:
    """三级阻断条件硬判：返回命中的阻断项列表。

    阻断面向客户生产：
    - eval_readiness < 3
    - risk_and_safety < 3（高影响动作无人审/风险无责任人等价于风险维度不足 3）
    - observability < 3
    - regulatory_readiness < 3（监管分类未定/合规路径不明）
    - launch_and_operations < 3（无分阶段放量计划）
    其余维度 < 3 记 warning 不阻断。
    """
    blockers: list[str] = []

    eval_score = scores.get("eval_readiness", 0)
    if eval_score < 3:
        blockers.append(
            f"评测就绪度 {eval_score}/5 < 3：无法测量质量，不应面向客户发布"
        )

    risk_score = scores.get("risk_and_safety", 0)
    if risk_score < 3:
        blockers.append(
            f"风险与安全 {risk_score}/5 < 3：高风险动作无人审核或风险无责任人"
        )

    obs_score = scores.get("observability", 0)
    if obs_score < 3:
        blockers.append(
            f"可观测性 {obs_score}/5 < 3：无法在生产环境中看到 AI 是否正常工作"
        )

    reg_score = scores.get("regulatory_readiness", 0)
    if reg_score < 3:
        blockers.append(
            f"监管就绪 {reg_score}/5 < 3：风险分类或合规路径未确定"
        )

    launch_score = scores.get("launch_and_operations", 0)
    if launch_score < 3:
        blockers.append(
            f"发布与运营 {launch_score}/5 < 3：无分阶段放量计划或上线后运营节奏"
        )

    return blockers


def score_readiness(assessment: dict) -> dict:
    """纯函数：从 11 维度打分算出加权均分、档位、阻断项。

    Args:
        assessment: 符合 ReadinessAssessmentSchema 的 dict（11 个维度各含 score/evidence/risk/owner/next_action）。

    Returns:
        dict 含：
        - ``weighted_avg``：加权均分（float，保留两位小数）
        - ``level``：档位标签（not_ready / prototype_only / pilot_candidate / pilot_ready / limited_production / scale_ready）
        - ``level_label``：档位中文含义
        - ``blockers``：阻断项列表（命中三级阻断条件的维度）
        - ``low_dimensions``：低于 3 分的维度列表（含分数，供展示）
        - ``dimension_scores``：11 维度分数汇总（{dim: score}）
    """
    if not isinstance(assessment, dict):
        assessment = {}

    dimension_scores: dict[str, int] = {}
    for dim in DIMENSIONS:
        entry = assessment.get(dim)
        if isinstance(entry, dict):
            raw = entry.get("score", 0)
            try:
                score = int(raw)
            except (TypeError, ValueError):
                score = 0
            score = max(0, min(5, score))
        else:
            score = 0
        dimension_scores[dim] = score

    total_weight = sum(DIMENSION_WEIGHTS.values())
    weighted_sum = sum(
        dimension_scores[dim] * DIMENSION_WEIGHTS[dim] for dim in DIMENSIONS
    )
    weighted_avg = round(weighted_sum / total_weight, 2)

    level, level_label = _readiness_level(weighted_avg)
    blockers = _blocker_reasons(dimension_scores)
    low_dimensions = [
        {"dimension": dim, "score": score}
        for dim, score in dimension_scores.items()
        if score < 3
    ]

    return {
        "weighted_avg": weighted_avg,
        "level": level,
        "level_label": level_label,
        "blockers": blockers,
        "low_dimensions": low_dimensions,
        "dimension_scores": dimension_scores,
    }


def make_readiness_assessment(deps):
    """就绪度打分节点工厂：返回签名 (state: dict) -> dict 的节点函数。"""

    def readiness_assessment(state: dict) -> dict:
        prompt = deps.registry.read_prompt("readiness_assessment")
        schema = deps.registry.load_schema("readiness_assessment")
        spec = NodeSpec(
            name="readiness_assessment",
            prompt_template=prompt,
            output_schema=schema,
        )

        result = deps.runner.run_raw(spec, state)
        computed = score_readiness(result)

        assessment_record = {
            **result,
            **computed,
        }

        return {
            "readiness_assessment": assessment_record,
        }

    return readiness_assessment


