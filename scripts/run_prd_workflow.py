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
    runner = NodeRunner(llm=llm)
    deps = NodeDeps(runner=runner, registry=registry, artifacts=artifacts_mgr, kb=kb)

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
    payload_lines = _format_materials(PAYLOAD_RECAP_FIELDS, payload)
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

    节点特化：requirement_confirm 是当前唯一的 HITL 中断点，给出更贴切的提示；
    其它节点用通用模板。详细材料见 STATUS 块的 --- 段。
    """
    if node_name == "requirement_confirm":
        return (
            "节点「requirement_confirm」进入需求确认门。"
            "请审阅下方需求理解与能力边界三色表，确认无误后回复 confirmed，"
            "或回复修改意见。"
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
