# [C 2026-09-09] M6 纵切联调 - nodes 包：NodeDeps 依赖容器 + build_nodes 汇总
# [C 2026-09-10] 新增 prd_review 评审门节点：prd_generation 之后、artifact_persist 之前
# [C 2026-09-11] 新增 issue_splitting 拆研发工单节点（块1，8 节点）：
#     prd_review 通过类 -> issue_splitting -> artifact_persist 直连（确认门块2再插）
# [C 2026-09-11] 块2 新增 issue_confirm 工单确认门（9 节点）：
#     issue_splitting -> issue_confirm(HITL) -> 条件边三分支
#     （确认落盘 / 意见回 issue_splitting 重拆 / 回PRD 回炉 prd_generation，回炉限 1 次）
# [C 2026-09-11] 块1 新增 launch_plan 发布计划节点（10 节点）：工单确认门确认分支走
#     launch_plan（route 返回 "artifact_persist" 语义值映射到 launch_plan 节点）。
# [C 2026-09-11] 块2 新增 launch_confirm 发布计划确认门（11 节点）：
#     launch_plan -> launch_confirm(HITL) -> 条件边三分支
#     （确认落盘 / 意见回 launch_plan 重调 / 回工单回 issue_splitting 重拆，回工单限 1 次）
# [C 2026-09-12 by MA] S033 块2a：requirement_confirm 内部 capability_boundary 调用换成
#     ai_triage 分流判定 + resume 四态协议（确认/非AI/AI核心/自由文本修订）；
#     prd_generation 按 ai_core 选 ai-native / 普通 PRD 模板。
#     图结构不动（仍 11 节点），仅在节点内部分流，块 3 可行性门才插新节点与条件边。
"""nodes 包：纵切 11 节点真实接线。

- NodeDeps：节点依赖容器（runner / registry / artifacts / kb）；
- build_nodes(deps)：返回有序 dict，key 顺序即图执行顺序：
  kb_lookup → intake → requirement_confirm(HITL，含 AI 适用性分流)
  → needs_discovery → prd_generation（按 ai_core 选 ai-native / 普通模板）
  → prd_review →（条件边：打回回 prd_generation / 否则）issue_splitting
  → issue_confirm（HITL；条件边：launch_plan / issue_splitting / prd_generation）
  → launch_plan → launch_confirm（HITL；条件边：artifact_persist / launch_plan / issue_splitting）。
"""
from __future__ import annotations

from dataclasses import dataclass

from components.registry import ComponentRegistry
from kernel.artifact import ArtifactManager
from kernel.runner import NodeRunner
from kb.store import KBStore

from nodes.artifact import make_artifact_persist
from nodes.exploration import make_intake, make_kb_lookup, make_needs_discovery
from nodes.hitl import make_requirement_confirm
from nodes.prd import make_prd_generation
from nodes.review import make_prd_review  # [C 2026-09-10] PRD 评审门节点
from nodes.issues import (  # [C 2026-09-11] 拆研发工单 + 工单确认门
    make_issue_confirm,
    make_issue_splitting,
)
from nodes.launch_plan import (  # [C 2026-09-11] 发布计划 + 发布计划确认门
    make_launch_confirm,
    make_launch_plan,
)


@dataclass
class NodeDeps:
    """节点依赖容器：所有节点通过闭包访问这四个依赖。"""

    runner: NodeRunner
    registry: ComponentRegistry
    artifacts: ArtifactManager
    kb: KBStore


def build_nodes(deps: NodeDeps) -> dict:
    """构建 11 个节点函数的有序 dict（key 顺序与图执行顺序一致）。"""
    return {
        "kb_lookup": make_kb_lookup(deps),
        "intake": make_intake(deps),
        "requirement_confirm": make_requirement_confirm(deps),
        "needs_discovery": make_needs_discovery(deps),
        "prd_generation": make_prd_generation(deps),
        # [C 2026-09-10] 评审门插在 PRD 生成之后，条件边由 graph.py 装配
        "prd_review": make_prd_review(deps),
        # [C 2026-09-11] 块1 评审通过类去拆单；块2 拆单后先进工单确认门再落盘
        "issue_splitting": make_issue_splitting(deps),
        # [C 2026-09-11] 块2 工单确认门（HITL，不调模型），三分支条件边由 graph.py 装配
        "issue_confirm": make_issue_confirm(deps),
        # [C 2026-09-11] 块1 发布计划节点（工单确认门通过后产计划）
        "launch_plan": make_launch_plan(deps),
        # [C 2026-09-11] 块2 发布计划确认门（HITL，不调模型），三分支条件边由 graph.py 装配：
        # 确认落盘 launch_plan.md / 意见回 launch_plan 重调 / 回工单回 issue_splitting 重拆
        "launch_confirm": make_launch_confirm(deps),
        "artifact_persist": make_artifact_persist(deps),
    }


# [C 2026-09-09] nodes 包汇总完成
