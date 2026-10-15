# 知识库入库命令行工具
"""rag_ingest.py — 领域知识库入库 CLI（存量批量 / 增量单文档 / inbox 目录 / 飞书拉取四个入口）。

用途：把资料写进 ChromaDB 的 agent_knowledge collection，供 rag_query.py 检索。

四种入口（规划 §七）：
1. 存量批量（v1 保留）：--layer concept|curated-paper|tracked-paper [--source ...] [--clean]
2. 增量单文档：--incremental --file <路径>    按《增量文档格式契约》解析 frontmatter 入库；
              --incremental --text "<文本>" [--title "<标题>"]  直接投喂，自动包壳
              （source 取内容 hash、type 取 note），必填缺失直接报错并列出缺项。
3. inbox 目录投递：--inbox [目录]   扫描目录下 .md/.txt 逐篇走增量入库，
   已入过的按 content_hash 跳过，结束时报告入库/跳过/失败计数。
   不带目录参数时用 <domain_kb.source_path>/inbox（即 domain_kb/source/inbox）。
4. 飞书输入同步：--from-feishu --space <空间id> [--node <节点id>]   从飞书知识库
   拉 docx 正文入增量域（domain=increment）。需要用户身份 OAuth（见
   scripts/feishu_auth.py）；tenant token 下列不到空间，不是权限问题而是身份问题。

用法（项目根下，python 3.11）：
    python scripts/rag_ingest.py --layer concept
    python scripts/rag_ingest.py --layer curated-paper --clean
    python scripts/rag_ingest.py --incremental --file domain_kb/source/inbox/note.md
    python scripts/rag_ingest.py --incremental --text "要点第一条" --title "手输笔记"
    python scripts/rag_ingest.py --inbox domain_kb/source/inbox/
    python scripts/rag_ingest.py --from-feishu --list-spaces
    python scripts/rag_ingest.py --from-feishu --space <space_id>
    python scripts/rag_ingest.py --from-feishu --space <space_id> --node <node_token>

参数：
    --layer   存量批量知识层：concept | curated-paper | tracked-paper（与另三入口四选一）
    --source  存量批量：源路径；增量模式：覆盖文档 source（幂等锚点），不给则用 frontmatter
              或内容 hash（自动包壳）
    --clean   可选，存量批量入库前先删除该层已有 chunk，清空条数计入输出
    --config  可选，配置文件路径；缺省走 kernel.config.load_config()
    --incremental  增量单文档入库（配 --file 或 --text 之一）
    --file    增量：待入库文档路径
    --text    增量：直接投喂的文本（无 frontmatter → 自动包壳）
    --title   增量：标题（--text 模式没有标题线索时使用）
    --inbox   可选目录，inbox 目录投递入口；不带目录时用 <source_path>/inbox
    --from-feishu        飞书入口：从飞书知识库拉文档入库（增量域）
    --space              飞书知识空间 id（--from-feishu 时给，或用 --list-spaces 只列空间）
    --node               可选，起点节点 node_token（只处理该节点及其子树）
    --list-spaces        只列可见知识空间（真调 GET /wiki/v2/spaces），不入库
    --limit              飞书入口：最多入库几篇（0 = 不限）
    --dry-run            飞书入口：只拉取不入库（验证拉取链路）
    --no-recursive       飞书入口：不递归子节点
    --registry           飞书入口：token registry 路径；缺省用 config.yaml 口径

输出（stdout，人类可读文本）：单篇模式打印入库摘要（source / doc_id / 写入 chunk 数 /
删除旧 chunk 数 / 是否跳过），inbox 模式打印扫描、入库、跳过、失败计数，飞书入口打印
空间清单或扫描节点数、入库文档数、跳过清单、逐篇回读校验结果。
退出码：0 成功，其中「全部跳过」按 0 处理；1 运行时错误（路径不存在、格式契约不满足、
JSON 解析失败等）；2 参数错误；3 飞书需要用户重新授权（token 失效且 refresh 也失效）。

依赖：仅标准库 + 项目内模块（kb.rag.ingest / kb.rag.contract / kb.feishu_sync / kernel.config）。
"""
from __future__ import annotations

import argparse
import datetime
import json
import sys
import time
from pathlib import Path

