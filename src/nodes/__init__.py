# [C 2026-09-09] M6 纵切联调 - nodes 包：NodeDeps 依赖容器 + build_nodes 汇总
# [C 2026-09-10] 新增 prd_review 评审门节点：prd_generation 之后、artifact_persist 之前
"""nodes 包：纵切 7 节点真实接线。

- NodeDeps：节点依赖容器（runner / registry / artifacts / kb）；
- build_nodes(deps)：返回有序 dict，key 顺序即图执行顺序：
  kb_lookup → intake → requirement_confirm → needs_discovery
  → prd_generation → prd_review →（条件边：打回回 prd_generation / 否则）artifact_persist。
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


@dataclass
class NodeDeps:
    """节点依赖容器：所有节点通过闭包访问这四个依赖。"""

    runner: NodeRunner
    registry: ComponentRegistry
    artifacts: ArtifactManager
    kb: KBStore


def build_nodes(deps: NodeDeps) -> dict:
    """构建 7 个节点函数的有序 dict（key 顺序与图执行顺序一致）。"""
    return {
        "kb_lookup": make_kb_lookup(deps),
        "intake": make_intake(deps),
        "requirement_confirm": make_requirement_confirm(deps),
        "needs_discovery": make_needs_discovery(deps),
        "prd_generation": make_prd_generation(deps),
        # [C 2026-09-10] 评审门插在 PRD 生成与落盘之间，条件边由 graph.py 装配
        "prd_review": make_prd_review(deps),
        "artifact_persist": make_artifact_persist(deps),
    }


# [C 2026-09-09] nodes 包汇总完成
