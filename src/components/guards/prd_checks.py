# 组件框架 - PRD 自检 guards
# 纵切联调 - 适配 sections 片段结构（title + body_html）
"""PRD 产物自检函数：技术术语检查 / 章节数检查 / HTML 片段标签检查。

每个 guard 签名统一：(result: dict) -> tuple[bool, str]，返回 (passed, feedback)。
"""
from __future__ import annotations

import re

# 技术接口表述关键词（大小写不敏感匹配）
TECH_TERMS = ("API", "字段", "错误码", "数据库", "HTTP", "接口", "endpoint")

# HTML 结构片段标签（开标签，忽略属性）
_HTML_TAG_RE = re.compile(
    r"<(p|ul|ol|li|h[1-6]|table|thead|tbody|tfoot|tr|td|th|dl|dt|dd|"
    r"div|section|article|span|strong|em|b|i|u|br|hr|img|a|"
    r"figure|figcaption|blockquote|pre|code)\b",
    re.IGNORECASE,
)


def _section_text(result: dict) -> str:
    """拼接所有 section 的 title + body_html。"""
    parts: list[str] = []
    for section in result.get("sections", []) or []:
        if isinstance(section, dict):
            parts.append(str(section.get("title", "")))
            parts.append(str(section.get("body_html", "")))
    return "\n".join(parts)


def no_tech_jargon(result: dict) -> tuple[bool, str]:
    """检查所有章节文本中是否包含技术接口表述。"""
    text = _section_text(result)
    text_lower = text.lower()
    found = [term for term in TECH_TERMS if term.lower() in text_lower]
    if found:
        return False, f"发现技术接口表述: {', '.join(found)}"
    return True, ""


def required_blocks_present(result: dict) -> tuple[bool, str]:
    """检查 sections 数量是否至少 3 个。"""
    count = len(result.get("sections", []) or [])
    if count < 3:
        return False, f"章节不足（当前 {count} 个章节，需要至少 3 个）"
    return True, ""


def html_self_contained(result: dict) -> tuple[bool, str]:
    """片段标签检查（自包含外壳由 Jinja2 模板保证）。

    检查每个 section 的 body_html 是否含至少一个 HTML 结构标签
    （<p>/<ul>/<h3>/<table>/<ol>/<li>/<div> 等）；
    PRD 正文是 HTML 片段，<html>/<head>/<body> 外壳由 prd.html.j2 模板统一提供。
    """
    sections = result.get("sections", []) or []
    for section in sections:
        body = str(section.get("body_html", "")) if isinstance(section, dict) else ""
        if not _HTML_TAG_RE.search(body):
            return False, "章节正文缺少 HTML 结构标签"
    return True, ""


