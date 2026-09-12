# [C 2026-09-08] M1 内核骨架 - 图装配
# [C 2026-09-09] M6 纵切联调 - 注册 nodes 包构建的 6 个真实节点，build_graph(deps, db_path)
# [C 2026-09-10] 插入 prd_review 评审门：prd_generation → prd_review，
#     条件边按硬判 verdict 回 prd_generation（打回，最多 3 轮）或去 artifact_persist
# [C 2026-09-11] 块1 插入 issue_splitting：评审通过分支改走拆单，拆单后直连 artifact_persist
#     （人工确认门块2再插在 issue_splitting → artifact_persist 之间）
# [C 2026-09-11] 块2 插入 issue_confirm 工单确认门：拆单先进确认门，条件边三分支
#     （确认落盘 / 意见回 issue_splitting 重拆 / 回PRD 回炉 prd_generation，回炉限 1 次、
#       第 3 版仍有意见进入升级暂停中断，由人主动发起下一步，不自动空转）
# [C 2026-09-11] 块1 插入 launch_plan 发布计划节点：issue_confirm 确认分支改走
#     launch_plan（route 返回 "artifact_persist" 语义值映射到 launch_plan 节点）。
# [C 2026-09-11] 块2 插入 launch_confirm 发布计划确认门：launch_plan 产出后先进确认门，
#     条件边三分支（确认落盘 launch_plan.md / 意见回 launch_plan 重调 / 回工单回
#     issue_splitting 重拆，回工单限 1 次、第 3 版仍有意见进入升级暂停中断）
# [C 2026-09-12 by MA] S033 块2a：requirement_confirm 内部 capability_boundary 调用换成
#     ai_triage 分流判定 + resume 四态协议；prd_generation 按 state["ai_core"] 选
#     ai-native / 普通 PRD 模板。图结构不动（仍 11 节点），分流判定在确认门节点内部完成，
#     模板选择在 prd_generation 节点内部完成；块 3 可行性门才新增节点与条件边。
# [C 2026-09-12 by codebuddy-ds41flash] S033 块3：插入验证AI可行性两节点（13 节点）。
#     requirement_confirm 条件边分流：ai_core=True → feasibility_check → feasibility_confirm(HITL)
#     → 四态条件边（pass/reclassify→prd_generation；reshape→requirement_confirm；abandon→END）；
#     ai_core=False/None → needs_discovery（原路径，普通轨行为不变）。
# [C 2026-09-12 by codebuddy-ds41flash] 第 5 段：插入设计评测体系两节点（15 节点）。
#     prd_review 条件边三态：reject→prd_generation；非 reject 且 ai_core=True→eval_design；
#     其余→issue_splitting（普通轨逐字不变）。eval_design→eval_confirm(HITL)→两态条件边
#     （pass→issue_splitting；redraft→eval_design）。
"""LangGraph 图装配：纵切 16 节点真实接线 + requirement_confirm / feasibility_confirm / eval_confirm / issue_confirm / launch_confirm 五扇 HITL 门。

节点函数由 nodes.build_nodes(deps) 构建（依赖通过 NodeDeps 注入）。
图结构：kb_lookup → intake → requirement_confirm(HITL)
→（条件边：ai_core=True → feasibility_check → feasibility_confirm(HITL)
    四态：pass/reclassify → prd_generation；reshape → requirement_confirm；abandon → END
   ／ ai_core=False/None → needs_discovery → prd_generation）
→ prd_generation → prd_review
    →（verdict == "reject" 回 prd_generation 重写，最多 3 轮）
    →（非 reject 且 ai_core=True）eval_design → eval_confirm(HITL 确认评测体系门)
        → 条件边两态：pass → issue_splitting（确认落盘 YAML 草案与评测档案进 state）
                       redraft → eval_design（修改意见重起草；前 2 轮自动，第 3 版起升级暂停）
    →（非 reject 且普通轨）issue_splitting
    → issue_confirm(HITL 工单确认门) → 条件边三分支：
        eval_run（确认 且 ai_core=True：第 8 段构建期跑评测）
        launch_plan（确认 且普通轨：route 返回 "artifact_persist" 语义值映射到 launch_plan 节点）
        issue_splitting（修改意见打回重拆；前 2 轮自动，第 3 版起升级暂停）
        prd_generation（"回PRD"回炉重写，全程限 1 次；重写后自动复审→重拆→重回确认门）
    → eval_run（第 8 段构建期跑评测，AI 核心需求经此）→ 条件边两态：
        launch_plan（达标放行）／eval_run（未达标/工具错误在节点内 interrupt，恢复后重跑自环）
    → launch_plan → launch_confirm(HITL 发布计划确认门) → 条件边三分支：
        artifact_persist（确认：落盘 launch_plan.md 及前序四产物）
        launch_plan（修改意见打回重调；前 2 轮自动，第 3 版起升级暂停）
        issue_splitting（"回工单"重拆工单，全程限 1 次；重拆后自动重生成计划→重回确认门）
    → artifact_persist → END。
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
    from nodes.issues import route_after_issue_confirm  # [C 2026-09-11] 块2 工单确认门条件边
    from nodes.launch_plan import route_after_launch_confirm  # [C 2026-09-11] 块2 发布计划确认门条件边
    from nodes.feasibility import (  # [C 2026-09-12 by codebuddy-ds41flash] 验证AI可行性路由
        route_after_feasibility_confirm,
        route_after_requirement_confirm,
    )
    from nodes.eval_design import (  # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门条件边
        route_after_eval_confirm,
    )
    from nodes.eval_run import (  # [C 2026-09-12 by codebuddy-ds41flash] 构建期跑评测条件边
        route_after_eval_run,
    )

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
    # [C 2026-09-12 by codebuddy-ds41flash] S033 块3：需求确认门后按分流走——
    # ai_core=True 先过验证AI可行性（feasibility_check→feasibility_confirm），
    # ai_core=False/None 走原路径 needs_discovery（普通轨行为不变）
    graph.add_conditional_edges(
        "requirement_confirm",
        route_after_requirement_confirm,
        {
            "feasibility_check": "feasibility_check",
            "needs_discovery": "needs_discovery",
        },
    )
    graph.add_edge("feasibility_check", "feasibility_confirm")
    # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门四态：
    # pass/reclassify -> prd_generation（reclassify 已把 ai_core 改 False，自动选普通模板）；
    # reshape -> requirement_confirm（回第 1 段改范围，限 1 次）；abandon -> END。
    graph.add_conditional_edges(
        "feasibility_confirm",
        route_after_feasibility_confirm,
        {
            "prd_generation": "prd_generation",
            "requirement_confirm": "requirement_confirm",
            END: END,
        },
    )
    graph.add_edge("needs_discovery", "prd_generation")
    # [C 2026-09-10] PRD 生成后先进评审门；打回回 prd_generation 重写（最多 3 轮），
    # 通过/带警告通过/第 3 轮强制放行则落盘
    graph.add_edge("prd_generation", "prd_review")
    graph.add_conditional_edges(
        "prd_review",
        route_after_review,
        # [C 2026-09-11] 通过分支去 issue_splitting 拆单（块1直连落盘，块2插确认门）
        # [C 2026-09-12 by codebuddy-ds41flash] 第 5 段三态：非 reject 且 ai_core=True
        # 先走 eval_design（设计评测体系），普通轨仍直接去 issue_splitting
        {
            "prd_generation": "prd_generation",
            "eval_design": "eval_design",
            "issue_splitting": "issue_splitting",
        },
    )
    # [C 2026-09-12 by codebuddy-ds41flash] 第 5 段：评测体系起草后先进确认门（HITL）
    graph.add_edge("eval_design", "eval_confirm")
    graph.add_conditional_edges(
        "eval_confirm",
        route_after_eval_confirm,
        # 确认 -> issue_splitting（落盘 YAML 草案与评测档案进 state）；
        # 修改意见 -> eval_design 重起草（前 2 轮自动，第 3 版起升级暂停）。
        # 升级暂停靠节点内部第二次 interrupt 实现，二次答复最终只剩 pass/feedback 两类，
        # 不会返回 graph 未映射的值，故无需 escalated 映射项。
        {
            "eval_design": "eval_design",
            "issue_splitting": "issue_splitting",
        },
    )
    # [C 2026-09-11] 块2：拆单后不再直连落盘，先进工单确认门（HITL）
    graph.add_edge("issue_splitting", "issue_confirm")
    graph.add_conditional_edges(
        "issue_confirm",
        route_after_issue_confirm,
        # 确认 -> launch_plan（route_after_issue_confirm 返回 "artifact_persist" 语义值
        # = 确认进落盘流程，graph 把它映射到 launch_plan 节点；不动 issues.py 纯函数）；
        # 修改意见 -> issue_splitting 重拆；回PRD -> prd_generation 回炉。
        # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：确认且 ai_core=True 时 route 返回
        # "eval_run"，先跑构建期评测；普通轨仍返回 "artifact_persist" 直达 launch_plan。
        {
            "issue_splitting": "issue_splitting",
            "prd_generation": "prd_generation",
            "artifact_persist": "launch_plan",
            "eval_run": "eval_run",
        },
    )
    # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：构建期跑评测（AI 核心需求经此，普通轨不经）。
    # 达标 -> launch_plan；未达标/工具错误/待 prompt 均在节点内部 interrupt，graph 拿到的
    # 只有 passed=True 的返回值；route 保守兜底自环 eval_run->eval_run（未达标恢复后重跑本节点）。
    graph.add_conditional_edges(
        "eval_run",
        route_after_eval_run,
        {
            "launch_plan": "launch_plan",
            "eval_run": "eval_run",
        },
    )
    # [C 2026-09-11] 块2：launch_plan 产出后不再直连落盘，先进发布计划确认门（HITL）
    graph.add_edge("launch_plan", "launch_confirm")
    graph.add_conditional_edges(
        "launch_confirm",
        route_after_launch_confirm,
        # 确认 -> artifact_persist（落盘 launch_plan.md 及前序四产物）；
        # 修改意见 -> launch_plan 重调（前 2 轮自动，第 3 版起升级暂停）；
        # 回工单 -> issue_splitting 重拆（全程限 1 次；重拆后自动重生成计划→重回本确认门）。
        # route_after_launch_confirm 不写"escalated"分支：升级暂停靠节点内部第二次
        # interrupt 实现，二次答复最终走 confirm/feedback/redo_issues 三路之一，
        # 不会返回 graph 未映射的值，故无需 escalated 映射项。 [C 2026-09-11]
        {
            "artifact_persist": "artifact_persist",
            "launch_plan": "launch_plan",
            "issue_splitting": "issue_splitting",
        },
    )
    graph.add_edge("artifact_persist", END)

    # 编译 + SQLite 断点持久化
    checkpointer = get_checkpointer(db_path) if db_path else get_checkpointer()
    return graph.compile(checkpointer=checkpointer)


# [C 2026-09-09] graph.py M6 真实节点接线完成
