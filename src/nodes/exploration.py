# [C 2026-09-09] M6 纵切联调 - 探索阶段节点（kb_lookup / intake / needs_discovery）
"""探索阶段节点工厂：每个 make_xxx(deps) 返回一个签名 (state: dict) -> dict 的节点函数。"""
from __future__ import annotations

from kernel.spec import NodeSpec


def make_kb_lookup(deps):
    """G4 查家底（确定性，不调模型）：检索知识库相关档案。"""

    def kb_lookup(state: dict) -> dict:
        result = deps.kb.retrieve_relevant(state.get("raw_requirement", ""))
        return {"kb_context": result, "current_stage": "探索"}

    return kb_lookup


def make_intake(deps):
    """需求接收：6 维度评估信息完整度。"""

    def intake(state: dict) -> dict:
        prompt = deps.registry.read_prompt("intake")
        schema = deps.registry.load_schema("intake")
        spec = NodeSpec(name="intake", prompt_template=prompt, output_schema=schema)
        result = deps.runner.run_raw(spec, state)
        return {"info_completeness": result}

    return intake


def make_needs_discovery(deps):
    """需求挖掘：从用户脑中挖信息（6 类洞察）。"""

    def needs_discovery(state: dict) -> dict:
        prompt = deps.registry.read_prompt("needs_discovery")
        schema = deps.registry.load_schema("needs_discovery")
        spec = NodeSpec(name="needs_discovery", prompt_template=prompt, output_schema=schema)
        result = deps.runner.run_raw(spec, state)
        return {"user_insights": result}

    return needs_discovery


# [C 2026-09-09] nodes/exploration.py 实现完成
