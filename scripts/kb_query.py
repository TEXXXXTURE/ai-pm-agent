# 知识库查询命令行工具
"""kb_query.py — 本地知识库查询 CLI。

用途：Pi 在做需求分析时，用关键词查询本地知识库中的历史
决策（decision）/ 评测（eval）/ 案例（case）档案，复用 KBStore 的关键词打分检索。

用法（项目根下）：
    python scripts/kb_query.py --query "用户访谈"
    python scripts/kb_query.py --query "登录方案" --type decision --top-k 5
    python scripts/kb_query.py --query "指标体系" --type decision,eval --top-k 5

参数：
    --query       查询词（必填）
    --type        档案类型，可选；支持 decision|eval|case，多个用逗号分隔或重复指定
    --top-k       返回条数，默认 3
    --store-path  知识库根目录；默认从 config.yaml 的 kb.store_path 读取

输出：JSON 到 stdout，结构同 KBStore.retrieve_relevant() 的返回值：
    {"status": "ok"/"empty", "query": ..., "archives": [...]}

退出码：0 成功；1 运行时错误；2 参数错误。

依赖：仅标准库 + 项目内模块（kb.store / kernel.config）。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kb.store import ARCHIVE_TYPES, KBStore
from kernel.config import PROJECT_ROOT, load_config


def _resolve_path(path: str) -> Path:
    """相对路径以 PROJECT_ROOT 为基准解析，绝对路径原样返回。"""
    p = Path(path)
    return p if p.is_absolute() else PROJECT_ROOT / p


def _parse_types(raw: list[str] | None) -> list[str] | None:
    """把 --type 参数合并 + 拆逗号；返回 None 表示不限类型。"""
    if not raw:
        return None
    out: list[str] = []
    for chunk in raw:
        for part in chunk.split(","):
            part = part.strip()
            if part:
                out.append(part)
    return out or None


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """解析命令行参数。"""
    parser = argparse.ArgumentParser(
        prog="kb_query.py",
        description="本地知识库查询 CLI（关键词打分检索 decision/eval/case 档案）",
    )
    parser.add_argument("--query", required=True, help="查询词（必填）")
    parser.add_argument(
        "--type",
        action="append",
        default=[],
        metavar="decision|eval|case",
        help="档案类型；可重复指定或用逗号分隔多类型",
    )
    parser.add_argument(
        "--top-k",
        type=int,
        default=3,
        help="返回条数（默认 3）",
    )
    parser.add_argument(
        "--store-path",
        default=None,
        help="知识库根目录；默认从 config.yaml 的 kb.store_path 读取",
    )
    args = parser.parse_args(argv)

    # 校验 type 取值
    types = _parse_types(args.type)
    if types:
        bad = [t for t in types if t not in ARCHIVE_TYPES]
        if bad:
            parser.error(
                f"非法 --type 值: {bad}；必须是 {list(ARCHIVE_TYPES)} 之一"
            )
    args._types = types

    if args.top_k <= 0:
        parser.error("--top-k 必须为正整数")

    return args


def main(argv: list[str] | None = None) -> int:
    """主流程。"""
    args = parse_args(argv)

    try:
        if args.store_path:
            store_path = _resolve_path(args.store_path)
        else:
            cfg = load_config()
            store_path = _resolve_path(cfg["kb"]["store_path"])
    except Exception as exc:
        print(f"[kb_query] 配置/路径解析失败: {exc}", file=sys.stderr)
        return 1

    try:
        kb = KBStore(str(store_path))
        result: dict[str, Any] = kb.retrieve_relevant(
            query=args.query,
            types=args._types,
            top_k=args.top_k,
        )
    except Exception as exc:
        print(f"[kb_query] 检索失败: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
