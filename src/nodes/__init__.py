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
# [C 2026-09-12 by codebuddy-ds41flash] S033 块3：新增判断需求与 AI 的边界两节点（13 节点）：
#     requirement_confirm 条件边（ai_core=True）→ feasibility_check → feasibility_confirm(HITL)
#     → 四态条件边（pass/reclassify→prd_generation；reshape→requirement_confirm；abandon→END）；
#     普通轨（ai_core=False）由条件边直接去 needs_discovery，不经可行性节点。
# [C 2026-09-12 by codebuddy-ds41flash] 第 5 段：新增设计评测体系两节点（15 节点）：
#     prd_review 非 reject 且 ai_core=True → eval_design → eval_confirm(HITL)
#     → 两态条件边（pass→issue_splitting；redraft→eval_design）；
#     普通轨（ai_core=False/None）由 route_after_review 直接去 issue_splitting，不经评测节点。
# [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：新增对比选型节点（17 节点）：
#     eval_confirm pass → bake_off →（ran/skipped）issue_splitting；
#     bake_off 工具错误在节点内 interrupt，恢复后自环重跑；普通轨不经此节点。
# [C 2026-09-14 by codebuddy-ds41flash] S041：新增需求修订整合节点（18 节点）。
#     requirement_confirm 出口改条件边——pending=True（feedback/改判带附言）走 requirement_refine
#     （调模型整合 + HITL 确认/改判/放弃/带新意见重整合，前 2 版自动，第 3 版升级暂停）；
#     pending=False（confirm/纯改判）走 needs_discovery（原路径，普通轨行为逐字不变）。
"""nodes 包：纵切 18 节点真实接线。

- NodeDeps：节点依赖容器（runner / registry / artifacts / kb / rag / eval_tool / bake_off_config）；
- build_nodes(deps)：返回有序 dict，key 顺序即图执行顺序：
  kb_lookup → intake → requirement_confirm(HITL，含 AI 适用性分流)
  →（条件边：pending=True）requirement_refine(HITL 整合节点；条件边：confirm/reclassify→needs_discovery / feedback→自环 / abandon→END)
  →（条件边：pending=False）needs_discovery →（条件边 ai_core=True）feasibility_check → feasibility_confirm(HITL，四态)
  →（条件边 ai_core=False）prd_generation（按 ai_core 选 ai-native / 普通模板）
  → prd_review →（条件边：打回回 prd_generation / 非 reject 且 ai_core=True）eval_design
  → eval_confirm（HITL；条件边：pass→bake_off / redraft→eval_design）
  → bake_off（第 6 段对比选型，仅 AI 核心需求；条件边：issue_splitting / bake_off 自环）
  →（条件边：非 reject 且普通轨）issue_splitting
  → issue_confirm（HITL；条件边：eval_run / launch_plan / issue_splitting / prd_generation）
  → eval_run（第 8 段构建期跑评测，仅 AI 核心需求；条件边：launch_plan / eval_run 自环）
  → launch_plan → launch_confirm（HITL；条件边：artifact_persist / launch_plan / issue_splitting）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from components.registry import ComponentRegistry
from kernel.artifact import ArtifactManager
from kernel.runner import NodeRunner
from kb.store import KBStore

from nodes.artifact import make_artifact_persist
from nodes.exploration import make_intake, make_kb_lookup, make_needs_discovery
from nodes.hitl import make_requirement_confirm
from nodes.refine import make_requirement_refine  # [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点
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
from nodes.feasibility import (  # [C 2026-09-12 by codebuddy-ds41flash] 判断需求与 AI 的边界 + 确认AI可行性门
    make_feasibility_check,
    make_feasibility_confirm,
)
from nodes.eval_design import (  # [C 2026-09-12 by codebuddy-ds41flash] 设计评测体系 + 确认评测体系门
    make_eval_confirm,
    make_eval_design,
)
from nodes.eval_run import (  # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：构建期跑评测
    make_eval_run,
)
from nodes.bake_off import (  # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：对比选型模型
    make_bake_off,
)


@dataclass
class NodeDeps:
    """节点依赖容器：所有节点通过闭包访问这个依赖容器中的各个依赖。"""

    runner: NodeRunner
    registry: ComponentRegistry
    artifacts: ArtifactManager
    kb: KBStore
    rag: Any = None  # [C 2026-09-12 by codebuddy-ds41flash] R02：AI 领域知识库 RAGStore；None=不接领域库
    # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：外置评测工具配置（含 promptfoo_dir）；
    # None=未配置，eval_run 节点据此抛 NodeExecutionError 明确提示配置缺失
    eval_tool: dict | None = None
    # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：对比选型候选清单（config.yaml bake_off 段）；
    # None=未配置，bake_off 节点据此抛 NodeExecutionError 明确提示配置缺失
    bake_off_config: dict | None = None
    # [C 2026-09-14 by S043-b1] 工具能力清单（config.yaml tool_catalog 段）；
    # None=未配置，feasibility_check 节点降级为空列表（不阻断流程）
    tool_catalog: list[dict] | None = None


def build_nodes(deps: NodeDeps) -> dict:
    """构建 18 个节点函数的有序 dict（key 顺序与图执行顺序一致）。"""
    return {
        "kb_lookup": make_kb_lookup(deps),
        "intake": make_intake(deps),
        "requirement_confirm": make_requirement_confirm(deps),
        # [C 2026-09-14 by codebuddy-ds41flash] S041 需求修订整合节点：
        # 确认门 pending=True（feedback/改判带附言）时经此节点，模型整合"当前需求 + 修订意见"
        # 为完整新需求草案，HITL 确认/改判/放弃/带新意见重整合（前 2 版自动，第 3 版升级暂停）；
        # pending=False（confirm/纯改判）由 graph 条件边直接去 needs_discovery，不经本节点。
        "requirement_refine": make_requirement_refine(deps),
        # [C 2026-09-12 by codebuddy-ds41flash] 判断需求与 AI 的边界：AI 核心需求经此两节点，
        # 普通轨（ai_core=False）由 graph 条件边直接去 needs_discovery，不经此二节点
        "feasibility_check": make_feasibility_check(deps),
        "feasibility_confirm": make_feasibility_confirm(deps),
        "needs_discovery": make_needs_discovery(deps),
        "prd_generation": make_prd_generation(deps),
        # [C 2026-09-10] 评审门插在 PRD 生成之后，条件边由 graph.py 装配
        "prd_review": make_prd_review(deps),
        # [C 2026-09-12 by codebuddy-ds41flash] 第 5 段设计评测体系：
        # AI 核心需求在评审通过后、拆单前先经这两节点；普通轨由条件边直接去 issue_splitting
        "eval_design": make_eval_design(deps),
        # [C 2026-09-12 by codebuddy-ds41flash] 确认评测体系门（HITL，不调模型），两态条件边由 graph.py 装配
        "eval_confirm": make_eval_confirm(deps),
        # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段对比选型模型：
        # 评测体系确认后先横跑候选（AI 核心需求经此，普通轨由条件边直达 issue_splitting）
        "bake_off": make_bake_off(deps),
        # [C 2026-09-11] 块1 评审通过类去拆单；块2 拆单后先进工单确认门再落盘
        "issue_splitting": make_issue_splitting(deps),
        # [C 2026-09-11] 块2 工单确认门（HITL，不调模型），三分支条件边由 graph.py 装配
        "issue_confirm": make_issue_confirm(deps),
        # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段构建期跑评测（仅 AI 核心需求经过）：
        # 确认工单后、写发布计划前，subprocess 调 Promptfoo + 代码硬判达标；普通轨由条件边跳过
        "eval_run": make_eval_run(deps),
        # [C 2026-09-11] 块1 发布计划节点（工单确认门通过后产计划）
        "launch_plan": make_launch_plan(deps),
        # [C 2026-09-11] 块2 发布计划确认门（HITL，不调模型），三分支条件边由 graph.py 装配：
        # 确认落盘 launch_plan.md / 意见回 launch_plan 重调 / 回工单回 issue_splitting 重拆
        "launch_confirm": make_launch_confirm(deps),
        "artifact_persist": make_artifact_persist(deps),
    }


# [C 2026-09-09] nodes 包汇总完成
