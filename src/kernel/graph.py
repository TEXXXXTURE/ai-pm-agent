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
# [C 2026-09-12 by codebuddy-ds41flash] S033 块3：插入判断需求与 AI 的边界两节点（13 节点）。
#     requirement_confirm 条件边分流：ai_core=True → feasibility_check → feasibility_confirm(HITL)
#     → 四态条件边（pass/reclassify→prd_generation；reshape→requirement_confirm；abandon→END）；
#     ai_core=False/None → needs_discovery（原路径，普通轨行为不变）。
# [C 2026-09-12 by codebuddy-ds41flash] 第 5 段：插入设计评测体系两节点（15 节点）。
#     prd_review 条件边三态：reject→prd_generation；非 reject 且 ai_core=True→eval_design；
#     其余→issue_splitting（普通轨逐字不变）。eval_design→eval_confirm(HITL)→两态条件边
#     （pass→issue_splitting；redraft→eval_design）。
# [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：插入对比选型节点（17 节点）。
#     eval_confirm pass 改映射为 bake_off；bake_off 条件边 {issue_splitting, bake_off}（自环重跑）；
#     普通轨不经此节点，行为不变。
# [C 2026-09-14 by codebuddy-ds41flash] S040 块1：分流点从"需求确认门出口"后移到"挖需求出口"——
#     requirement_confirm 改为无条件普通边进 needs_discovery；needs_discovery 加条件边按 ai_core
#     分流（True→feasibility_check；False/None→prd_generation）；feasibility_confirm 四态路由不变。
#     节点数仍 17 不变，普通轨实际执行路径逐字不变（确认 → 挖需求 → 写 PRD）。
# [C 2026-09-14 by codebuddy-ds41flash] S041：requirement_confirm 与 needs_discovery 之间插
#     requirement_refine 整合节点（18 节点）。确认门出口改条件边——pending=True（feedback/
#     改判带附言）走 requirement_refine（调模型整合 + HITL 确认）；pending=False（confirm/
#     纯改判）走 needs_discovery（原路径，普通轨行为逐字不变）。整合节点条件边三态：
#     confirm/reclassify→needs_discovery；feedback→自环重整合（前 2 版自动，第 3 版升级暂停）；
#     abandon→END。普通轨实际执行路径逐字不变（确认 → 挖需求 → 写 PRD）。
# [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 修复单：第 8 段拆两步（19 节点）。
#     eval_run 改为普通边到 eval_gate（只跑与记录，达标与否都 return 三状态字段）；
#     eval_gate 条件边两态（passed is True → launch_plan；其余 → eval_run 重跑）。
#     未达标的停等从 eval_run 内部移到 eval_gate；普通轨仍由 issue_confirm 直达 launch_plan，逐字不变。
"""LangGraph 图装配：纵切 20 节点真实接线 + requirement_confirm / requirement_refine /
feasibility_confirm / eval_confirm / issue_confirm / launch_confirm 六扇 HITL 门。

节点函数由 nodes.build_nodes(deps) 构建（依赖通过 NodeDeps 注入）。
图结构：kb_lookup → intake → requirement_confirm(HITL)
→（条件边：requirement_refine_pending=True → requirement_refine(HITL 整合节点)
    条件边三态：confirm/reclassify → needs_discovery；
                feedback → requirement_refine 自环（前 2 版自动，第 3 版起升级暂停）；
                abandon → END
   ／ requirement_refine_pending=False → needs_discovery（两轨都先挖需求））
→（条件边：ai_core=True → feasibility_check → feasibility_confirm(HITL)
    四态：pass/reclassify → prd_generation；reshape → requirement_confirm；abandon → END
   ／ ai_core=False/None → prd_generation）
→ prd_generation → prd_review
    →（verdict == "reject" 回 prd_generation 重写，最多 3 轮）
    →（非 reject 且 ai_core=True）eval_design → eval_confirm(HITL 确认评测体系门)
        → 条件边两态：pass → bake_off（确认落盘 YAML 草案与评测档案进 state）
                       redraft → eval_design（修改意见重起草；前 2 轮自动，第 3 版起升级暂停）
    →（非 reject 且普通轨）issue_splitting
    → bake_off（第 6 段对比选型：经 Promptfoo 横跑候选，代码硬判推荐，写 model_selection）
        → 条件边两态：issue_splitting（横跑完成/人工跳过）
                       bake_off（工具错误在节点内 interrupt，恢复后自环重跑）
    → issue_confirm(HITL 工单确认门) → 条件边三分支：
        eval_run（确认 且 ai_core=True：第 8 段构建期跑评测）
        launch_plan（确认 且普通轨：route 返回 "artifact_persist" 语义值映射到 launch_plan 节点）
        issue_splitting（修改意见打回重拆；前 2 轮自动，第 3 版起升级暂停）
        prd_generation（"回PRD"回炉重写，全程限 1 次；重写后自动复审→重拆→重回确认门）
    → eval_run（第 8 段前半：构建期跑评测 + 记录，AI 核心需求经此）
        → 普通边 eval_gate（第 8 段后半：判定 + 停等，纯函数）
            → 条件边两态：launch_plan（达标放行）／eval_run（未达标/记录缺失回重跑；
              未达标在 eval_gate 内 interrupt，恢复后经条件边回 eval_run 重跑，不设自动放行）
    → launch_plan（第 9 段：写发布计划）→ readiness_assessment（R11 就绪度打分：11 维度 0-5 分 + 加权均分 + 三级阻断）
    → launch_confirm(HITL 发布计划确认门) → 条件边三分支：
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
    from nodes.exploration import (  # [C 2026-09-14 by codebuddy-ds41flash] 挖需求后分流条件边
        route_after_needs_discovery,
    )
    from nodes.feasibility import (  # [C 2026-09-12 by codebuddy-ds41flash] 判断需求与 AI 的边界路由
        route_after_feasibility_confirm,
    )
    from nodes.eval_design import (  # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门条件边
        route_after_eval_confirm,
    )
    from nodes.eval_run import (  # [C 2026-09-12 by codebuddy-ds41flash] 构建期跑评测条件边
        route_after_eval_gate,  # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 由 route_after_eval_run 改名
    )
    from nodes.bake_off import (  # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段对比选型条件边
        route_after_bake_off,
    )
    from nodes.refine import (  # [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点条件边
        route_after_requirement_confirm,
        route_after_requirement_refine,
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
    # [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点接入：
    # 确认门出口改条件边——pending=True（feedback/改判带附言）走 requirement_refine；
    # pending=False（confirm/纯改判）走 needs_discovery（原路径，普通轨行为逐字不变）。
    graph.add_conditional_edges(
        "requirement_confirm",
        route_after_requirement_confirm,
        {
            "needs_discovery": "needs_discovery",
            "requirement_refine": "requirement_refine",
        },
    )
    # [C 2026-09-14 by codebuddy-ds41flash] S041 整合节点条件边三态：
    # confirm/reclassify -> needs_discovery（整合后需求写回 confirmed_requirement，eval_cases 重初始化）；
    # feedback -> requirement_refine 自环（带新意见重整合，前 2 版自动，第 3 版升级暂停）；
    # abandon -> END（放弃，留档当前草案与意见）。
    graph.add_conditional_edges(
        "requirement_refine",
        route_after_requirement_refine,
        {
            "needs_discovery": "needs_discovery",
            "requirement_refine": "requirement_refine",
            END: END,
        },
    )
    # [C 2026-09-14 by codebuddy-ds41flash] S040 块1：挖需求后按 ai_core 分流——
    # ai_core=True 走 AI 轨先判断需求与 AI 的边界（feasibility_check→feasibility_confirm）；
    # 其余走普通轨直接写 PRD（普通轨实际路径逐字不变：确认 → 挖需求 → 写 PRD）
    graph.add_conditional_edges(
        "needs_discovery",
        route_after_needs_discovery,
        {
            "feasibility_check": "feasibility_check",
            "prd_generation": "prd_generation",
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
        # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：确认（route 返回语义值 "issue_splitting"）
        # 改映射为 bake_off（对比选型）；修改意见 -> eval_design 重起草（前 2 轮自动，第 3 版起升级暂停）。
        # 不改 route_after_eval_confirm 纯函数（保持其"确认语义值=issue_splitting"），只在 graph 换目标节点。
        # 升级暂停靠节点内部第二次 interrupt 实现，二次答复最终只剩 pass/feedback 两类，
        # 不会返回 graph 未映射的值，故无需 escalated 映射项。
        {
            "eval_design": "eval_design",
            "issue_splitting": "bake_off",
        },
    )
    # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段对比选型（AI 核心需求经此，普通轨不经）：
    # 横跑完成或人工跳过 -> issue_splitting；工具错误/待 prompt 在节点内 interrupt，
    # route 保守兜底自环 bake_off->bake_off（恢复后重跑本节点）。
    graph.add_conditional_edges(
        "bake_off",
        route_after_bake_off,
        {
            "issue_splitting": "issue_splitting",
            "bake_off": "bake_off",
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
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 修复单：第 8 段拆两步（AI 核心需求经此，
    # 普通轨不经）。eval_run 只跑与记录（三种前置暂停留在节点内），无条件走普通边到 eval_gate；
    # eval_gate 判定达标放行 / 未达标在节点内 interrupt，恢复后由条件边回 eval_run 重跑（口径一致：
    # 不设自动放行）。route_after_eval_gate 的判定规则与原 route_after_eval_run 一字不改。
    graph.add_edge("eval_run", "eval_gate")
    graph.add_conditional_edges(
        "eval_gate",
        route_after_eval_gate,
        {
            "launch_plan": "launch_plan",
            "eval_run": "eval_run",
        },
    )
    # [C 2026-09-11] 块2：launch_plan 产出后先进就绪度打分，再进发布计划确认门（HITL）
    # [C 2026-09-16] R11：launch_plan -> readiness_assessment -> launch_confirm
    graph.add_edge("launch_plan", "readiness_assessment")
    graph.add_edge("readiness_assessment", "launch_confirm")
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
