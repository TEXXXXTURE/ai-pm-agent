# 知识库入库命令行工具
"""rag_ingest.py — 领域知识库入库 CLI（概念层 Markdown / 论文层 JSON → ChromaDB）。

用途：把 domain_kb/source 下的概念层文档与论文层 JSON 写进 ChromaDB 的
agent_knowledge collection，供 rag_query.py 检索。入库逐篇嵌入较慢，过程按
源单位打进度行到 stderr（概念层每 20 个文件、论文层每 50 条）。

用法（项目根下）：
    python scripts/rag_ingest.py --layer concept
    python scripts/rag_ingest.py --layer curated-paper
    python scripts/rag_ingest.py --layer concept --clean
    python scripts/rag_ingest.py --layer curated-paper --source 自定义.json

参数：
    --layer   必填，知识层：concept | curated-paper | tracked-paper
    --source  可选，源路径；concept 为目录，论文层为 JSON 文件路径。
              缺省解析：concept → <source_path>/concept；
              curated-paper/tracked-paper → <source_path>/curated-papers/papers_with_abstract.json
    --clean   可选，入库前先删除该层已有 chunk（delete_by_layer），清空条数计入输出
    --config  可选，配置文件路径；缺省走 kernel.config.load_config()

输出（stdout，人类可读文本）：层名、源路径、（--clean 时）清空条数、
写入 chunk 总数、耗时秒（保留 1 位小数）。

退出码：0 成功；1 运行时错误（路径不存在、JSON 解析失败等）；2 参数错误。

依赖：仅标准库 + 项目内模块（kb.rag.ingest / kernel.config）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from kb.rag.ingest import RAGIngest
from kernel.config import PROJECT_ROOT, load_config

# 支持的层名（与 RAGIngest 的 LAYER_ID_PREFIX 对齐）
LAYERS = ("concept", "curated-paper", "tracked-paper")
# 概念层进度打印间隔：每处理满该数量的文件打一行
CONCEPT_PROGRESS_EVERY = 20
# 论文层进度打印间隔：每处理满该数量的条目打一行
PAPER_PROGRESS_EVERY = 50
# 概念层缺省源目录（相对 source_path）
DEFAULT_CONCEPT_SOURCE = "concept"
# 论文层缺省源文件（相对 source_path）
DEFAULT_PAPER_SOURCE = "curated-papers/papers_with_abstract.json"


def _resolve_path(path: str | Path) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _default_source(cfg: dict, layer: str) -> Path:
    """按层解析缺省源路径（相对 config 的 domain_kb.source_path）。"""
    source_path = ((cfg or {}).get("domain_kb") or {}).get("source_path")
    base = _resolve_path(str(source_path or "./domain_kb/source"))
    if layer == "concept":
        return base / DEFAULT_CONCEPT_SOURCE
    return base / DEFAULT_PAPER_SOURCE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数（--layer 必填，非法层名由 argparse 拒绝并退出 2）。"""
    parser = argparse.ArgumentParser(
        prog="rag_ingest.py",
        description="领域知识库入库 CLI（概念层 Markdown / 论文层 JSON → ChromaDB）",
    )
    parser.add_argument(
        "--layer",
        required=True,
        choices=list(LAYERS),
        help="知识层：concept | curated-paper | tracked-paper（必填）",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="源路径：concept 为目录，论文层为 JSON 文件；缺省按 source_path 解析",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="入库前先清空该层已有 chunk",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径；缺省走 load_config() 默认",
    )
    return parser.parse_args(argv)


def build_ingester(config: dict) -> RAGIngest:
    """构造入库器（独立函数便于测试 monkeypatch 注入假对象）。"""
    return RAGIngest(config)


def run_ingest(args: argparse.Namespace) -> dict:
    """执行入库并返回统计 dict。

    返回：{"layer", "source", "cleaned", "chunks", "elapsed"}。
    路径不存在 / JSON 解析失败 / 顶层不是 list 时抛异常，由 main 统一转退出码 1。
    """
    started = time.time()
    cfg = load_config(args.config) if args.config else load_config()

    source = _resolve_path(args.source) if args.source else _default_source(cfg, args.layer)

    papers: list[dict] | None = None
    if args.layer == "concept":
        if not source.is_dir():
            raise FileNotFoundError(f"概念层源目录不存在或不是目录：{source}")
    else:
        if not source.is_file():
            raise FileNotFoundError(f"论文层源文件不存在：{source}")
        try:
            papers = json.loads(source.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"论文 JSON 解析失败：{source}（{exc}）") from exc
        if not isinstance(papers, list):
            raise ValueError(
                f"论文 JSON 顶层必须是 list[dict]，实际为 {type(papers).__name__}：{source}"
            )

    ingester = build_ingester(cfg)

    # --clean：先清空该层，清空条数计入输出
    cleaned = ingester.delete_by_layer(args.layer) if args.clean else 0

    if args.layer == "concept":
        written = ingester.ingest_concept_dir(
            str(source), progress_every=CONCEPT_PROGRESS_EVERY
        )
    else:
        written = ingester.ingest_papers(
            papers, args.layer, progress_every=PAPER_PROGRESS_EVERY
        )

    return {
        "layer": args.layer,
        "source": str(source),
        "cleaned": cleaned,
        "chunks": written,
        "elapsed": time.time() - started,
    }


def main(argv: list[str] | None = None) -> int:
    """主流程：解析参数 → 入库 → 打印统计。返回进程退出码。"""
    args = parse_args(argv)

    try:
        stats = run_ingest(args)
    except Exception as exc:
        print(f"[rag_ingest] 入库失败：{exc}", file=sys.stderr)
        return 1

    print(f"层名: {stats['layer']}")
    print(f"源路径: {stats['source']}")
    if args.clean:
        print(f"清空条数: {stats['cleaned']}")
    print(f"写入 chunk 总数: {stats['chunks']}")
    print(f"耗时: {stats['elapsed']:.1f} 秒")
    return 0


if __name__ == "__main__":
    sys.exit(main())

