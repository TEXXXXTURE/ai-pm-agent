# 网页抓取工具
"""page_fetch.py — 网页正文抓取 CLI（httpx + BeautifulSoup）。

用途：Pi 在需求调研时，根据 URL 抓取网页正文（去标签后纯文本），用于消化网页内容。

用法：
    python scripts/page_fetch.py --url "https://example.com"
    python scripts/page_fetch.py --url "https://example.com" --max-chars 6000

参数：
    --url        网址（必填）
    --max-chars  正文最大字符数，默认 4000

输出：JSON 到 stdout：
    {"url": ..., "title": ..., "content": ...}
    content 截断到 max_chars

异常：
    - 抓取失败：输出 {"error": "...", "url": ...} 到 stderr，退出 1

依赖：标准库 + httpx + beautifulsoup4（项目 requirements.txt 已配）。
说明：走 run-tool.sh 跑时会自动清空代理；若直接 python 调用，请自行清空代理环境变量。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

import httpx
from bs4 import BeautifulSoup

_TIMEOUT = 15.0  # 秒
_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 " \
              "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _extract_text(html: str) -> tuple[str, str]:
    """从 HTML 提取 (title, 正文纯文本)。

    正文策略：优先 <main> / <article>；回退到 <body>。
    去掉 <script> / <style> / <noscript> / <head> 等干扰标签后取 get_text。
    """
    soup = BeautifulSoup(html, "html.parser")
    title = soup.title.get_text(strip=True) if soup.title else ""

    for tag_name in ("script", "style", "noscript", "iframe", "svg", "head"):
        for tag in soup.find_all(tag_name):
            tag.decompose()

    root = soup.find("main") or soup.find("article") or soup.body or soup
    text = root.get_text(separator="\n", strip=True) if root else ""
    # 合并多余空行
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return title, "\n".join(lines)


def fetch(url: str, max_chars: int = 4000) -> dict[str, Any]:
    """抓取单个 URL，返回 {url, title, content}。"""
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    }
    # follow_redirects=True：很多站点会 301/302；verify 不动，默认校验证书
    with httpx.Client(timeout=_TIMEOUT, follow_redirects=True) as client:
        resp = client.get(url, headers=headers)
        resp.raise_for_status()
    # httpx 会自动用 charset 解码；若未给出则回退 utf-8
    if resp.encoding is None:
        resp.encoding = "utf-8"
    html = resp.text
    title, content = _extract_text(html)
    if max_chars > 0 and len(content) > max_chars:
        content = content[:max_chars]
    return {"url": url, "title": title, "content": content}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="page_fetch.py",
        description="网页正文抓取 CLI（httpx + BeautifulSoup）",
    )
    parser.add_argument("--url", required=True, help="网址（必填）")
    parser.add_argument(
        "--max-chars", type=int, default=4000, help="正文最大字符数（默认 4000）"
    )
    args = parser.parse_args(argv)
    if args.max_chars < 0:
        parser.error("--max-chars 不能为负")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        result = fetch(args.url, max_chars=args.max_chars)
    except Exception as exc:
        print(
            json.dumps({"error": str(exc), "url": args.url}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