# 让裸命令（python scripts/rag_ingest.py）在仓库根直接可跑：把 src/ 挂进导入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kb.feishu_sync import FeishuAuthError, FeishuSync, verify_source
from kb.rag.contract import parse_document
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
# inbox 缺省目录（相对 source_path）
DEFAULT_INBOX_SOURCE = "inbox"
# inbox 扫描识别的扩展名（小写比较）
INBOX_SUFFIXES = (".md", ".txt")
# 飞书入口回读校验片段长度
VERIFY_SAMPLE_CHARS = 100


def _resolve_path(path: str | Path) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _source_root(cfg: dict) -> Path:
    """config 的 domain_kb.source_path 解析为绝对路径。"""
    source_path = ((cfg or {}).get("domain_kb") or {}).get("source_path")
    return _resolve_path(str(source_path or "./domain_kb/source"))


def _default_source(cfg: dict, layer: str) -> Path:
    """按层解析缺省源路径（相对 config 的 domain_kb.source_path）。"""
    base = _source_root(cfg)
    if layer == "concept":
        return base / DEFAULT_CONCEPT_SOURCE
    return base / DEFAULT_PAPER_SOURCE


def _default_inbox(cfg: dict) -> Path:
    """inbox 缺省目录：<source_path>/inbox（即 domain_kb/source/inbox）。"""
    return _source_root(cfg) / DEFAULT_INBOX_SOURCE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；三入口必须三选一，非法组合由 argparse 拒绝并退出 2。"""
    parser = argparse.ArgumentParser(
        prog="rag_ingest.py",
        description="领域知识库入库 CLI（存量批量 / 增量单文档 / inbox 目录）",
    )
    parser.add_argument(
        "--layer",
        default=None,
        choices=list(LAYERS),
        help="存量批量：知识层 concept | curated-paper | tracked-paper",
    )
    parser.add_argument(
        "--source",
        default=None,
        help="存量批量：源路径（concept 为目录，论文层为 JSON 文件）；"
        "增量模式：覆盖文档 source（幂等锚点）",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="存量批量：入库前先清空该层已有 chunk",
    )
    parser.add_argument(
        "--config",
        default=None,
        help="配置文件路径；缺省走 load_config() 默认",
    )
    parser.add_argument(
        "--incremental",
        action="store_true",
        help="增量单文档入库（配 --file 或 --text）",
    )
    parser.add_argument(
        "--file",
        default=None,
        help="增量：待入库文档路径（.md/.txt，按格式契约解析 frontmatter）",
    )
    parser.add_argument(
        "--text",
        default=None,
        help="增量：直接投喂的文本（无 frontmatter 时自动包壳）",
    )
    parser.add_argument(
        "--title",
        default=None,
        help="增量：标题（--text 模式没有标题线索时使用）",
    )
    parser.add_argument(
        "--inbox",
        nargs="?",
        const="",
        default=None,
        metavar="DIR",
        help="inbox 目录投递：扫描目录下 .md/.txt 入库；不带目录时用 <source_path>/inbox",
    )
    # ── 飞书入口（第四个入口）──
    parser.add_argument(
        "--from-feishu",
        action="store_true",
        help="飞书入口：从飞书知识库拉文档入库（增量域 domain=increment）",
    )
    parser.add_argument("--space", default=None, help="飞书知识空间 id（--from-feishu 时给）")
    parser.add_argument("--node", default=None, help="飞书入口：起点节点 node_token（含其子树）")
    parser.add_argument(
        "--list-spaces",
        action="store_true",
        help="飞书入口：只列可见知识空间（真调 GET /wiki/v2/spaces），不入库",
    )
    parser.add_argument("--limit", type=int, default=0, help="飞书入口：最多入库几篇（0 = 不限）")
    parser.add_argument("--dry-run", action="store_true", help="飞书入口：只拉取不入库")
    parser.add_argument(
        "--no-recursive", action="store_true", help="飞书入口：不递归子节点（只处理直接下一层）"
    )
    parser.add_argument("--registry", default=None, help="飞书入口：token registry 路径；缺省用 config 口径")
    args = parser.parse_args(argv)
    _validate_args(parser, args)
    return args


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """四入口互斥校验与各模式参数校验（失败一律 parser.error → 退出码 2）。"""
    mode_count = sum(
        [bool(args.layer), bool(args.incremental), args.inbox is not None, bool(args.from_feishu)]
    )
    if mode_count == 0:
        parser.error(
            "必须指定入库入口：--layer（存量批量）/ --incremental / --inbox / --from-feishu 四选一"
        )
    if mode_count > 1:
        parser.error("入库入口只能四选一：--layer / --incremental / --inbox / --from-feishu")
    if args.incremental and bool(args.file) == bool(args.text):
        parser.error("--incremental 需要且只需要 --file 或 --text 之一")
    if args.clean and not args.layer:
        parser.error("--clean 只适用于 --layer 存量批量入库")
    if args.from_feishu and not (args.space or args.list_spaces):
        parser.error("--from-feishu 需要 --space <空间id>（或 --list-spaces 只列空间）")
    if not args.from_feishu and (args.space or args.node or args.list_spaces):
        parser.error("--space / --node / --list-spaces 只适用于 --from-feishu（飞书入口）")


def build_ingester(config: dict) -> RAGIngest:
    """构造入库器（独立函数便于测试 monkeypatch 注入假对象）。"""
    return RAGIngest(config)


def build_sync(config: dict, registry: str | None = None) -> FeishuSync:
    """构造飞书同步客户端（独立函数便于测试 monkeypatch 注入假对象）。"""
    return FeishuSync(config, token_registry=registry)


def _load_config(args: argparse.Namespace) -> dict:
    """读配置（--config 给路径时按路径读，否则用项目默认 config.yaml）。"""
    return load_config(args.config) if args.config else load_config()


def _read_text(path: Path) -> str:
    """读文本文件：先 UTF-8，失败回退 GBK（Windows 中文记事本常存 GBK）。"""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="gbk")
        except (OSError, UnicodeDecodeError) as exc:
            raise ValueError(f"文档编码无法识别（既非 UTF-8 也非 GBK）：{path}（{exc}）") from exc
        print(f"[rag_ingest] 提示：{path} 按 GBK 解码入库", file=sys.stderr)
        return text
    except OSError as exc:
        raise ValueError(f"文档读取失败：{path}（{exc}）") from exc


def _file_date(path: Path) -> str:
    """文件最后修改日期（YYYY-MM-DD），作 frontmatter 缺 updated 时的缺省值。"""
    try:
        return datetime.date.fromtimestamp(path.stat().st_mtime).isoformat()
    except OSError:
        return datetime.date.today().isoformat()


def run_ingest(args: argparse.Namespace) -> dict:
    """存量批量入库，返回统计 dict。

    返回：{"mode": "stock", "layer", "source", "cleaned", "chunks", "elapsed"}。
    路径不存在 / JSON 解析失败 / 顶层不是 list 时抛异常，由 main 统一转退出码 1。
    """
    started = time.time()
    cfg = _load_config(args)

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
        "mode": "stock",
        "layer": args.layer,
        "source": str(source),
        "cleaned": cleaned,
        "chunks": written,
        "elapsed": time.time() - started,
    }


def run_incremental(args: argparse.Namespace) -> dict:
    """增量单文档入库（--file 读文件 / --text 直接投喂），返回统计 dict。

    流程：读文本 → 格式契约解析（无 frontmatter 自动包壳）→ ingest_document。
    必填缺失抛 DocumentFormatError（含缺项清单），由 main 转退出码 1。
    """
    cfg = _load_config(args)

    if args.file:
        path = _resolve_path(args.file)
        if not path.is_file():
            raise FileNotFoundError(f"增量文档不存在或不是文件：{path}")
        raw = _read_text(path)
        title_fallback = path.stem
        # 文件 mtime 只作「无 frontmatter 自动包壳」时的 updated；有 frontmatter 时
        # updated 必须由文档自己写（契约的必填字段，不做静默兜底）
        default_updated = _file_date(path)
        origin = str(path)
    else:
        raw = str(args.text)
        # 手输文本没有文件线索：标题回落顺序 = --title → 首个 # 标题 → 兜底标题
        title_fallback = None
        default_updated = datetime.date.today().isoformat()
        origin = "(stdin 文本)"

    doc = parse_document(
        raw,
        title=args.title,
        source=args.source,
        updated=default_updated,
        title_fallback=title_fallback,
    )

    ingester = build_ingester(cfg)
    stats = ingester.ingest_document(
        raw,
        doc["type"],
        doc["source"],
        title=doc["title"],
        updated=doc["updated"],
        tags=doc["tags"],
        auto_wrapped=doc["auto_wrapped"],
    )
    stats.update(
        {
            "mode": "incremental",
            "origin": origin,
            "auto_wrapped": doc["auto_wrapped"],
            "updated": doc["updated"],
        }
    )
    return stats


def list_inbox_files(inbox: Path) -> list[Path]:
    """列出 inbox 下待入库文件：递归 .md/.txt，跳过隐藏项与下划线开头的草稿，路径排序。"""
    found: list[Path] = []
    if not inbox.is_dir():
        return found
    for path in sorted(inbox.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in INBOX_SUFFIXES:
            continue
        relatives = path.relative_to(inbox).parts
        if any(part.startswith(".") or part.startswith("_") for part in relatives):
            continue
        found.append(path)
    return found


def run_inbox(args: argparse.Namespace) -> dict:
    """inbox 目录投递入库：逐篇走增量入库，单篇失败不中断整批。

    返回：{"mode": "inbox", "dir", "total", "ingested", "skipped", "failed",
           "chunks", "records"}（chunks = 本次真写入的 chunk 合计，跳过篇不计）。
    """
    cfg = _load_config(args)
    inbox = _resolve_path(args.inbox) if args.inbox else _default_inbox(cfg)
    if not inbox.is_dir():
        raise FileNotFoundError(f"inbox 目录不存在或不是目录：{inbox}")

    files = list_inbox_files(inbox)
    ingester = build_ingester(cfg)

    records: list[dict] = []
    for path in files:
        record: dict = {"file": str(path)}
        try:
            raw = _read_text(path)
            doc = parse_document(
                raw, updated=_file_date(path), title_fallback=path.stem
            )
            stats = ingester.ingest_document(
                raw,
                doc["type"],
                doc["source"],
                title=doc["title"],
                updated=doc["updated"],
                tags=doc["tags"],
                auto_wrapped=doc["auto_wrapped"],
            )
            record.update(
                {
                    "status": "skipped" if stats.get("skipped") else "ingested",
                    "source": doc["source"],
                    # chunks = 本次实际写入的 chunk 数（跳过为 0，故汇总口径即「真写入」）
                    "chunks": stats.get("chunks", 0),
                    "source_chunks": stats.get("source_chunks", stats.get("chunks", 0)),
                    "auto_wrapped": doc["auto_wrapped"],
                }
            )
        except Exception as exc:  # 单篇坏文件不拖垮整批：留痕后继续
            record.update({"status": "failed", "error": str(exc)})
            print(f"[rag_ingest] inbox 单篇失败，已跳过：{path}（{exc}）", file=sys.stderr)
        records.append(record)

    ingested = [item for item in records if item["status"] == "ingested"]
    skipped = [item for item in records if item["status"] == "skipped"]
    failed = [item for item in records if item["status"] == "failed"]
    return {
        "mode": "inbox",
        "dir": str(inbox),
        "total": len(records),
        "ingested": len(ingested),
        "skipped": len(skipped),
        "failed": len(failed),
        "chunks": sum(int(item.get("chunks") or 0) for item in records),
        "records": records,
    }


def run_feishu(args: argparse.Namespace) -> dict:
    """飞书入口：列空间，或从空间/节点拉文档入库（± 回读校验）。

    返回：{"mode": "feishu-list-spaces" | "feishu", ...}。token 失效且刷新失败时
    抛 FeishuAuthError，由 main 转退出码 3。
    """
    started = time.time()
    cfg = _load_config(args)
    sync = build_sync(cfg, args.registry)

    if args.list_spaces:
        return {
            "mode": "feishu-list-spaces",
            "spaces": sync.list_spaces(),
            "elapsed": time.time() - started,
        }

    ingester = None if args.dry_run else build_ingester(cfg)
    summary = sync.sync_space(
        args.space,
        node=args.node or "",
        ingest=ingester,
        limit=args.limit,
        recursive=not args.no_recursive,
        dry_run=args.dry_run,
    )

    # 回读校验：按 source 从库里把刚入的内容查回来（真读 Chroma）
    verify: list[dict] = []
    if ingester is not None:
        for entry in summary["ingested"]:
            check = verify_source(ingester, entry["url"], sample_chars=VERIFY_SAMPLE_CHARS)
            verify.append({**check, "title": entry["title"]})
    summary["mode"] = "feishu"
    summary["verify"] = verify
    summary["elapsed"] = time.time() - started
    return summary


def print_stats(stats: dict) -> None:
    """按模式打印人类可读统计（存量批量的输出形态与 v1 保持一致）。"""
    mode = stats.get("mode")
    if mode == "feishu-list-spaces":
        spaces = stats["spaces"]
        print("模式: 飞书列空间")
        print(f"可见知识空间数: {len(spaces)}")
        for space in spaces:
            print(
                f"  - space_id={space.get('space_id')} 名称={space.get('name')} "
                f"类型={space.get('space_type')} 可见性={space.get('visibility')}"
            )
        print(f"耗时: {stats['elapsed']:.1f} 秒")
        return
    if mode == "feishu":
        print("模式: 飞书知识库拉取" + ("（dry-run 不入库）" if stats.get("dry_run") else ""))
        print(
            f"飞书空间: {stats['space_id']}"
            + (f"（起点节点 {stats['node']}）" if stats.get("node") else "")
        )
        print(f"扫描节点: {stats['scanned']}")
        print(f"入库文档: {len(stats['ingested'])} 篇，写入 chunk 总数: {stats['chunks']}")
        for entry in stats["ingested"]:
            tail = "（内容未变，跳过）" if entry.get("skipped") else ""
            print(
                f"  - {entry['title']}（{entry['obj_type']}，{entry['chars']} 字，"
                f"{entry['chunks']} chunk）{entry['url']}{tail}"
            )
            if entry.get("sample"):
                print(f"    片段: {entry['sample'].replace(chr(10), ' ')[:120]}")
        if stats["skipped"]:
            print(f"跳过: {len(stats['skipped'])} 篇")
            for entry in stats["skipped"]:
                print(f"  - {entry['title']}（{entry['obj_type']}）：{entry['reason']}")
        for check in stats.get("verify") or []:
            print(
                f"回读校验: {check['title']} → 命中 chunk {check['chunks']} 条，"
                f"片段: {check['sample']!r}"
            )
        print(f"耗时: {stats['elapsed']:.1f} 秒")
        return
    if mode == "inbox":
        print(f"模式: inbox 目录投递")
        print(f"目录: {stats['dir']}")
        print(f"扫描文件: {stats['total']}")
        print(f"入库: {stats['ingested']}")
        print(f"跳过: {stats['skipped']}")
        print(f"失败: {stats['failed']}")
        print(f"写入 chunk 总数: {stats['chunks']}")
        print(f"库内 chunk 合计: {sum(int(item.get('source_chunks') or 0) for item in stats['records'])}")
        return
    if mode == "incremental":
        print("模式: 增量单文档")
        print(f"文档来源: {stats['origin']}")
        print(f"source: {stats['source']}")
        print(f"doc_id: {stats['doc_id']}")
        print(f"文档类型: {stats['doc_type']}（自动包壳: {'是' if stats.get('auto_wrapped') else '否'}）")
        print(f"内容 hash: {stats['content_hash']}")
        print(f"策略版本: {stats['strategy_version']}")
        print(f"跳过: {'是' if stats.get('skipped') else '否'}")
        print(f"写入 chunk 总数: {stats.get('written', stats['chunks'])}")
        if stats.get("skipped"):
            print(f"库内已有 chunk: {stats.get('source_chunks', stats['chunks'])}")
        else:
            print(f"该 source 库内 chunk 合计: {stats.get('source_chunks', stats['chunks'])}")
        print(f"删除旧 chunk: {stats['removed']}")
        return
    print(f"层名: {stats['layer']}")
    print(f"源路径: {stats['source']}")
    if stats.get("cleaned"):
        print(f"清空条数: {stats['cleaned']}")
    print(f"写入 chunk 总数: {stats['chunks']}")
    print(f"耗时: {stats['elapsed']:.1f} 秒")


def main(argv: list[str] | None = None) -> int:
    """主流程：解析参数 → 按入口入库 → 打印统计。返回进程退出码。"""
    args = parse_args(argv)

    try:
        if args.from_feishu:
            stats = run_feishu(args)
        elif args.inbox is not None:
            stats = run_inbox(args)
        elif args.incremental:
            stats = run_incremental(args)
        else:
            stats = run_ingest(args)
    except FeishuAuthError as exc:
        print(f"[rag_ingest] 飞书身份失效，需要用户重新授权：{exc}", file=sys.stderr)
        print(
            "[rag_ingest] 下一步：运行 python scripts/feishu_auth.py"
            "（生成授权链接并等本机回调），授权完成后重跑本命令",
            file=sys.stderr,
        )
        return 3
    except Exception as exc:
        print(f"[rag_ingest] 入库失败：{exc}", file=sys.stderr)
        return 1

    print_stats(stats)
    return 0


if __name__ == "__main__":
    sys.exit(main())
