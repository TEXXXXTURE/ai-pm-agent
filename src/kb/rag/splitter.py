# RAG 子库 - 分块器（概念层按 Markdown 标题切 / 论文整篇一块）
"""split_document：把一篇文档切成带元数据的 chunk 列表。

分块策略：
- doc_type="concept"：先剥离 YAML front matter（开头 --- 到下一个 --- 的块）；
  再按 Markdown 1-3 级标题行切分，每个标题块为一个基础 chunk，该块标题文本记入
  metadata.section_title（标题之前的引言部分 section_title 记空串）；
  单个基础 chunk 超 800 字符时按空行段落二次切分，目标 500-800 字符、相邻块重叠 100 字符。
- doc_type="paper"：整篇一个 chunk，section_title 为空串。
- 其余 doc_type 按整篇一块处理（防御性兜底）。

返回列表，每项 {"content": str, "metadata": {**metadata, "chunk_index": int,
"section_title": str}}；chunk_index 从 0 递增。
"""
from __future__ import annotations

import re

# 单个基础 chunk 的最大字符数（超过则二次切分）
MAX_CHUNK_CHARS = 800
# 二次切分时相邻块的重叠字符数
OVERLAP_CHARS = 100

# Markdown 1-3 级标题行（# / ## / ### 后须空格或行尾；#### 及更深不视为标题）
_HEADING_RE = re.compile(r"^(#{1,3})(?:\s+(.*))?\s*$")
# 空行段落分隔（一个或多个空行）
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
# ATX 标题行尾的闭合 # 号
_TRAILING_HASH_RE = re.compile(r"\s*#+\s*$")


def _strip_front_matter(text: str) -> str:
    """剥离文首 YAML front matter；无 front matter 或未闭合时原样返回。"""
    lines = str(text).splitlines()
    if not lines or lines[0].lstrip("\ufeff").strip() != "---":
        return text
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            return "\n".join(lines[i + 1 :])
    return text


def _clean_title(raw: str) -> str:
    """标题文本去首尾空白与闭合 # 号。"""
    return _TRAILING_HASH_RE.sub("", raw or "").strip()


def _split_by_headings(body: str) -> list[tuple[str, str]]:
    """按 1-3 级标题行切分，返回 [(section_title, 块正文)]，正文含该标题行。"""
    blocks: list[tuple[str, str]] = []
    title = ""
    buf: list[str] = []
    started = False

    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            if started or buf:
                blocks.append((title, "\n".join(buf).strip()))
            title = _clean_title(match.group(2))
            buf = [line.strip()]
            started = True
        else:
            buf.append(line)

    if started or buf:
        blocks.append((title, "\n".join(buf).strip()))
    return blocks


def _hard_split_paragraph(paragraph: str) -> list[str]:
    """单段超长时硬切：每片至多 800 字符，片间重叠 100 字符。"""
    step = MAX_CHUNK_CHARS - OVERLAP_CHARS
    return [
        paragraph[i : i + MAX_CHUNK_CHARS]
        for i in range(0, len(paragraph), step)
    ]


def _split_long_block(block: str) -> list[str]:
    """把超长基础块按空行段落二次切分：目标 500-800 字符、相邻块重叠 100 字符。"""
    paragraphs: list[str] = []
    for raw in _BLANK_LINE_RE.split(block):
        para = raw.strip()
        if not para:
            continue
        if len(para) > MAX_CHUNK_CHARS:
            paragraphs.extend(_hard_split_paragraph(para))
        else:
            paragraphs.append(para)

    chunks: list[str] = []
    buf = ""
    for para in paragraphs:
        if not buf:
            buf = para
            continue
        if len(buf) + 2 + len(para) <= MAX_CHUNK_CHARS:
            buf = f"{buf}\n\n{para}"
            continue
        # 放不下：先落盘当前块，再用其尾部 100 字符作重叠，拼上本段开新块
        chunks.append(buf)
        room = MAX_CHUNK_CHARS - len(para) - 2
        if room > 0:
            tail = buf[-min(OVERLAP_CHARS, room) :]
            buf = f"{tail}\n\n{para}"
        else:
            buf = para
    if buf:
        chunks.append(buf)
    return chunks


def split_document(text: str, doc_type: str, metadata: dict) -> list[dict]:
    """把文档切分为 chunk 列表。

    Args:
        text: 文档全文。
        doc_type: "concept"（概念层，按标题切）| "paper"（论文层，整篇一块）。
        metadata: 附加到每个 chunk 的元数据（如 layer / category / doc_id）。

    Returns:
        [{"content": str, "metadata": {...含 chunk_index / section_title}}}]
    """
    base_meta = dict(metadata or {})
    chunks: list[dict] = []

    def _push(content: str, section_title: str) -> None:
        content = content.strip()
        if not content:
            return
        chunks.append(
            {
                "content": content,
                "metadata": {
                    **base_meta,
                    "chunk_index": len(chunks),
                    "section_title": section_title,
                },
            }
        )

    if doc_type == "concept":
        body = _strip_front_matter(text)
        for title, block in _split_by_headings(body):
            if len(block) > MAX_CHUNK_CHARS:
                for piece in _split_long_block(block):
                    _push(piece, title)
            else:
                _push(block, title)
    else:
        # paper 及未知类型：整篇一块，语义完整不切
        _push(str(text), "")

    return chunks


