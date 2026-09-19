#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""model_catalog.py —— litellm 模型表查询工具（零模型调用）

用途
    给 `references/模型候选清单.md` 的字段提供可复核的取数口径。分两类：
    - **不变字段**（上下文窗口、输出上限、函数调用、流式）：读 litellm 自带的本地 json；
    - **价格**：不入清单（会变），用 `price` 子命令**实时取数**——优先拉 litellm 官方远端表，
      远端不可达则回退本地备份并明确标注「可能已过期」。
    本脚本不调用任何模型、不写库。

数据源
    本地表（不变字段 + price 的回退源），litellm 包内自带，默认路径随包自动定位（可用 `--table` 指定）；
    远端表（price 的首选源），litellm 官方仓库 raw 文件：
    https://raw.githubusercontent.com/BerriAI/litellm/main/model_prices_and_context_window.json
    两侧字段格式一致（同一个 json）。litellm 升级后本地文件被覆盖，字段随之更新。

用法
    # 1) 列出表内全部 provider 与条目计数（条目数降序），用于确定覆盖范围
    python scripts/model_catalog.py providers

    # 2) 按模型 id 输出不变字段（上下文 / 输出上限 / 函数调用 / 流式）+ 本地表单价
    python scripts/model_catalog.py models deepseek-chat deepseek-reasoner gpt-5.2

    # 3) 按关键词搜模型 id（找厂商型号时用，如 zai、moonshot、doubao、qwen）
    python scripts/model_catalog.py search doubao

    # 4) 实时查价（回答"某模型多少钱"一律走这条，不要引用清单或记忆里的历史数字）
    python scripts/model_catalog.py price deepseek-chat
    python scripts/model_catalog.py price deepseek-chat deepseek-reasoner --json

    # 5) 指定其它表文件 / 只输出 JSON（供程序消费）
    python scripts/model_catalog.py providers --table /path/to/table.json
    python scripts/model_catalog.py models deepseek-chat --json

参数
    providers [--table PATH] [--json] [--top N]   列 provider 与条目计数（--top 只显示前 N 名）
    models ID [ID ...] [--table PATH] [--json]    按 id 输出对照行；表内没有的 id 会明确标注
    search KW [--table PATH] [--limit N] [--json] 按关键词匹配模型 id
    price ID [ID ...] [--remote-url URL] [--timeout SEC] [--proxy URL] [--offline]
          [--table PATH] [--json]
          实时查价：输入/输出单价、上下文与输出上限、**本次取数来源**（远端 / 本地备份）、取数时间

说明
    - 单价统一换算为「美元 / 百万 token」展示（表内原值是「美元 / 单 token」，乘 1e6 得到）。
      只给美元原值，需要人民币时按当日实时汇率现场折算——汇率本身也会变，不落表。
    - 字段缺失显示 `-`，表示表未提供该字段，不等于「不支持」。价格缺失会另写一行提示。
    - 表中不含的模型 id（如硅基流动托管的 Qwen3-Embedding-8B）会标「表内未收录」，
      需另行核实，不得据本脚本臆断其规格。
    - `price` 走网络，其余子命令不联网。**远端失败不报错退出**：回退本地备份，并在输出里
      写明「用的是本地备份，可能已过期」。取数优先级：远端 → 本地备份。
      本机联网需走代理时用 `--proxy http://127.0.0.1:7892` 显式指定；经 `scripts/run-tool.sh`
      调用时代理环境变量被清空，若直连不通会自动走本地备份回退。
    - `models` / `search` 里的单价读的是本地表，只作量级参考，实时值一律以 `price` 为准。

"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys

def _default_table_path() -> str:
    """定位 litellm 包自带的模型价格表：优先随包路径，找不到返回空串（调用方走报错提示）。"""
    try:
        import litellm
        base = os.path.dirname(litellm.__file__)
    except Exception:
        return ""
    for name in ("model_prices_and_context_window_backup.json",
                 "model_prices_and_context_window.json"):
        cand = os.path.join(base, name)
        if os.path.isfile(cand):
            return cand
    return ""


DEFAULT_TABLE = _default_table_path()

