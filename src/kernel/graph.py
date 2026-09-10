# [C 2026-09-08] M1 内核骨架 - 图装配
# [C 2026-09-09] M6 纵切联调 - 注册 nodes 包构建的 6 个真实节点，build_graph(deps, db_path)
"""LangGraph 图装配：纵切 6 节点真实接线 + requirement_confirm HITL 中断。

节点函数由 nodes.build_nodes(deps) 构建（依赖通过 NodeDeps 注入），
图结构保持线性：kb_lookup → intake → requirement_confirm(HITL)
→ needs_discovery → prd_generation → artifact_persist → END。
"""
from typing import Any

from langgraph.graph import END, StateGraph

from kernel.checkpointer import get_checkpointer
from kernel.state import PMState


def build_graph(deps: Any, db_path: str | None = None) -> Any:
    """构建并编译 LangGraph 状态图。

    Args:
        deps: nodes.NodeDeps 依赖容器（runner / registry / artifacts / kb）。
        db_path: SQLite 检查点数据库路径；None 时使用默认路径。

    Returns:
        编译后的 LangGraph（带 SqliteSaver checkpointer，支持断点续跑）。
    """
    # 延迟导入：nodes 依赖 kernel，kernel.__init__ 又会导入本模块，
    # 放函数内导入可避免 kernel <-> nodes 循环导入。 [C 2026-09-09]
    from nodes import build_nodes

    nodes = build_nodes(deps)

    graph = StateGraph(PMState)

    # 注册节点（节点名与 build_nodes 返回的 key 一致）
    for node_name, node_fn in nodes.items():
        graph.add_node(node_name, node_fn)

    # 设置入口
    graph.set_entry_point("kb_lookup")

    # 线性边
    graph.add_edge("kb_lookup", "intake")
    graph.add_edge("intake", "requirement_confirm")
    graph.add_edge("requirement_confirm", "needs_discovery")
    graph.add_edge("needs_discovery", "prd_generation")
    graph.add_edge("prd_generation", "artifact_persist")
    graph.add_edge("artifact_persist", END)

    # 编译 + SQLite 断点持久化
    checkpointer = get_checkpointer(db_path) if db_path else get_checkpointer()
    return graph.compile(checkpointer=checkpointer)


# [C 2026-09-09] graph.py M6 真实节点接线完成
