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


def _split_point(text: str, start: int, limit: int) -> int:
    """在 [start, start+limit] 窗口里找一个尽量靠后的安全断点，返回绝对下标。

    优先级：换行 > 句末（。！？!?.；;）> 空格 > 制表符。都找不到才退回硬位置
    （start + limit）。目的：不要把词、句子从中间砍断。
    """
    end = min(start + limit, len(text))
    if end >= len(text):
        return len(text)
    window = text[start:end]
    for marker in ("\n", "。", "！", "？", "!", "?", ".", ";", "；", "\t", " "):
        idx = window.rfind(marker)
        if idx > 0:
            return start + idx + 1
    return end


def _hard_split_by_chars(text: str) -> list[str]:
    """无换行的长串按字符切片，但优先在词句边界断。

    每片至多 MAX_CHUNK_CHARS，相邻片重叠 OVERLAP_CHARS。
    """
    pieces: list[str] = []
    pos = 0
    while pos < len(text):
        cut = _split_point(text, pos, MAX_CHUNK_CHARS)
        pieces.append(text[pos:cut])
        if cut >= len(text):
            break
        # 退 OVERLAP_CHARS 作重叠，但**退到行边界**（没有换行才退到空格），
        # 否则重叠本身又切出半截行
        seg_start = max(pos, cut - OVERLAP_CHARS)
        nl = text.rfind("\n", seg_start, cut)
        if nl >= 0:
            nxt = nl + 1
        else:
            sp = text.rfind(" ", seg_start, cut)
            nxt = sp + 1 if sp >= 0 else cut
        if nxt <= pos:
            nxt = cut
        pos = nxt
    return pieces or [text]


def _tail_overlap(text: str, limit: int) -> str:
    """取尾部若干「完整行」作重叠，总长不超过 limit。

    只取整行，是为了重叠本身不再切出半截行——否则后一片开头又是「ion with guardrails |」。
    """
    if limit <= 0:
        return ""
    kept: list[str] = []
    used = 0
    for line in reversed(str(text).split("\n")):
        add = len(line) + (1 if kept else 0)
        if used + add > limit:
            break
        kept.insert(0, line)
        used += add
    return "\n".join(kept)


def _hard_split_paragraph(paragraph: str) -> list[str]:
    """单段超长时切片：**优先在换行处断**，保住表格行、列表项、代码行的完整。

    为什么不能直接按字符位置砍：Markdown 表格的行与行之间没有空行，整个表在上一层
    眼里就是「一整段」；按固定字符位置切，刀会落在表格行中间，切出「ion with
    guardrails |」这种半截行——检索回来没有列头，读者不知道这行在说谁。

    规则：
    - 逐行累积到 MAX_CHUNK_CHARS 就落一片（切点永远落在换行处）；
    - 重叠取上一片尾部的「完整行」，跨片内容不丢；
    - 只有单行本身就超长且无换行时，才退回按字符位置硬切。
    """
    lines = str(paragraph).split("\n")
    pieces: list[str] = []
    buf = ""
    for line in lines:
        if len(line) > MAX_CHUNK_CHARS:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(_hard_split_by_chars(line))
            continue
        candidate = line if not buf else f"{buf}\n{line}"
        if len(candidate) <= MAX_CHUNK_CHARS:
            buf = candidate
            continue
        pieces.append(buf)
        tail = _tail_overlap(buf, OVERLAP_CHARS)
        buf = f"{tail}\n{line}" if tail else line
    if buf:
        pieces.append(buf)
    return pieces or [str(paragraph)]


def _split_long_block(block: str) -> list[str]:
    """把超长基础块按空行段落二次切分：目标 500-800 字符、相邻块重叠 100 字符。"""
    paragraphs: list[str] = []
    hard_split_pieces: set[str] = set()
    for raw in _BLANK_LINE_RE.split(block):
        para = raw.strip()
        if not para:
            continue
        if len(para) > MAX_CHUNK_CHARS:
            pieces = _hard_split_paragraph(para)
            paragraphs.extend(pieces)
            hard_split_pieces.update(pieces)
        else:
            paragraphs.append(para)

    chunks: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(buf)
            buf = ""

    for para in paragraphs:
        # 超长段的切片保持原子：不再与相邻段落合并，
        # 否则合并会在表格中间插入空行，把表拆散
        if para in hard_split_pieces:
            _flush()
            chunks.append(para)
            continue
        if not buf:
            buf = para
            continue
        if len(buf) + 2 + len(para) <= MAX_CHUNK_CHARS:
            buf = f"{buf}\n\n{para}"
            continue
        # 放不下：先落盘当前块，再用其尾部作重叠，拼上本段开新块。
        # 重叠取「完整行」——按字符截会切出半截表格行（`ation |`）
        chunks.append(buf)
        room = MAX_CHUNK_CHARS - len(para) - 2
        if room > 0:
            tail = _tail_overlap(buf, min(OVERLAP_CHARS, room))
            buf = f"{tail}\n\n{para}" if tail else para
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
        # 标题下面没写内容时，会切出「只有标题行」的空壳块（如 `### Key Features`）。
        # 这种块检索回来占名额、不给信息，直接不入库。
        non_empty_lines = [ln for ln in content.split("\n") if ln.strip()]
        if len(non_empty_lines) == 1 and non_empty_lines[0].lstrip().startswith("#"):
            return
        # 只有 markdown 分隔线（--- / *** / ___）的块同样不入库：纯噪声，
        # 而且会因为「字符量大」在检索里拿到高分（曾是查询第一名）
        if all(re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", ln.strip()) for ln in non_empty_lines):
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