# price 子命令的首选源：litellm 官方仓库 raw 表（与本地备份同一个 json，字段一致）
DEFAULT_REMOTE_URL = (
    "https://raw.githubusercontent.com/BerriAI/litellm/main/"
    "model_prices_and_context_window.json"
)
DEFAULT_TIMEOUT = 20.0

# 对照行里要展示的字段：键值来自 litellm 表
RATE_FIELDS = (
    ("input_cost_per_token", "输入"),
    ("output_cost_per_token", "输出"),
)


def load_table(path: str) -> dict:
    if not os.path.isfile(path):
        sys.stderr.write(
            "找不到 litellm 模型表：%s\n"
            "请确认 litellm 已安装在该 venv，或用 --table 指定表文件路径。\n" % path
        )
        raise SystemExit(2)
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fmt_int(value) -> str:
    if value is None:
        return "-"
    try:
        return "{:,}".format(int(value))
    except (TypeError, ValueError):
        return str(value)


def fmt_rate(per_token) -> str:
    """表内单价是「美元/单 token」，换算成「美元/百万 token」。"""
    if per_token is None:
        return "-"
    try:
        return "$%.4f" % (float(per_token) * 1_000_000)
    except (TypeError, ValueError):
        return str(per_token)


def fmt_bool(value) -> str:
    if value is None:
        return "-"
    return "是" if value else "否"


def cmd_providers(args) -> int:
    table = load_table(args.table)
    counts: dict[str, int] = {}
    for entry in table.values():
        if not isinstance(entry, dict):
            continue
        counts[entry.get("litellm_provider", "(无 provider 字段)")] = (
            counts.get(entry.get("litellm_provider", "(无 provider 字段)"), 0) + 1
        )
    rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    if args.top:
        rows = rows[: args.top]

    if args.json:
        print(json.dumps({
            "table": args.table,
            "total_entries": len(table),
            "provider_count": len(counts),
            "providers": dict(rows),
        }, ensure_ascii=False, indent=2))
        return 0

    print("litellm 模型表：%s" % args.table)
    print("总条目：%d 条；provider 数：%d 个" % (len(table), len(counts)))
    print("-" * 40)
    print("%-32s %s" % ("litellm_provider", "条目数"))
    print("-" * 40)
    for name, cnt in rows:
        print("%-32s %d" % (name, cnt))
    return 0


def entry_to_row(model_id: str, entry: dict | None) -> dict:
    if not isinstance(entry, dict):
        return {"model_id": model_id, "found": False}
    rates = {}
    for key, label in RATE_FIELDS:
        rates[label] = fmt_rate(entry.get(key))
    return {
        "model_id": model_id,
        "found": True,
        "provider": entry.get("litellm_provider", "-"),
        "mode": entry.get("mode", "-"),
        "max_input_tokens": entry.get("max_input_tokens"),
        "max_output_tokens": entry.get("max_output_tokens", entry.get("max_tokens")),
        "input_per_million": rates["输入"],
        "output_per_million": rates["输出"],
        "supports_function_calling": entry.get("supports_function_calling"),
        "supports_native_streaming": entry.get("supports_native_streaming"),
        "supports_prompt_caching": entry.get("supports_prompt_caching"),
        "source": entry.get("source", "-"),
    }


def print_rows(rows: list[dict]) -> None:
    for row in rows:
        if not row["found"]:
            print("[表内未收录] %s —— 需另行核实（不得据本表臆断规格）" % row["model_id"])
            continue
        print("模型 id          : %s" % row["model_id"])
        print("  provider       : %s（mode=%s）" % (row["provider"], row["mode"]))
        print("  最大输入 tokens: %s" % fmt_int(row["max_input_tokens"]))
        print("  最大输出 tokens: %s" % fmt_int(row["max_output_tokens"]))
        print("  输入单价       : %s / 百万 token" % row["input_per_million"])
        print("  输出单价       : %s / 百万 token" % row["output_per_million"])
        print("  函数调用       : %s" % fmt_bool(row["supports_function_calling"]))
        print("  原生流式       : %s" % fmt_bool(row["supports_native_streaming"]))
        print("  prompt 缓存    : %s" % fmt_bool(row["supports_prompt_caching"]))
        print("  表内 source    : %s" % row["source"])
        print("")


