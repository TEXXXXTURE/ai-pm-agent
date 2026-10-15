# 知识库检索命令行工具
"""rag_query.py — 领域知识库检索 CLI（双路召回 bm25s ∥ 向量 + RRF 融合）。

用途：把问题文本交给 RAGStore 做双路召回 + RRF 融合检索，默认输出人类可读结果段，
--json 输出结构化结果（含查询输出契约字段，给下游 Agent 消费）。

用法（项目根下）：
    python scripts/rag_query.py --query "什么是 ReAct"
    python scripts/rag_query.py --query "planning" --layers concept --category planning
    python scripts/rag_query.py --query "Agent 记忆" --top-k 3 --json

参数：
    --query     必填，查询文本
    --layers    可选，逗号分隔的层名，如 concept,curated-paper
    --category  可选，分类过滤（单值）
    --top-k     可选，返回条数；缺省取 config domain_kb.retrieval.top_k（5）
    --json      可选，输出 JSON（RAGStore.search 返回的 list 原样序列化）
    --config    可选，配置文件路径；缺省走 kernel.config.load_config()

输出：
    默认人类可读——每条一段：序号 + title（空则用 source）、[layer/category] score=x.xxxx、
    融合明细（rrf=… bm25#n vec#n，未命中的那路显示 -）、source: ...、
    查询输出契约（块类型 / 父块 / 标题路径）、正文 content 前 200 字符（换行压成空格）；
    子块额外打「父块上下文」一行（父块全文预览）。条间空行。
    无结果打印"未检索到相关内容"，退出码仍为 0。

退出码：0 成功；1 运行时错误；2 参数错误。

依赖：仅标准库 + 项目内模块（kb.rag.store / kernel.config）。
"""
from __future__ import annotations

import argparse
import json
import sys

from kb.rag.store import DEFAULT_TOP_K, RAGStore
from kernel.config import load_config

# 人类可读输出里正文预览的字符数上限
CONTENT_PREVIEW_CHARS = 200
# 人类可读输出里父块上下文预览的字符数上限（JSON 模式给全文）
PARENT_PREVIEW_CHARS = 200


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；--layers 拆分成列表挂在 args._layers 上。"""
    parser = argparse.ArgumentParser(
        prog="rag_query.py",
        description="领域知识库检索 CLI（双路召回 + RRF 融合）",
    )
    parser.add_argument("--query", required=True, help="查询文本（必填）")
    parser.add_argument(
        "--layers",
        default=None,
        help="层名过滤，逗号分隔，如 concept,curated-paper",
    )
    parser.add_argument("--category", default=None, help="分类过滤（单值）")
    parser.add_argument(
        "--top-k",
        type=int,
        default=None,
        help="返回条数；缺省取 config domain_kb.retrieval.top_k",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="输出 JSON（含全字段）",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径；缺省走 load_config() 默认",
    )
    args = parser.parse_args(argv)

    if args.top_k is not None and args.top_k <= 0:
        parser.error("--top-k 必须为正整数")

    args._layers = _parse_layers(args.layers)
    return args


def _parse_layers(raw: str | None) -> list[str] | None:
    """把 --layers 的逗号分隔串拆成列表；空串或缺省返回 None（表示不过滤）。"""
    if not raw:
        return None
    layers = [part.strip() for part in str(raw).split(",") if part.strip()]
    return layers or None


def _default_top_k(cfg: dict) -> int:
    """从 config 的 domain_kb.retrieval.top_k 取缺省条数，缺失/非法时回落到 5。"""
    retrieval = ((cfg or {}).get("domain_kb") or {}).get("retrieval") or {}
    try:
        value = int(retrieval.get("top_k") or DEFAULT_TOP_K)
    except (TypeError, ValueError):
        value = DEFAULT_TOP_K
    return value if value > 0 else DEFAULT_TOP_K


def build_store(config: dict) -> RAGStore:
    """构造检索器（独立函数便于测试 monkeypatch 注入假对象）。"""
    return RAGStore(config)


def run_query(args: argparse.Namespace) -> list[dict]:
    """执行检索，返回 RAGStore.search 的结果列表（原样，不加工）。"""
    cfg = load_config(args.config) if args.config else load_config()
    store = build_store(cfg)
    top_k = args.top_k if args.top_k is not None else _default_top_k(cfg)
    return store.search(
        query=args.query,
        top_k=top_k,
        layers=args._layers,
        category=args.category,
    )


def _rank_detail(item: dict) -> str:
    """融合明细一行：rrf 分 + 两路各自排名（未命中的那路显示 -）。"""
    parts: list[str] = []
    if item.get("rrf_score") is not None:
        try:
            parts.append(f"rrf={float(item.get('rrf_score')):.6f}")
        except (TypeError, ValueError):
            parts.append(f"rrf={item.get('rrf_score')}")
    for key, label in (("bm25_rank", "bm25"), ("vector_rank", "vec")):
        if key not in item:
            continue
        value = item.get(key)
        parts.append(f"{label}#{'-' if value is None else int(value)}")
    return "  ".join(parts)


def _contract_detail(item: dict) -> str:
    """查询输出契约一行：块类型 / 父块 / 标题路径（都为空则该行不打）。"""
    parts: list[str] = []
    chunk_type = str(item.get("chunk_type") or "")
    if chunk_type:
        parts.append(f"块类型: {chunk_type}")
    parent_id = str(item.get("parent_id") or "")
    if parent_id:
        parts.append(f"父块: {parent_id}")
    heading_path = str(item.get("heading_path") or "")
    if heading_path:
        parts.append(f"标题路径: {heading_path}")
    return "  ".join(parts)


def _format_item(index: int, item: dict) -> str:
    """把一条检索结果格式化成人类可读段落（含融合明细与查询输出契约字段）。"""
    title = str(item.get("title") or "").strip() or str(item.get("source") or "")
    layer = str(item.get("layer") or "")
    category = str(item.get("category") or "")
    source = str(item.get("source") or "")
    try:
        score = float(item.get("score") or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    content = " ".join(str(item.get("content") or "").split())[:CONTENT_PREVIEW_CHARS]

    lines = [
        f"{index}. {title}",
        f"   [{layer}/{category}] score={score:.4f}",
    ]
    rank_detail = _rank_detail(item)
    if rank_detail:
        lines.append(f"   {rank_detail}")
    lines.append(f"   source: {source}")
    contract_detail = _contract_detail(item)
    if contract_detail:
        lines.append(f"   {contract_detail}")
    lines.append(f"   {content}")

    # 子块的父块上下文（给下游 Agent 消费的同一份契约在 CLI 上也要看得见）
    if "parent_content" in item:
        parent_text = " ".join(str(item.get("parent_content") or "").split())
        lines.append(
            f"   父块上下文({len(str(item.get('parent_content') or ''))} 字): "
            f"{parent_text[:PARENT_PREVIEW_CHARS]}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """主流程：解析参数 → 检索 → 打印结果。返回进程退出码。"""
    args = parse_args(argv)

    try:
        results = run_query(args)
    except Exception as exc:
        print(f"[rag_query] 检索失败：{exc}", file=sys.stderr)
        return 1

    if args.as_json:
        print(json.dumps(results, ensure_ascii=False, indent=2))
        return 0

    if not results:
        print("未检索到相关内容")
        return 0

    blocks = [_format_item(index, item) for index, item in enumerate(results, 1)]
    print("\n\n".join(blocks))
    return 0


if __name__ == "__main__":
    sys.exit(main())

