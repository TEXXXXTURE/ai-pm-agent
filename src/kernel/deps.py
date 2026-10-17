# 内核骨架 - 依赖装配
"""把 config.yaml + .env 装配成 NodeDeps，供 build_graph 消费。

历史上 cli.main.main 与 scripts/run_prd_workflow._build_graph_and_config 各有
一份内联装配（后者注释自陈「仿写自 cli.main」）。本模块是第三处调用方
（langgraph dev / Studio 的图工厂）的落点，取两份的超集（含 tool_catalog）。
两处旧装配收敛到本函数是独立的清理项，不在本次范围内——它们行为不变。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from components.registry import ComponentRegistry
from components.tools.tool_catalog import load_tool_catalog
from kb.rag import build_rag_store_if_available
from kb.store import KBStore
from kernel.artifact import ArtifactManager
from kernel.config import PROJECT_ROOT, load_config
from kernel.model import build_llm
from kernel.runner import NodeRunner
from nodes import NodeDeps


def resolve_path(path: str) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def build_deps(cfg: dict[str, Any] | None = None) -> tuple[NodeDeps, Path]:
    """装配节点依赖容器。

    Args:
        cfg: 已加载的配置；None 时自行 load_config()（顺带加载项目根 .env）。

    Returns:
        (deps, db_path)：db_path 是 config.yaml 里 persistence.db_path 解析后的
        绝对路径，只有自带 SqliteSaver 的编译路径会用到。
    """
    if cfg is None:
        cfg = load_config()

    llm = build_llm(cfg)

    kb_store = resolve_path(cfg["kb"]["store_path"])
    output_root = resolve_path(cfg["artifacts"]["output_root"])
    template_dir = resolve_path(cfg["artifacts"]["template_dir"])
    assets_dir = PROJECT_ROOT / "artifacts" / "assets"
    components_dir = PROJECT_ROOT / "src" / "components"
    db_path = resolve_path(cfg["persistence"]["db_path"])

    registry = ComponentRegistry(str(components_dir))
    artifacts_mgr = ArtifactManager(
        str(output_root), str(template_dir), str(assets_dir)
    )
    kb = KBStore(str(kb_store))
    rag = build_rag_store_if_available(cfg)  # 库不存在/为空返回 None

    # 第 8 段：外置评测工具配置。缺失时不设 eval_tool，
    # eval_run 节点抛 NodeExecutionError 明确提示配置缺失（不静默失败）。
    eval_tools_cfg = cfg.get("eval_tools", {}) or {}
    eval_tool: dict = {}
    if eval_tools_cfg.get("promptfoo_dir"):
        eval_tool["promptfoo_dir"] = str(resolve_path(eval_tools_cfg["promptfoo_dir"]))

    # 第 6 段：对比选型候选清单。缺失时 bake_off 节点同样抛错提示。
    bake_off_cfg = cfg.get("bake_off", {}) or {}

    # 工具能力清单。缺失时 tool_catalog 为 None。
    tool_catalog_cfg = cfg.get("tool_catalog", {}) or {}
    tool_catalog: list[dict] = []
    if tool_catalog_cfg.get("routing_md_path"):
        tool_catalog = load_tool_catalog(
            str(resolve_path(tool_catalog_cfg["routing_md_path"]))
        )

    # 候选池料件。两条路径都解析为绝对路径；缺失时为 None，
    # feasibility_check 节点降级为空候选池并记原因（不报错）。
    model_catalog_cfg = cfg.get("model_catalog", {}) or {}
    model_catalog: dict = {}
    if model_catalog_cfg.get("path"):
        model_catalog["path"] = str(resolve_path(model_catalog_cfg["path"]))
    if model_catalog_cfg.get("price_script"):
        model_catalog["price_script"] = str(resolve_path(model_catalog_cfg["price_script"]))

    deps = NodeDeps(
        runner=NodeRunner(llm=llm),
        registry=registry,
        artifacts=artifacts_mgr,
        kb=kb,
        rag=rag,
        eval_tool=eval_tool or None,
        bake_off_config=bake_off_cfg or None,
        tool_catalog=tool_catalog or None,
        model_catalog=model_catalog or None,
    )
    return deps, db_path
