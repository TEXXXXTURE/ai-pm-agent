# run_prd_workflow.py — PRD 流水线非交互包装器
# 让 Pi 通过 bash 调用项目 PRD 流程，在 HITL 中断点输出问题退出，
# Pi 拿到用户答案后用 --resume 恢复执行。
#
# USAGE
# =====
# 模式 1：新建需求（跑到首个 HITL 中断或完成，输出 STATUS 块后退出 0）
#   bash scripts/run-tool.sh scripts/run_prd_workflow.py \
#       --requirement "我想做一个帮产品经理整理会议纪要的工具" \
#       --name "test-prd"
#   可选：--config <路径>  指定 config.yaml（默认项目根 config.yaml）
#         --stage <阶段>    指定起始阶段（可选，记录到 state.current_stage）
#
# 模式 2：断点恢复（用上一步返回的 THREAD_ID + 用户答案恢复执行）
#   bash scripts/run-tool.sh scripts/run_prd_workflow.py \
#       --resume <thread_id> --answer "confirmed"
#   --answer 缺省时按 "confirmed" 处理。
#
# 输出（stdout，Pi 可解析的纯文本块；argparse 参数错误走 stderr 退出 2）
#   STATUS: HITL / DONE / ERROR 三种块，详见 _emit_hitl / _emit_done / _emit_error。
#
# 退出码：0 = 正常（HITL 中断或完成）；1 = 运行错误；2 = argparse 参数错误。
#
# [C 2026-09-10] T4-5 PRD 流水线非交互包装器
"""PRD 流水线非交互包装器：HITL 中断输出问题退出，Pi 恢复。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from langgraph.types import Command

from components.registry import ComponentRegistry
from kernel.artifact import ArtifactManager
from kernel.config import PROJECT_ROOT, load_config
from kernel.graph import build_graph
from kernel.model import build_llm
from kernel.runner import NodeRunner
from kernel.state import default_state
from kb.store import KBStore
from kb.rag import build_rag_store_if_available  # [C 2026-09-12 by codebuddy-ds41flash] R02
from nodes import NodeDeps

# 复用 cli.hitl_cli 的中断收集逻辑与决策材料字段定义（不调用 handle_hitl，
# 它 console.input 会卡住非交互进程；只复用 collect_interrupts 与字段常量）
from cli.hitl_cli import (
    DECISION_MATERIAL_FIELDS,
    PAYLOAD_RECAP_FIELDS,
    collect_interrupts,
)


def _resolve_path(path: str) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。

    仿写自 cli.main._resolve_path（私有函数，import 不优雅；此处保持脚本自包含）。
    """
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


