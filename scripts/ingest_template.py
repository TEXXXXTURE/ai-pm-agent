# [C 2026-09-10] T4-4 模板吸收程序
"""ingest_template.py — 模板吸收 CLI（compress / extract 两种模式）。

用途：把 PRD 模板、决策记录模板等"写作规矩"吸收进知识库，
或把模板文件提炼成纯文本写作指引（不调 LLM，纯文本处理）。

用法：
    # extract：提炼模板写作规矩（输出纯文本到 stdout）
    python scripts/ingest_template.py --input artifacts/templates/prd.md --mode extract

    # compress：把模板压缩成一条档案写入知识库
    python scripts/ingest_template.py \
        --input artifacts/templates/decision_record.md \
        --mode compress --type decision --id DR-2026-001 \
        --title "决策记录模板" --tags "模板,决策"

参数：
    --input       模板文件路径（必填）
    --mode        compress | extract（必填）

    compress 模式额外参数（必填）：
      --type       decision | eval | case
      --id         档案 id
      --title      标题
    compress 模式可选：
      --tags       逗号分隔标签
      --store-path 知识库根目录；默认从 config.yaml 的 kb.store_path 读取

输出：
    compress 成功：stdout 输出 {"status": "ok", "path": "...", "id": ..., "type": ...}
    extract：stdout 直接 print 提炼后的纯文本（不是 JSON）

退出码：0 成功；1 运行时错误；2 参数错误（含 type 非法）。

依赖：标准库 + kb.writer + kernel.config，不引入新依赖。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kb.store import ARCHIVE_TYPES
from kb.writer import KBWriter
from kernel.config import PROJECT_ROOT, load_config


def _resolve_path(path: str) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _extract_rules(text: str) -> str:
    """从 markdown 模板中提炼写作规矩摘要。

    规则：保留所有标题行（# 开头）、列表项（- 或 * 开头）、表格行（| 开头），
    去掉其它行；按出现顺序拼接为纯文本。
    """
    kept: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("#")
            or stripped.startswith("-")
            or stripped.startswith("*")
            or stripped.startswith("|")
        ):
            kept.append(stripped)
    return "\n".join(kept)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数；compress 模式下校验额外必填项。"""
    parser = argparse.ArgumentParser(
        prog="ingest_template.py",
        description="模板吸收 CLI（compress 写库 / extract 提炼写作规矩）",
    )
    parser.add_argument("--input", required=True, help="模板文件路径（必填）")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["compress", "extract"],
        help="模式：compress 写入知识库；extract 提炼写作规矩",
    )
    # compress 模式专用参数（mode 在 parse 后再校验必填）
    # --type 不用 argparse choices 拦截：让 KBWriter.write_back 抛 ValueError
    # 后由 _run_compress 捕获，输出友好错误（符合验收要求：type 非法退出 2）。
    parser.add_argument(
        "--type",
        default=None,
        help="compress 模式必填：decision | eval | case",
    )
    parser.add_argument("--id", default=None, help="compress 模式必填：档案 id")
    parser.add_argument("--title", default=None, help="compress 模式必填：标题")
    parser.add_argument(
        "--tags",
        default="",
        help="compress 模式可选：逗号分隔标签，如 模板,决策",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="compress 模式可选：知识库根目录；默认读 config.yaml 的 kb.store_path",
    )
    args = parser.parse_args(argv)

    # 校验 input 文件存在性（参数错误退出 2）
    input_path = _resolve_path(args.input)
    if not input_path.exists():
        parser.error(f"--input 文件不存在: {input_path}")
    args.input_path = input_path

    # compress 模式额外必填校验
    if args.mode == "compress":
        missing = []
        if not args.type:
            missing.append("--type")
        if not args.id:
            missing.append("--id")
        if not args.title:
            missing.append("--title")
        if missing:
            parser.error(
                f"compress 模式下以下参数为必填项: {', '.join(missing)}"
            )

    return args


def _run_compress(args: argparse.Namespace) -> int:
    """compress 模式：读 input 内容写库。"""
    content = args.input_path.read_text(encoding="utf-8")

    tags = [t.strip() for t in args.tags.split(",") if t.strip()] if args.tags else []
    archive = {
        "id": args.id,
        "type": args.type,
        "title": args.title,
        "content": content,
        "tags": tags,
    }

    try:
        if args.store_path:
            store_path = _resolve_path(args.store_path)
        else:
            cfg = load_config()
            store_path = _resolve_path(cfg["kb"]["store_path"])
    except Exception as exc:
        print(f"[ingest_template] 配置/路径解析失败: {exc}", file=sys.stderr)
        return 1

    try:
        writer = KBWriter(str(store_path))
        path = writer.write_back(archive)
    except ValueError as exc:
        # type 非法：友好错误到 stderr，退出 2
        print(f"[ingest_template] 档案类型非法: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"[ingest_template] 写入失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(
        {"status": "ok", "path": str(path), "id": args.id, "type": args.type},
        ensure_ascii=False, indent=2,
    ))
    return 0


def _run_extract(args: argparse.Namespace) -> int:
    """extract 模式：提炼写作规矩纯文本，直接 print。"""
    text = args.input_path.read_text(encoding="utf-8")
    rules = _extract_rules(text)
    print(rules)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.mode == "compress":
        return _run_compress(args)
    return _run_extract(args)


if __name__ == "__main__":
    sys.exit(main())