def cmd_models(args) -> int:
    table = load_table(args.table)
    rows = [entry_to_row(mid, table.get(mid)) for mid in args.model_ids]
    if args.json:
        print(json.dumps({"table": args.table, "models": rows}, ensure_ascii=False, indent=2))
        return 0
    print("litellm 模型表：%s" % args.table)
    print("=" * 60)
    print_rows(rows)
    print("提示：上面单价读的是本地表，只作量级参考；要实时价请跑 "
          "`python scripts/model_catalog.py price <id>`。")
    missing = [r["model_id"] for r in rows if not r["found"]]
    if missing:
        print("未收录 %d 个：%s" % (len(missing), ", ".join(missing)))
    return 0


def cmd_search(args) -> int:
    table = load_table(args.table)
    kw = args.keyword.lower()
    hits = [
        entry_to_row(mid, entry)
        for mid, entry in table.items()
        if isinstance(entry, dict) and kw in mid.lower()
    ]
    hits.sort(key=lambda r: r["model_id"])
    total = len(hits)
    if args.limit:
        hits = hits[: args.limit]
    if args.json:
        print(json.dumps({"table": args.table, "keyword": args.keyword,
                          "total_matches": total, "models": hits},
                         ensure_ascii=False, indent=2))
        return 0
    print("匹配到 %d 条（关键词 %s，显示前 %d 条）：" % (total, args.keyword, len(hits)))
    print("-" * 100)
    print("%-52s %-14s %12s %12s %13s %13s" %
          ("model id", "provider", "max_in", "max_out", "输入$/1M", "输出$/1M"))
    print("-" * 100)
    for r in hits:
        print("%-52s %-14s %12s %12s %13s %13s" % (
            r["model_id"][:52], str(r["provider"])[:14],
            fmt_int(r["max_input_tokens"]), fmt_int(r["max_output_tokens"]),
            r["input_per_million"], r["output_per_million"],
        ))
    return 0


def now_text() -> str:
    return datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")


def mtime_text(path: str) -> str:
    if not os.path.isfile(path):
        return "-"
    return datetime.datetime.fromtimestamp(os.path.getmtime(path)).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %z"
    )


def fetch_remote_table(url: str, timeout: float, proxy: str | None) -> tuple[dict | None, str]:
    """拉远端表，返回 (表 或 None, 失败原因)。任何网络/解析异常都转成失败原因，不向上抛。"""
    import urllib.request

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "ai-pm-agent/model_catalog.py", "Accept": "application/json"},
    )
    # 不传 --proxy 时不加 ProxyHandler，交由 urllib 默认行为读环境变量里的代理
    handlers = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=timeout) as resp:
            raw = resp.read()
    except Exception as exc:  # URLError / HTTPError / timeout / SSL 等一律转成回退
        return None, "%s: %s" % (type(exc).__name__, exc)
    try:
        table = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return None, "远端返回内容不是合法 JSON（%d 字节）：%s" % (len(raw), exc)
    if not isinstance(table, dict) or not table:
        return None, "远端返回内容不是非空 JSON 对象（%d 字节）" % len(raw)
    return table, ""


def entry_has_price(entry: dict) -> bool:
    return any(entry.get(key) is not None for key, _ in RATE_FIELDS)