# ────────────────────────── CLI 参数 ──────────────────────────


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。argparse 错误走 stderr 退出 2（不走 STATUS 块格式）。"""
    parser = argparse.ArgumentParser(
        prog="run_prd_workflow.py",
        description="PRD 流水线非交互包装器（HITL 中断输出问题退出，Pi 恢复）",
    )
    parser.add_argument(
        "--requirement",
        type=str,
        help="用户原始需求描述（与 --resume 互斥）",
    )
    parser.add_argument(
        "--name",
        type=str,
        help="需求名（新建时必填，用作产物文件夹名）",
    )
    parser.add_argument(
        "--resume",
        type=str,
        metavar="THREAD_ID",
        help="断点恢复：传入 thread_id（与 --requirement 互斥）",
    )
    parser.add_argument(
        "--answer",
        type=str,
        default=None,
        help="恢复时的用户答案（仅 --resume 模式有效；缺省为 confirmed）",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=None,
        help="config.yaml 路径（默认项目根 config.yaml）",
    )
    parser.add_argument(
        "--stage",
        type=str,
        default=None,
        help="指定起始阶段（可选，记录到 state.current_stage）",
    )

    args = parser.parse_args(argv)

    # 互斥校验：--requirement 与 --resume 二选一
    if args.resume and args.requirement:
        parser.error("--resume 与 --requirement 互斥，只能指定其一")
    if not args.resume and not args.requirement:
        parser.error("必须指定 --requirement（新需求）或 --resume（断点恢复）")
    if not args.resume and not args.name:
        parser.error("新建需求时必须指定 --name")

    return args


# ────────────────────────── 依赖初始化 ──────────────────────────


def _check_api_key(cfg: dict[str, Any]) -> tuple[bool, str]:
    """检查配置的 LLM provider 密钥环境变量是否存在。

    镜像 kernel.config.get_llm_config 的密钥读取逻辑，但在 LLM 初始化前提前校验，
    缺失时给出干净的 STATUS: ERROR，而不是让 DeepSeek 调用在 stream 阶段才炸。
    本地模型（api_key_env 为空）直接放行。
    """
    llm_section = cfg.get("llm", {})
    provider_name = llm_section.get("default_provider", "deepseek")
    providers = llm_section.get("providers", {})
    p = providers.get(provider_name, {})
    api_key_env = p.get("api_key_env", "") or ""
    if not api_key_env:
        # 本地模型（如 ollama）无密钥，放行
        return True, ""
    if not os.environ.get(api_key_env):
        return (
            False,
            f"环境变量 {api_key_env} 未设置，无法调用 LLM。"
            f"请通过 .env 或环境变量注入密钥后再调用。",
        )
    return True, ""


def _build_graph_and_config(cfg: dict[str, Any]) -> tuple[Any, dict]:
    """初始化真实依赖并构建 graph（复用 cli.main.main 的依赖装配逻辑）。

    不修改 src/ 下任何文件；此处仅按相同顺序装配依赖并返回 graph + 空 config。
    """
    llm = build_llm(cfg)

    kb_store = _resolve_path(cfg["kb"]["store_path"])
    output_root = _resolve_path(cfg["artifacts"]["output_root"])
    template_dir = _resolve_path(cfg["artifacts"]["template_dir"])
    assets_dir = PROJECT_ROOT / "artifacts" / "assets"
    components_dir = PROJECT_ROOT / "src" / "components"
    db_path = _resolve_path(cfg["persistence"]["db_path"])

    registry = ComponentRegistry(str(components_dir))
    artifacts_mgr = ArtifactManager(
        str(output_root), str(template_dir), str(assets_dir)
    )
    kb = KBStore(str(kb_store))
    rag_store = build_rag_store_if_available(cfg)  # [C 2026-09-12 by codebuddy-ds41flash] R02：库不存在/为空返回 None
    # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段：装配外置评测工具配置（eval_tools 段）
    # promptfoo_dir 用 _resolve_path 解析相对路径；缺失则 eval_tool 无 promptfoo_dir，
    # eval_run 节点会抛 NodeExecutionError 明确提示配置缺失（不静默失败）。
    eval_tools_cfg = cfg.get("eval_tools", {}) or {}
    eval_tool: dict = {}
    if eval_tools_cfg.get("promptfoo_dir"):
        eval_tool["promptfoo_dir"] = str(_resolve_path(eval_tools_cfg["promptfoo_dir"]))
    # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段：装配对比选型候选清单（bake_off 段），
    # 直接传 dict（仿 cli.main）；缺失则 bake_off 节点抛 NodeExecutionError 明确提示配置缺失。
    bake_off_cfg = cfg.get("bake_off", {}) or {}
    # [C 2026-09-14 by S043-b1] 工具能力清单（tool_catalog 段）
    tool_catalog_cfg = cfg.get("tool_catalog", {}) or {}
    tool_catalog: list[dict] = []
    if tool_catalog_cfg.get("routing_md_path"):
        from components.tools.tool_catalog import load_tool_catalog
        tool_catalog = load_tool_catalog(
            str(_resolve_path(tool_catalog_cfg["routing_md_path"]))
        )
    # [C 2026-09-16 by codebuddy-deepseek-v4.1-flash] S048 候选池料件（model_catalog 段）：
    # 两处路径都解析为绝对路径（样式参照 tool_catalog）；缺失则 field 为 None，
    # feasibility_check 节点降级为空候选池并记原因（不报错）。
    model_catalog_cfg = cfg.get("model_catalog", {}) or {}
    model_catalog: dict = {}
    if model_catalog_cfg.get("path"):
        model_catalog["path"] = str(_resolve_path(model_catalog_cfg["path"]))
    if model_catalog_cfg.get("price_script"):
        model_catalog["price_script"] = str(
            _resolve_path(model_catalog_cfg["price_script"])
        )
    runner = NodeRunner(llm=llm)
    deps = NodeDeps(
        runner=runner,
        registry=registry,
        artifacts=artifacts_mgr,
        kb=kb,
        rag=rag_store,
        eval_tool=eval_tool or None,
        bake_off_config=bake_off_cfg or None,
        tool_catalog=tool_catalog or None,
        model_catalog=model_catalog or None,
    )

    graph = build_graph(deps, db_path=str(db_path))
    config: dict = {"configurable": {}}
    return graph, config
    # [C 2026-09-10] T4-5 依赖装配复用 cli.main.main 的顺序


# ────────────────────────── STATUS 块输出 ──────────────────────────


def _emit_hitl(
    graph: Any, config: dict, thread_id: str, interrupt_value: Any
) -> None:
    """输出 STATUS: HITL 块。

    interrupt_value 来自 collect_interrupts（已 _extract_interrupt_value 提取）：
    dict 载荷（M6 起）含 node / requirement_name / raw_requirement /
    info_completeness / capability_boundary；字符串载荷（旧行为）为节点名。
    """
    if isinstance(interrupt_value, dict):
        node_name = str(interrupt_value.get("node", "hitl"))
        payload: dict = interrupt_value
    else:
        node_name = str(interrupt_value) if interrupt_value is not None else "unknown"
        payload = {}

    # 取当前 state 决策材料（节点尚未返回时 capability_boundary 等只在 payload 里，
    # 但 raw_requirement / requirement_name 等已在 state 中，state 也作为补充来源）
    state = graph.get_state(config).values or {}

    question = _build_question(node_name, payload, state)

    materials_lines: list[str] = []
    # [C 2026-09-11] 升级暂停三字段（status/reason/prior_feedbacks）仅对
    # issue_confirm / launch_confirm 载荷追加：先从共用元组中剔除，再按节点显式补回，
    # 保证 ① STATUS 块不重复打印；② 绝不污染 requirement_confirm 等其他节点
    # （即便其载荷碰巧出现同名字段）。list/dict 由 _format_materials 走 JSON。
    escalation_fields = ("status", "reason", "prior_feedbacks")
    common_recap_fields = tuple(
        f for f in PAYLOAD_RECAP_FIELDS if f not in escalation_fields
    )
    payload_lines = _format_materials(common_recap_fields, payload)
    # [C 2026-09-12 by codebuddy-ds41flash] feasibility_confirm 升级暂停（重塑额度用尽）也
    # 携带 status/reason/prior_feedbacks，一并透传；eval_confirm 升级暂停同此模式
    if node_name in (
        "issue_confirm",
        "launch_confirm",
        "feasibility_confirm",
        "eval_confirm",
        # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段评测执行门：透传 status/reason
        #（await_prompt/tool_error/eval_failed 三态都带 status + reason；与 issue_confirm 等同处理）
        "eval_run",
        # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段对比选型门：透传 status/reason
        #（await_decision/await_prompt/tool_error 三态都带 status + reason）
        "bake_off",
    ) and isinstance(interrupt_value, dict):
        payload_lines.extend(_format_materials(escalation_fields, payload))
    if payload_lines:
        materials_lines.append("[中断载荷]")
        materials_lines.extend(payload_lines)
    state_lines = _format_materials(DECISION_MATERIAL_FIELDS, state)
    if state_lines:
        materials_lines.append("[当前 state 决策材料]")
        materials_lines.extend(state_lines)

    print("STATUS: HITL")
    print(f"THREAD_ID: {thread_id}")
    print(f"NODE: {node_name}")
    print(f"QUESTION: {question}")
    print("---")
    for line in materials_lines:
        print(line)
    print("---")
    print("END HITL")


def _emit_done(thread_id: str, final_state: dict) -> None:
    """输出 STATUS: DONE 块。产物路径从 state['artifacts'] 提取（仿 output.py）。"""
    artifacts: dict = final_state.get("artifacts", {}) or {}
    print("STATUS: DONE")
    print(f"THREAD_ID: {thread_id}")
    print("ARTIFACTS:")
    for _name, path in artifacts.items():
        print(f"  - {path}")
    print("END DONE")


def _emit_error(thread_id: str, message: str) -> None:
    """输出 STATUS: ERROR 块（stdout，退出码 1）。"""
    print("STATUS: ERROR")
    print(f"THREAD_ID: {thread_id}")
    print(f"MESSAGE: {message}")
    print("END ERROR")


# ────────────────────────── 材料格式化 ──────────────────────────


def _build_question(node_name: str, payload: dict, state: dict) -> str:
    """把中断材料整理成 Pi 能读懂并转述给用户的一句话摘要。

    节点特化：requirement_confirm（需求确认门）/ issue_confirm（工单确认门）
    各给贴切的三选一提示；其它节点用通用模板。详细材料见 STATUS 块的 --- 段。
    """
    if node_name == "requirement_confirm":
        return (
            "节点「requirement_confirm」进入需求确认门。"
            "请审阅下方需求理解与 AI 适用性分流建议，确认无误后回复 confirmed，"
            "或回复「非AI」改判普通轨、「AI核心」改判 AI 全轨；"
            "其他文本作为需求修订意见处理（分流沿用模型建议，不二次中断）。"
        )
    if node_name == "feasibility_confirm":
        # [C 2026-09-12 by codebuddy-ds41flash] 确认AI可行性门：提示四态选项
        if payload.get("status") == "escalated":
            # 重塑额度用尽后的升级暂停：明确已停、由人重新拍板三选一
            return (
                "节点「feasibility_confirm」确认AI可行性门：流水线已升级暂停"
                "（重塑额度已用尽，全程限 1 次），暂停原因见下方中断材料的 reason 字段，"
                "历轮意见见 prior_feedbacks。请重新拍板：回复「通过」进 PRD；"
                "回复「改判普通」转普通轨（ai_core=False，按普通 PRD 模板）；"
                "回复「放弃」结束流程。"
            )
        return (
            "节点「feasibility_confirm」确认AI可行性门。请审阅可行性报告，"
            "人工执行探针方案后录入实测结论，四选一："
            "回复「通过」进 ai-native PRD；回复「改判普通」转普通轨（探针证实传统方案即可）；"
            "回复「重塑」回需求确认门调整范围后重过判定（限 1 次）；"
            "回复「放弃」结束流程。自由文本作为补充意见，默认按通过处理。"
        )
    if node_name == "eval_confirm":
        # [C 2026-09-12 by codebuddy-ds41flash] 第 5 段确认评测体系门：提示两选一选项
        if payload.get("status") == "escalated":
            # 第 3 版仍提意见后的升级暂停：明确已停、由人重新拍板
            return (
                "节点「eval_confirm」确认评测体系门：流水线已升级暂停"
                "（已看完第 3 版评测体系，或升级后重起草额度已用尽），"
                "暂停原因见下方中断材料的 reason 字段，历轮意见见 prior_feedbacks。"
                "请二选一：① 回复「确认」按当前版落盘（写 Promptfoo YAML 草案与评测档案）；"
                "② 让 Pi 协助调查后，回复一条带来新决策的具体意见，"
                "由你主动发起再起草一轮（是否还能重起草以 reason 的说明为准）。"
            )
        # draft：四层考题（典型/边界/对抗/线上回放）+ 及格线建议值，确认或提修改意见
        return (
            "节点「eval_confirm」确认评测体系门。请审阅评测体系草案"
            "（四层考题：典型题/边界题/对抗题/线上回放题 + 每题评分方式 + 及格线建议值）："
            "回复「确认」落盘（写 Promptfoo YAML 草案与评测档案进 state）；"
            "其他文本一律作为修改意见打回重新起草（最多2轮，之后进入升级暂停，"
            "可让 Pi 协助调查后带新决策再起草）。"
        )
    if node_name == "eval_run":
        # [C 2026-09-12 by codebuddy-ds41flash] 第 8 段评测执行门：按 status 三态给中文提示
        status = payload.get("status")
        if status == "await_prompt":
            return (
                "节点「eval_run」构建期跑评测：缺少被测 prompt 文件，流水线暂停等待。"
                "请把被测 system_prompt.txt 放到下方中断材料的 prompt_path 指定路径，"
                "放好后回复任意内容恢复，流水线将重新检查并继续执行评测。"
            )
        if status == "tool_error":
            return (
                "节点「eval_run」构建期跑评测：Promptfoo 工具执行失败（配置/环境/网络错误，"
                "不代表模型质量结论），失败原因见下方中断材料的 reason 字段尾部。"
                "请修复环境或评测配置后回复任意内容重跑评测。"
            )
        # eval_failed：工具跑通但未达及格线
        return (
            "节点「eval_run」构建期跑评测：评测结果未达及格线，流水线暂停（不设自动放行）。"
            "下方中断材料的 eval_report / report 给出整体通过率、关键题通过率、与阈值的差距"
            "及未通过的关键题。请工程师线下修复被测 prompt、考题或模型方案后"
            "回复任意内容重跑评测；达标前不会进入发布计划。"
        )
    if node_name == "bake_off":
        # [C 2026-09-13 by codebuddy-ds41flash] 第 6 段对比选型门：按 status 三态给中文提示
        status = payload.get("status")
        if status == "await_prompt":
            return (
                "节点「bake_off」对比选型模型：缺少被测 prompt 文件，流水线暂停等待。"
                "请把被测 system_prompt.txt 放到下方中断材料的 prompt_path 指定路径，"
                "放好后回复任意内容恢复，流水线将重新检查并继续横跑候选模型。"
            )
        if status == "tool_error":
            return (
                "节点「bake_off」对比选型模型：Promptfoo 工具执行失败或配置生成失败"
                "（配置/环境/网络错误，不代表模型质量结论），失败原因与出错的候选见下方"
                "中断材料的 provider_id / reason。请修复环境或评测配置后回复任意内容重跑本节点。"
            )
        # await_decision：首次确认是否横跑（横跑会真实产生多次模型调用）
        return (
            "节点「bake_off」对比选型模型（第 6 段，条件触发）：本版暂无历史选型记录。"
            "候选模型清单见下方中断材料的 candidates（id + label）。"
            "注意：横跑会真实产生多次模型调用（按候选数逐个跑同一批考题，产生真实成本与延迟）。"
            "请三选一：① 回复「跑」/「确认」/「横跑」开始对比选型；"
            "② 回复「跳过」/「先用默认」跳过横跑（按默认 DeepSeek 记推荐，不调任何模型）；"
            "③ 提出其他具体意见，流水线不猜、继续暂停等你明确答复。"
        )
    if node_name == "issue_confirm":
        if payload.get("status") == "escalated":
            # [C 2026-09-11] 升级暂停特化文案：明确流水线已停、原因见 reason；
            # 三选一中"回PRD"是否仍可回炉以 reason 说明为准，额度用尽时不承诺可回炉
            return (
                "节点「issue_confirm」工单确认门：流水线已升级暂停"
                "（已看完第 3 版工单，或 PRD 回炉额度已用尽），"
                "暂停原因见下方中断材料的 reason 字段，历轮意见见 prior_feedbacks。"
                "请三选一：① 回复「确认」按当前版落盘；"
                "② 让 Pi 协助调查后，回复一条带来新决策的具体意见，再主动拆一轮；"
                "③ 回复「回PRD」——是否还能回炉以 reason 的说明为准"
                "（回炉额度用尽时不再承诺可以回炉重写 PRD）。"
            )
        # [C 2026-09-11] 块2 工单确认门 draft 原文案（status 缺省也按 draft 处理）
        return (
            "节点「issue_confirm」工单确认门。请审阅工单清单草案："
            "回复「确认」落盘；回复「回PRD」回炉重写 PRD（限1次）；"
            "其他文本作为修改意见打回重拆（最多2轮，之后进入升级暂停，"
            "可让 Pi 协助调查后带新决策再拆）。"
        )
    if node_name == "launch_confirm":
        if payload.get("status") == "escalated":
            # [C 2026-09-11] 升级暂停特化文案：明确流水线已停、原因见 reason；
            # 三选一中"回工单"是否仍可回工单以 reason 说明为准，额度用尽时不承诺可回工单
            return (
                "节点「launch_confirm」发布计划确认门：流水线已升级暂停"
                "（已看完第 3 版发布计划，或回工单额度已用尽），"
                "暂停原因见下方中断材料的 reason 字段，历轮意见见 prior_feedbacks。"
                "请三选一：① 回复「确认」按当前版落盘；"
                "② 让 Pi 协助调查后，回复一条带来新决策的具体意见，再主动调一轮；"
                "③ 回复「回工单」——是否还能回工单以 reason 的说明为准"
                "（回工单额度用尽时不再承诺可以回 issue_splitting 重拆）。"
            )
        # [C 2026-09-11] 块2 发布计划确认门 draft 原文案（status 缺省也按 draft 处理）
        return (
            "节点「launch_confirm」发布计划确认门。请审阅发布计划草案："
            "回复「确认」落盘 launch_plan.md；回复「回工单」回 issue_splitting 重拆（限1次）；"
            "其他文本作为修改意见打回重调（最多2轮，之后进入升级暂停，"
            "可让 Pi 协助调查后带新决策再调）。"
        )
    return (
        f"节点「{node_name}」请求人确认。"
        "请审阅下方决策材料，回复 confirmed 确认，或回复修改意见。"
    )


def _format_materials(fields: tuple[str, ...], source: dict) -> list[str]:
    """把指定字段从 source 中提取为纯文本行（空值跳过）。

    标量: ``key: value``；dict/list: ``key:`` 换行后接多行 JSON。
    """
    lines: list[str] = []
    if not isinstance(source, dict):
        return lines
    for field in fields:
        value = source.get(field)
        # 跳过空值/默认值，只展示有意义的材料（与 hitl_cli 一致）
        if value in (None, "", {}, []):
            continue
        if isinstance(value, (dict, list)):
            try:
                j = json.dumps(value, ensure_ascii=False, indent=2)
                lines.append(f"{field}:")
                lines.extend(j.split("\n"))
            except Exception:
                lines.append(f"{field}: {value}")
        else:
            lines.append(f"{field}: {value}")
    return lines


# ────────────────────────── 执行流程 ──────────────────────────


def _run_fresh(
    graph: Any,
    config: dict,
    thread_id: str,
    requirement: str,
    name: str,
    stage: str | None,
) -> int:
    """模式 1：新建需求。构造 state -> stream -> 检查中断 -> HITL/DONE。"""
    state = default_state()
    state["raw_requirement"] = requirement
    state["requirement_name"] = name
    state["initiative_id"] = thread_id
    if stage:
        state["current_stage"] = stage

    # 流式执行（非交互，静默丢弃 chunk，不打印节点名进度）
    for _chunk in graph.stream(state, config, stream_mode="updates"):
        pass

    interrupts = collect_interrupts(graph, config)
    if interrupts:
        _emit_hitl(graph, config, thread_id, interrupts[0])
        return 0

    final_state = graph.get_state(config).values or {}
    _emit_done(thread_id, final_state)
    return 0


def _run_resume(
    graph: Any, config: dict, thread_id: str, answer: str | None
) -> int:
    """模式 2：断点恢复。检查待处理中断 -> Command(resume) -> 检查中断 -> HITL/DONE。

    若 thread_id 不存在或无待处理中断：检查 graph 状态，已结束 -> DONE，否则 ERROR。
    """
    answer_text = answer if answer else "confirmed"

    # 先检查是否有待处理的中断
    interrupts = collect_interrupts(graph, config)
    if not interrupts:
        # 无待处理中断：检查 graph 状态判断是"已结束"还是"thread 不存在/卡住"
        snap = graph.get_state(config)
        values = snap.values or {}
        next_nodes = list(snap.next or [])
        if values and not next_nodes:
            # thread 存在且已到达 END：幂等输出 DONE
            _emit_done(thread_id, values)
            return 0
        # thread 不存在（values 空）或卡在无中断的中间态（next 非空）
        _emit_error(
            thread_id,
            f"thread_id {thread_id} 无待处理中断"
            + ("（thread_id 不存在）" if not values else "（状态异常，无法恢复）"),
        )
        return 1

    # 有待处理中断：用 Command(resume=answer) 恢复执行
    for _chunk in graph.stream(
        Command(resume=answer_text), config, stream_mode="updates"
    ):
        pass

    interrupts = collect_interrupts(graph, config)
    if interrupts:
        _emit_hitl(graph, config, thread_id, interrupts[0])
        return 0

    final_state = graph.get_state(config).values or {}
    _emit_done(thread_id, final_state)
    return 0
    # [C 2026-09-10] T4-5 恢复流程：collect_interrupts -> Command(resume) -> 再检查


# ────────────────────────── 主入口 ──────────────────────────


def main(argv: list[str] | None = None) -> int:
    """主流程：分阶段捕获异常，统一输出 STATUS 块。KeyboardInterrupt 不捕获。"""
    args = parse_args(argv)  # argparse 错误走 stderr 退出 2

    # 阶段 1：配置 / 密钥 / 依赖 / graph（thread_id 尚未持久化，错误报 args.resume 或 "-"）
    pre_tid = args.resume if args.resume else "-"

    try:
        cfg = load_config(args.config)
    except Exception as exc:
        _emit_error(pre_tid, f"配置加载失败: {exc}")
        return 1

    try:
        ok, msg = _check_api_key(cfg)
        if not ok:
            _emit_error(pre_tid, msg)
            return 1
    except Exception as exc:
        _emit_error(pre_tid, f"LLM 密钥检查失败: {exc}")
        return 1

    try:
        graph, config = _build_graph_and_config(cfg)
    except Exception as exc:
        _emit_error(pre_tid, f"Graph 构建失败: {exc}")
        return 1

    # 阶段 2：执行（thread_id 已确定并写入 config，错误时报真实 thread_id）
    if args.resume:
        thread_id = args.resume
    else:
        thread_id = str(uuid.uuid4())
    config["configurable"]["thread_id"] = thread_id

    try:
        if args.resume:
            return _run_resume(graph, config, thread_id, args.answer)
        return _run_fresh(
            graph, config, thread_id, args.requirement, args.name, args.stage
        )
    except Exception as exc:
        _emit_error(thread_id, f"执行失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())


# [C 2026-09-10] run_prd_workflow.py 实现完成
