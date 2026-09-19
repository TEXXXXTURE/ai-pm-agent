# 联网搜索工具（DuckDuckGo HTML）
"""web_search.py — 联网搜索 CLI（基于 DuckDuckGo HTML 接口，零 API key）。

用途：Pi 在需求调研时，用关键词联网检索资料，返回标题/URL/摘要列表。

用法：
    python scripts/web_search.py --query "AI 产品经理"
    python scripts/web_search.py --query "用户访谈方法" --top-k 8

参数：
    --query   查询词（必填）
    --top-k   返回条数，默认 5

输出：JSON 数组到 stdout，每项：
    {"title": ..., "url": ..., "snippet": ...}

异常：
    - 网络失败：输出 {"error": "...", "query": ...} 到 stderr，退出 1
    - 无结果：输出 [] 到 stdout，退出 0

依赖：仅标准库（urllib.request / urllib.parse / re / json / argparse）。
说明：走 run-tool.sh 跑时会自动清空代理；若直接 python 调用，请自行清空代理环境变量。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.parse
import urllib.request

# DuckDuckGo HTML 接口
_DDGO_HTML_URL = "https://html.duckduckgo.com/html/"
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
_TIMEOUT = 10  # 秒

# 结果项锚点：DuckDuckGo html 版结果标题在 <a class="result__a" href="...">标题</a>
# href 形如 "//duckduckgo.com/l/?uddg=<urlencoded real url>&rut=..." 或 "?uddg=..."
_RESULT_A_RE = re.compile(
    r'<a\s+[^>]*class="[^"]*result__a[^"]*"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
    re.IGNORECASE | re.DOTALL,
)
# 摘要：result__snippet（<a class="result__snippet"> 或 <td class="result__snippet">）
_SNIPPET_RE = re.compile(
    r'<(?:a|td)\s+[^>]*class="[^"]*result__snippet[^"]*"[^>]*>(.*?)</(?:a|td)>',
    re.IGNORECASE | re.DOTALL,
)
_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    """去标签 + HTML 实体解码（常见实体）。"""
    s = _TAG_RE.sub("", s)
    s = (s.replace("&amp;", "&")
          .replace("&lt;", "<")
          .replace("&gt;", ">")
          .replace("&quot;", '"')
          .replace("&#39;", "'")
          .replace("&nbsp;", " "))
    return urllib.parse.unquote(s)


def _extract_real_url(href: str) -> str:
    """从 DuckDuckGo 跳转链接中提取真实 URL。

    href 形如：
      //duckduckgo.com/l/?uddg=https%3A//example.com&rut=...
      /l/?uddg=https%3A//example.com
      https://duckduckgo.com/l/?uddg=...
    若无 uddg 参数，则返回原始 href（去掉前导 //）。
    """
    if not href:
        return ""
    # 取 query 部分
    q_idx = href.find("?")
    qs = href[q_idx + 1:] if q_idx >= 0 else href
    for pair in qs.split("&"):
        if pair.lower().startswith("uddg="):
            return urllib.parse.unquote(pair[5:])
    # 无 uddg：剥掉前导 //
    if href.startswith("//"):
        return "https:" + href
    return href


def _parse_results(html: str, top_k: int) -> list[dict]:
    """从 DuckDuckGo HTML 中解析搜索结果。"""
    titles = _RESULT_A_RE.findall(html)
    snippets = _SNIPPET_RE.findall(html)

    # DuckDuckGo 页面里 result__a 与 result__snippet 顺序一一对应，
    # 但稳健起见按序号取（取最小者长度）
    out: list[dict] = []
    n = len(titles)
    for i in range(n):
        if len(out) >= top_k:
            break
        raw_href, raw_title = titles[i]
        url = _extract_real_url(raw_href.strip())
        title = _strip_html(raw_title).strip()
        if not title and not url:
            continue
        snippet = _strip_html(snippets[i]).strip() if i < len(snippets) else ""
        out.append({"title": title, "url": url, "snippet": snippet})
    return out


def search(query: str, top_k: int = 5) -> list[dict]:
    """执行一次 DuckDuckGo HTML 搜索，返回结果列表。"""
    url = f"{_DDGO_HTML_URL}?q={urllib.parse.quote_plus(query)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": _USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
        raw = resp.read()
    # 备选编码：HTTP 头优先，失败回退 utf-8
    charset = resp.headers.get_content_charset() or "utf-8"
    try:
        html = raw.decode(charset, errors="replace")
    except LookupError:
        html = raw.decode("utf-8", errors="replace")
    return _parse_results(html, top_k)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="web_search.py",
        description="联网搜索 CLI（DuckDuckGo HTML 接口，零 API key）",
    )
    parser.add_argument("--query", required=True, help="查询词（必填）")
    parser.add_argument(
        "--top-k", type=int, default=5, help="返回条数（默认 5）"
    )
    args = parser.parse_args(argv)
    if args.top_k <= 0:
        parser.error("--top-k 必须为正整数")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        results = search(args.query, top_k=args.top_k)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc), "query": args.query}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
