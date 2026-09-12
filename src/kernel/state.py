# [C 2026-09-08] M1 内核骨架 - State 定义
"""LangGraph 全局状态定义，字段与 workflow-design.md 第二节完全一致。"""
from __future__ import annotations

import uuid
from typing import Any, TypedDict


class PMState(TypedDict, total=False):
    """AI PM Agent 全流程状态。

    字段对齐 docs/workflow-design.md 第二节 State Schema。
    使用 total=False 以便分步构建状态；各节点只写自己负责的字段。
    """

    # ─── 标识 ───
    initiative_id: str                # 全流程唯一标识（兼作 SQLite thread_id）
    requirement_name: str             # 需求名（文件夹名）
    current_stage: str                # 当前阶段名（探索/PRD/评估/AI专项）

    # ─── 知识库底座（G4 横切）───
    kb_context: dict                  # G4: 开工前查家底的检索结果
    kb_written_back: list             # G4: 已写回知识库的档案 ID 列表

    # ─── 探索阶段 ───
    raw_requirement: str              # 用户原始需求描述
    info_completeness: dict           # 6 维度信息完整度评估
    confirmed_requirement: str        # 用户确认后的需求
    user_insights: dict               # 从用户脑中挖出的信息
    external_evidence: dict           # 外部验证证据
    opportunity_score: dict           # 机会评分（ODI/RICE）+ cost_feasibility
    competitor_teardown: dict         # 竞品拆解结果
    ai_feasibility: dict              # G5: 必须AI做/传统就能做/AI更差
    capability_boundary: dict         # G8: 自动/工具/人工 三色表（[C 2026-09-12 by MA] S033 块2a：
                                       #   确认门不再调用此组件，仅保留字段供旧检查点兼容；
                                       #   块 3 可行性门将重新设计为语义不同的产品能力三色表）
    # [C 2026-09-12 by MA] S033 块2a：AI 适用性分流判定字段（v3.0 第 1 段分流）
    ai_triage: dict                   # 模型给出的分流建议 {suggestion, reason, signals}
    ai_core: bool | None              # 用户拍板的最终分流：True=AI 全轨 / False=普通轨 / None=未判定
    component_candidates: list        # G8+G9: 组件候选 + 开源检查
    proceed_decision: bool | None     # 是否继续做（用户决策）

    # ─── 评测集（G2）───
    eval_cases: list                  # G2: 需求确认门后建立的评测用例

    # ─── PRD 阶段 ───
    section_plan: dict                # 章节裁剪计划
    section_confirmed: bool           # 章节裁剪是否经用户确认
    prd_markdown: str                 # 模型原生 Markdown PRD 全文 [C 2026-09-09] T1
    # [C 2026-09-09] T1 以下两个字段退役：旧 JSON sections / HTML 套壳通道不再写入，
    # 保留字段定义仅供旧 SQLite 检查点/历史 state 兼容，新流程一律用 prd_markdown
    prd_html: str                     # [T1 退役] 旧 PRD HTML 内容，保留供旧检查点兼容，不再写入
    prd_sections: list                # [T1 退役] 旧 PRD 章节片段列表，保留供旧检查点兼容，不再写入
    red_team_review: dict             # 红队审查反馈
    prd_revision_count: int           # PRD 修订次数（上限 3 轮）

    # ─── 工单拆解（issue_splitting + issue_confirm 确认门）───
    # [C 2026-09-11] 块1：评审通过后拆研发工单；块2 在 issue_splitting 后插人工确认门
    issue_plan: dict                  # 研发工单拆解方案（含 shape_errors/warnings/self_fixed）
    issue_revision_count: int         # 工单方案修订次数（确认门前 2 轮自动重拆，之后升级暂停）
    issue_revision_feedback: str      # 工单方案上轮修改意见（注入拆单 prompt，消费即清零）
    # [C 2026-09-11] 块2：确认门"回PRD"回炉专用
    issue_prd_redo_count: int         # 回炉重写 PRD 次数（硬上限 1 次）
    prd_rewrite_feedback: str         # 工单阶段发起的 PRD 回炉意见（注入 prd_generation，消费即清零）
    # [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级暂停后再给意见的硬深度上限计数
    issue_escalation_depth: int       # 已批准的升级后重拆轮数（达上限后保持 escalated 暂停不自动空转）

    # ─── 发布计划（launch_plan 节点；块2 会插 launch_confirm HITL 门）───
    # [C 2026-09-11] 块1：工单确认门通过后产发布计划；关键字段非空由 judge 硬判
    launch_plan: dict                  # 发布计划 JSON（含 shape_errors/warnings/self_fixed）
    launch_plan_errors: list           # 发布计划字段自检 errors（随产物醒目展示）
    launch_plan_warnings: list         # 发布计划字段自检 warnings
    # [C 2026-09-11] 块2 确认门预留字段（块1 恒空/恒 0，不写回全局 state）
    launch_revision_count: int         # 发布计划重调计数（块2 用）
    launch_revision_feedback: str      # 发布计划上轮修改意见（块2 注入，消费即清零）
    launch_issue_redo_count: int       # 发布计划回工单计数（块2 用）
    # [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级暂停后再给意见的硬深度上限计数
    launch_escalation_depth: int       # 已批准的升级后重调轮数（达上限后保持 escalated 暂停不自动空转）

    # ─── 评估阶段 ───
    metrics_tree: dict                # 指标树
    experiment_design: dict           # 实验设计
    ai_eval_design: dict              # AI 评估设计

    # ─── AI 专项 ───
    model_selection: dict             # G1: 模型选型循环化
    failure_modes: dict               # 失败模式分析 + 缓解方案

    # ─── 产物管理 ───
    artifacts: dict                   # 产物文件路径映射
    human_feedback: list              # 所有人机交互记录

    # ─── 交付后接口（G3 二期留接口）───
    post_launch_hooks: dict | None    # G3: bad case 回收 / 回归重跑 接口占位


