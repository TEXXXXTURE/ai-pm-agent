# [C 2026-09-09] M6 纵切联调 - HITL 节点（requirement_confirm 需求确认门）
"""需求确认门节点：能力边界三色表 -> interrupt 请用户确认 -> 初始化评测用例。

注意：LangGraph resume 时节点会从头重跑（边界 LLM 会再调一次），这是已知可接受行为。
"""
from __future__ import annotations

from langgraph.types import interrupt

from components.tools.init_eval_cases import init_eval_cases
from kernel.spec import NodeSpec


def make_requirement_confirm(deps):
    """需求确认门（HITL）：结构化能力边界 + 用户确认。"""

    def requirement_confirm(state: dict) -> dict:
        # 1. 能力边界三色表（auto / tool / manual）
        prompt = deps.registry.read_prompt("capability_boundary")
        schema = deps.registry.load_schema("capability_boundary")
        spec = NodeSpec(
            name="capability_boundary",
            prompt_template=prompt,
            output_schema=schema,
        )
        boundary = deps.runner.run_raw(spec, state)

        # 2. 中断，请用户确认/修改需求
        user_input = interrupt(
            value={
                "node": "requirement_confirm",
                "requirement_name": state.get("requirement_name", ""),
                "raw_requirement": state.get("raw_requirement", ""),
                "info_completeness": state.get("info_completeness"),
                "capability_boundary": boundary,
            }
        )

        # 3. resume 值：空或 "confirmed"（不区分大小写）视为确认原需求，否则用用户文本
        original = user_input if isinstance(user_input, str) else str(user_input)
        text = original.strip()
        if not text or text.lower() == "confirmed":
            confirmed = state.get("raw_requirement", "")
        else:
            confirmed = text

        # 4. 初始化评测用例（纯确定性模板生成）
        eval_cases = init_eval_cases({**state, "confirmed_requirement": confirmed})

        return {
            "capability_boundary": boundary,
            "confirmed_requirement": confirmed,
            "proceed_decision": True,
            "eval_cases": eval_cases,
            "human_feedback": (state.get("human_feedback") or [])
            + [{"node": "requirement_confirm", "feedback": original}],
            "current_stage": "探索",
        }

    return requirement_confirm


# [C 2026-09-09] nodes/hitl.py 实现完成
