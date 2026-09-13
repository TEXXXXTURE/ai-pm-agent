# [C 2026-09-09] M6 纵切联调 - 探索阶段节点（kb_lookup / intake / needs_discovery）
# [C 2026-09-12 by codebuddy-ds41flash] R02：kb_lookup 追加 AI 领域知识库（kb.rag）检索，写入 domain_kb_context
"""探索阶段节点工厂：每个 make_xxx(deps) 返回一个签名 (state: dict) -> dict 的节点函数。"""
from __future__ import annotations

from kernel.spec import NodeSpec


def make_kb_lookup(deps):
    """G4 查家底（确定性，不调大模型）：检索业务档案库 + AI 领域知识库（R02）。"""

    def kb_lookup(state: dict) -> dict:
        result = deps.kb.retrieve_relevant(state.get("raw_requirement", ""))
        update = {"kb_context": result, "current_stage": "探索"}
        rag_store = getattr(deps, "rag", None)
        if rag_store is None:
            update["domain_kb_context"] = []
            return update
        query = state.get("raw_requirement", "")
        try:
            top_k = int(getattr(rag_store, "default_top_k", 5) or 5)
            update["domain_kb_context"] = rag_store.search(query, top_k=top_k)
        except Exception:
            update["domain_kb_context"] = []
        return update

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


def route_after_needs_discovery(state: dict) -> str:
    """挖需求后的条件边路由：AI 核心需求去验证AI可行性，其余直接写 PRD。

    - ``ai_core is True`` -> ``feasibility_check``
    - 其余（False / None / 缺失）-> ``prd_generation``（普通轨）
    """
    return "feasibility_check" if state.get("ai_core") is True else "prd_generation"
    # [C 2026-09-14 by codebuddy-ds41flash] 挖需求出口条件边路由纯函数（S040 块1 分流点后移）


# [C 2026-09-09] nodes/exploration.py 实现完成
