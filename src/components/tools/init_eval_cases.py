# [C 2026-09-09] M6 纵切联调 - 评测用例初始化工具
"""init_eval_cases：需求确认门后，按模板确定性生成 4 条初始评测用例。

纯函数、不调模型；依据 confirmed_requirement（无则回退 raw_requirement）
与 requirement_name 生成，覆盖：需求复述 / 六维度覆盖 / 无技术术语 / HITL 三色确认门。
"""
from __future__ import annotations


def init_eval_cases(state: dict) -> list[dict]:
    """生成 4 条初始评测用例。

    Returns:
        list[dict]，每条含 id / type / question / source 四个字段。
    """
    requirement = state.get("confirmed_requirement") or state.get("raw_requirement") or ""
    name = state.get("requirement_name") or "该需求"

    return [
        {
            "id": "eval-1",
            "type": "golden_qa",
            "question": (
                f"Agent 对需求「{requirement}」（需求名：{name}）的复述是否准确，"
                "无遗漏关键目标、用户与约束？"
            ),
            "source": "G2-auto",
        },
        {
            "id": "eval-2",
            "type": "completeness",
            "question": (
                f"针对需求「{requirement}」生成的 PRD 是否完整覆盖 6 个维度："
                "目标用户、核心场景、痛点、现有方案、成功标准、约束条件？"
            ),
            "source": "G2-auto",
        },
        {
            "id": "eval-3",
            "type": "quality_gate",
            "question": (
                "PRD 全文是否不包含 API、字段名、错误码、数据库、HTTP、接口、endpoint "
                "等技术实现表述，全部以页面元素与交互说明描述？"
            ),
            "source": "G2-auto",
        },
        {
            "id": "eval-4",
            "type": "hitl",
            "question": (
                "需求确认门是否输出 auto/tool/manual 三色能力边界表，"
                "并在用户明确确认需求后才进入需求挖掘与文档生成？"
            ),
            "source": "G2-auto",
        },
    ]


# [C 2026-09-09] tools/init_eval_cases.py 实现完成