def default_state() -> dict[str, Any]:
    """返回全字段默认值的状态字典。

    - initiative_id 用 uuid4() 生成
    - dict 类默认为 {}，list 类默认为 []，int 默认为 0
    - bool 默认为 False，可选字段（bool|None / dict|None）默认为 None
    - str 默认为 ""
    """
    return {
        # 标识
        "initiative_id": str(uuid.uuid4()),
        "requirement_name": "",
        "current_stage": "",
        # 知识库底座
        "kb_context": {},
        "kb_written_back": [],
        # 探索阶段
        "raw_requirement": "",
        "info_completeness": {},
        "confirmed_requirement": "",
        "user_insights": {},
        "external_evidence": {},
        "opportunity_score": {},
        "competitor_teardown": {},
        "ai_feasibility": {},
        "capability_boundary": {},
        # [C 2026-09-12 by MA] S033 块2a：AI 分流字段（默认 None=未判定，prd_generation 视为普通轨）
        "ai_triage": {},
        "ai_core": None,
        "component_candidates": [],
        "proceed_decision": None,
        # 评测集
        "eval_cases": [],
        # PRD 阶段
        "section_plan": {},
        "section_confirmed": False,
        "prd_markdown": "",  # [C 2026-09-09] T1 模型原生 Markdown PRD 全文
        "prd_html": "",       # [T1 退役] 保留默认值供旧检查点兼容，不再写入
        "prd_sections": [],   # [T1 退役] 保留默认值供旧检查点兼容，不再写入
        "red_team_review": {},
        "prd_revision_count": 0,
        # 工单拆解 [C 2026-09-11]
        "issue_plan": {},
        "issue_revision_count": 0,
        "issue_revision_feedback": "",
        # [C 2026-09-11] 块2 确认门回炉字段
        "issue_prd_redo_count": 0,
        "prd_rewrite_feedback": "",
        # [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级深度上限计数
        "issue_escalation_depth": 0,
        # 发布计划 [C 2026-09-11]
        "launch_plan": {},
        "launch_plan_errors": [],
        "launch_plan_warnings": [],
        # [C 2026-09-11] 块2 确认门预留字段
        "launch_revision_count": 0,
        "launch_revision_feedback": "",
        "launch_issue_redo_count": 0,
        # [C 2026-09-12 by pi-deepseek-flash] 第⑥项修复：升级深度上限计数
        "launch_escalation_depth": 0,
        # 评估阶段
        "metrics_tree": {},
        "experiment_design": {},
        "ai_eval_design": {},
        # AI 专项
        "model_selection": {},
        "failure_modes": {},
        # 产物管理
        "artifacts": {},
        "human_feedback": [],
        # 交付后接口
        "post_launch_hooks": None,
    }


# [C 2026-09-08] state.py 实现完成