def cmd_price(args) -> int:
    """实时查价：远端优先，失败回退本地备份并标注可能过期。"""
    remote_error = ""
    table = None
    if args.offline:
        remote_error = "本次带 --offline，按用户要求跳过远端取数"
    else:
        table, remote_error = fetch_remote_table(args.remote_url, args.timeout, args.proxy)

    if table is not None:
        info = {
            "source": "remote",
            "source_label": "远端 litellm 官方表（实时）",
            "source_detail": args.remote_url,
            "backup_mtime": mtime_text(args.table),
            "stale_warning": "",
            "remote_error": "",
        }
    else:
        # 本地备份缺失时 load_table 会报错退出：远端与本地都取不到，无源可退，属于真实故障
        table = load_table(args.table)
        info = {
            "source": "local_backup",
            "source_label": "本地备份（可能已过期）",
            "source_detail": args.table,
            "backup_mtime": mtime_text(args.table),
            "stale_warning": "用的是本地备份，可能已过期",
            "remote_error": remote_error,
        }
    info["fetched_at"] = now_text()
    info["remote_url"] = args.remote_url
    info["table_entries"] = len(table)

    rows = [entry_to_row(mid, table.get(mid)) for mid in args.model_ids]
    info["models"] = rows

    if args.json:
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0

    if info["remote_error"]:
        if args.offline:
            print("[提示] %s" % info["remote_error"])
        else:
            print("[警告] 远端取数失败：%s" % info["remote_error"])
    print("取数来源：%s" % info["source_label"])
    print("来源地址：%s" % info["source_detail"])
    if info["stale_warning"]:
        print("备份写入：%s" % info["backup_mtime"])
    print("取数时间：%s" % info["fetched_at"])
    print("表内条目：%d 条" % info["table_entries"])
    if info["stale_warning"]:
        print("注意    ：%s；要最新价请恢复网络后重跑本命令。" % info["stale_warning"])
    print("=" * 60)
    for row in rows:
        if not row["found"]:
            print("[表内未收录] %s —— 需另行核实（不得据本表臆断其规格）" % row["model_id"])
            print("")
            continue
        entry = table[row["model_id"]]
        print("模型 id          : %s" % row["model_id"])
        print("  provider       : %s（mode=%s）" % (row["provider"], row["mode"]))
        print("  输入单价       : %s / 百万 token" % row["input_per_million"])
        print("  输出单价       : %s / 百万 token" % row["output_per_million"])
        print("  最大输入 tokens: %s" % fmt_int(row["max_input_tokens"]))
        print("  最大输出 tokens: %s" % fmt_int(row["max_output_tokens"]))
        print("  函数调用       : %s" % fmt_bool(row["supports_function_calling"]))
        if not entry_has_price(entry):
            print("  [缺价] 本表内无 input/output 单价，需另行核实，不得用同类模型价格替估。")
        print("")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="litellm 模型表查询（零模型调用；只有 price 子命令联网）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：python scripts/model_catalog.py providers | models deepseek-chat | "
               "search doubao | price deepseek-chat",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_prov = sub.add_parser("providers", help="列 provider 与条目计数")
    p_prov.add_argument("--table", default=DEFAULT_TABLE, help="表文件路径")
    p_prov.add_argument("--top", type=int, default=0, help="只显示前 N 名（0=全部）")
    p_prov.add_argument("--json", action="store_true", help="输出 JSON")
    p_prov.set_defaults(func=cmd_providers)

    p_mod = sub.add_parser("models", help="按模型 id 输出对照行")
    p_mod.add_argument("model_ids", nargs="+", help="litellm 模型 id，可多个")
    p_mod.add_argument("--table", default=DEFAULT_TABLE, help="表文件路径")
    p_mod.add_argument("--json", action="store_true", help="输出 JSON")
    p_mod.set_defaults(func=cmd_models)

    p_srch = sub.add_parser("search", help="按关键词搜模型 id")
    p_srch.add_argument("keyword", help="关键词，如 doubao、zai、qwen")
    p_srch.add_argument("--table", default=DEFAULT_TABLE, help="表文件路径")
    p_srch.add_argument("--limit", type=int, default=40, help="最多显示条数（0=不限）")
    p_srch.add_argument("--json", action="store_true", help="输出 JSON")
    p_srch.set_defaults(func=cmd_search)

    p_price = sub.add_parser("price", help="实时查价（远端优先，失败回退本地备份并标注可能过期）")
    p_price.add_argument("model_ids", nargs="+", help="litellm 模型 id，可多个")
    p_price.add_argument("--remote-url", default=DEFAULT_REMOTE_URL, help="远端表地址（默认 litellm 官方 raw 表）")
    p_price.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="远端取数超时秒数（默认 20）")
    p_price.add_argument("--proxy", default=None, help="显式代理，如 http://127.0.0.1:7892；不传则用环境变量")
    p_price.add_argument("--offline", action="store_true", help="跳过远端，直接读本地备份（用于无网环境）")
    p_price.add_argument("--table", default=DEFAULT_TABLE, help="本地备份表路径（远端失败时的回退源）")
    p_price.add_argument("--json", action="store_true", help="输出 JSON")
    p_price.set_defaults(func=cmd_price)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 控制台默认 GBK，避免中文输出报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
