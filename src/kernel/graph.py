# [C 2026-09-08] M1 内核骨架 - 图装配
# [C 2026-09-09] M6 纵切联调 - 注册 nodes 包构建的 6 个真实节点，build_graph(deps, db_path)
# [C 2026-09-10] 插入 prd_review 评审门：prd_generation → prd_review，
#     条件边按硬判 verdict 回 prd_generation（打回，最多 3 轮）或去 artifact_persist
"""LangGraph 图装配：纵切 7 节点真实接线 + requirement_confirm HITL 中断。

节点函数由 nodes.build_nodes(deps) 构建（依赖通过 NodeDeps 注入）。
图结构：kb_lookup → intake → requirement_confirm(HITL)
→ needs_discovery → prd_generation → prd_review
    →（verdict == "reject" 回 prd_generation 重写，最多 3 轮）
    →（pass / pass_with_warning / 第 3 轮强制放行）artifact_persist → END。
"""
from typing import Any

from langgraph.graph import END, StateGraph

from kernel.checkpointer import get_checkpointer
from kernel.state import PMState

# 注：route_after_review 与 build_nodes 一样在 build_graph() 内延迟导入，
# 避免 kernel <-> nodes 循环导入（见下方函数注释）。 [C 2026-09-10]


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
    from nodes.review import route_after_review  # [C 2026-09-10] 评审门条件边

    nodes = build_nodes(deps)

    graph = StateGraph(PMState)

    # 注册节点（节点名与 build_nodes 返回的 key 一致）
    for node_name, node_fn in nodes.items():
        graph.add_node(node_name, node_fn)

    # 设置入口
    graph.set_entry_point("kb_lookup")

    # 线性边（prd_generation → artifact_persist 直连已删除，改走评审门）
    graph.add_edge("kb_lookup", "intake")
    graph.add_edge("intake", "requirement_confirm")
    graph.add_edge("requirement_confirm", "needs_discovery")
    graph.add_edge("needs_discovery", "prd_generation")
    # [C 2026-09-10] PRD 生成后先进评审门；打回回 prd_generation 重写（最多 3 轮），
    # 通过/带警告通过/第 3 轮强制放行则落盘
    graph.add_edge("prd_generation", "prd_review")
    graph.add_conditional_edges(
        "prd_review",
        route_after_review,
        {"prd_generation": "prd_generation", "artifact_persist": "artifact_persist"},
    )
    graph.add_edge("artifact_persist", END)

    # 编译 + SQLite 断点持久化
    checkpointer = get_checkpointer(db_path) if db_path else get_checkpointer()
    return graph.compile(checkpointer=checkpointer)


# [C 2026-09-09] graph.py M6 真实节点接线完成
