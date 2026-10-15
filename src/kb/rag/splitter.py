# RAG 子库 - 分块器（v4 切片规则：断点优先级 + 条件句保护 + Contextual Header + 父子块）
"""split_document：把一篇文档切成带元数据的 chunk 列表（v4 切片规则）。

切片规则（对应选型笔记「第三轮定调」、实施规划 5.2 / 4.3）：
- 断点优先级：标题 > 段落 > 约束词（句末）> 字数上限。具体：
  1) 标题：按 1-3 级标题切基础块（父块）；
  2) 段落：超长块按空行段落打包二次切分（子块），段落整体放不下才往下一级；
  3) 约束词：单个段落放不下时按「原子单元」切——约束词表把条件句与其后至少一个
     句子绑定为一个原子单元，条件句与结论绝不切断。词表两套（v4 起）：中文表
     （必须 / 不得 / 仅当 / 前提 / 假设 / 适用 / 不适用 / 除非 / 如果 / 若 / 凡是 /
     只有 / 条件 / 要求 / 禁止）按子串匹配；英文表（if / unless / only if / must /
     shall / must not / may not / except / provided that / in case / whenever /
     prerequisite / requires / assumption / only when / provided）按整词匹配
     （大小写不敏感）。英文表是 v4 新增——知识库语料多为英文技术文档，原表全中文、
     从未命中；英文若不按整词匹配，if 会误伤 verify / specific / modify、must 会误伤
     mustering。英文句子靠半角句点收尾，故 v4 起句末标点集也纳入 "."（否则整段英文
     缩成一个句子，条件句判定只能退到段落粒度）；
  4) 字数上限（MAX_CHUNK_CHARS=800）只在前三种都放不下时兜底硬切。
- 「条件+结论」原子单元自身 ≤ ATOMIC_UNIT_HARD_LIMIT（1200 字符）时整体成块
  （宁可块只有 150 字也不劈开条件与结论）；只有超过 1200 字符才允许内部硬切
  （最后兜底）。字数上限按「块正文 + Contextual Header」总长计。
- Contextual Header：每块 content 前缀拼一行 `[领域] > [章节] > [条目]`（三段都带方
  括号），空一行后才是正文；header 写进 content（参与嵌入与词法索引），不是只进
  metadata。取值：领域 = 文档 category（缺省「通用」）；章节 = 标题路径去掉末级后
  用 > 连接（与 heading_path 同形，如「一 > 二」），没有上级则用文档 title（再缺省
  「正文」）；条目 = 末级标题，没有则用「正文」。
  metadata 同时存 heading_path（完整标题路径用 > 连接）。
- 父子块：1-3 级标题切出的基础块，未超长时自身即可检索小节块（chunk_type=section，
  parent_id 为空串）；超长基础块先二次切出子块（chunk_type=section，parent_id 指向
  父块），再把整块作父块（chunk_type=parent）入库——子块在前，父块紧随其后（小块
  拿靠前的 chunk_index）。父子块都入库、都带 Contextual Header。paper 整篇一块
  （chunk_type=abstract）；
  未知 doc_type 整篇兜底一块（chunk_type=parent）。调用方在 metadata 里显式给出
  chunk_type 时按原值透传（统一 schema 4.3 的增量文档由入库器定 chunk_type）。
- overlap：结构化内容（表格行 / 代码块 / 列表行）相邻子块重叠为 0；叙事长文相邻
  子块重叠约 10%（上限 OVERLAP_CHARS=100），只取完整行，绝不切出半行。
- 保护逻辑（v2 保留）：front matter 剥离、只有标题行的空壳块剔除、纯分隔线块剔除、
  切点优先落换行与句末、表格行不切半。
- 条件句保护在同一基础块内生效；跨标题块不粘连（断点优先级里标题高于约束词）。

块 ID 规则（沿用现行实现，splitter 本身不产 ID）：入库器拿 chunk_index 拼 ID——
  概念层 `concept-{source_hash}-{chunk_index}`；论文层 `{prefix}-{arxiv_id}`（单块时）
  或 `{prefix}-{arxiv_id}-{chunk_index}`；增量层
  `{doc_id}:{strategy_version}:{chunk_index}`。子块 parent_id 存父块的 chunk_index
  （十进制字符串）：把上述 ID 规则里的 chunk_index 换成 parent_id，即还原父块 ID。

返回列表，每项 {"content": str, "metadata": {**metadata, "chunk_index": int,
"section_title": str, "heading_path": str, "parent_id": str, "chunk_type": str}}；
chunk_index 全文档连续递增（父子块都算）。切片是单文档纯函数：同输入同参数
两次运行输出完全一致。
"""
from __future__ import annotations

import re

# 单块正文的目标上限（块正文 + Contextual Header 总长不超过 MAX_CHUNK_CHARS）
MAX_CHUNK_CHARS = 800
# 叙事长文二次切分时相邻子块的重叠上限（约 10%，且只取完整行）
OVERLAP_CHARS = 100
# 「条件+结论」原子单元允许内部硬切的最小长度：超过它才允许最后兜底硬切
ATOMIC_UNIT_HARD_LIMIT = 1200

# 中文约束词表：命中任一词的句子视为条件句，必须与其后至少一个句子落在同一个块
# （子串匹配，宁多勿漏——中文没有词缀误命中问题）
CONSTRAINT_WORDS = (
    "必须", "不得", "仅当", "前提", "假设", "适用", "不适用",
    "除非", "如果", "若", "凡是", "只有", "条件", "要求", "禁止",
)

# 英文约束词表（v4 新增）：知识库语料多为英文技术文档，原表全中文、从未命中。
# 英文按「整词」匹配（大小写不敏感）：照搬中文那种子串匹配会误伤一大片——
# if 命中 verify / specific / modify，must 命中 mustering，所以走 \b 词边界。
CONSTRAINT_WORDS_EN = (
    "if", "unless", "only if", "must", "shall", "must not", "may not",
    "except", "provided that", "in case", "whenever", "prerequisite",
    "requires", "assumption", "only when", "provided",
)

# 英文约束词整词正则：\b 边界保证 must 不命中 mustering、if 不命中 verify
_EN_CONSTRAINT_RE = re.compile(
    r"\b(?:%s)\b" % "|".join(re.escape(word) for word in CONSTRAINT_WORDS_EN),
    re.IGNORECASE,
)

# Markdown 1-3 级标题行（# / ## / ### 后须空格或行尾；#### 及更深不视为标题）
_HEADING_RE = re.compile(r"^(#{1,3})(?:\s+(.*))?\s*$")
# 空行段落分隔（一个或多个空行）
_BLANK_LINE_RE = re.compile(r"\n\s*\n")
# ATX 标题行尾的闭合 # 号
_TRAILING_HASH_RE = re.compile(r"\s*#+\s*$")
# 句子切分：句末标点（。！？!?；;.）与其余文本交替，两段合并成一个句子区间。
# 半角句点 "." 是 v4 新增：英文句子靠它收尾，不认它则整段英文缩成一个「句子」，
# 条件句判定就只能退到段落粒度（条件句与其结论永远同段落，保护等于没生效）。
_SENTENCE_TOKEN_RE = re.compile(r"[^。！？!?；;.]+|[。！？!?；;.]+")
# 句末标点集（_sentence_spans 用它判定句子是否收尾，须与 _SENTENCE_TOKEN_RE 同步）
_SENTENCE_END_CHARS = "。！？!?；;."
# 结构化行：表格行 / 代码围栏行 / 列表行
_TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
_CODE_FENCE_RE = re.compile(r"^\s*(?:```|~~~)")
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.、])\s+\S")


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


# ── 结构化内容与条件句识别 ─────────────────────────────────────────


def _is_structured_line(line: str) -> bool:
    """是否结构化行：表格行 / 代码围栏行 / 列表行。"""
    return bool(
        _TABLE_ROW_RE.match(line)
        or _CODE_FENCE_RE.match(line)
        or _LIST_ITEM_RE.match(line)
    )


def _is_structured_text(text: str) -> bool:
    """文本里是否含结构化行（含结构化行即按结构化对待：重叠为 0）。"""
    return any(_is_structured_line(ln) for ln in str(text).split("\n") if ln.strip())


def _is_condition_sentence(sentence: str) -> bool:
    """句子是否条件句：命中中文约束词（子串匹配，宁多勿漏）或英文约束词（整词匹配）。

    英文走 _EN_CONSTRAINT_RE 的 \\b 词边界 + 忽略大小写：MUST / must 都算，
    但 mustering、verify、specific 这类词缀不命中。
    """
    text = str(sentence)
    if any(word in text for word in CONSTRAINT_WORDS):
        return True
    return bool(_EN_CONSTRAINT_RE.search(text))


def _sentence_spans(text: str) -> list[tuple[int, int]]:
    """按句末标点切句子区间（左闭右开，首尾相接、无重叠无遗漏）。

    句内换行、空白原样留在区间里，拼回原文一字不差。
    """
    spans: list[tuple[int, int]] = []
    pending: tuple[int, int] | None = None
    for match in _SENTENCE_TOKEN_RE.finditer(str(text)):
        start, end = match.span()
        pending = (start, end) if pending is None else (pending[0], end)
        if text[end - 1] in _SENTENCE_END_CHARS:
            spans.append(pending)
            pending = None
    if pending is not None:
        spans.append(pending)
    return spans


def _unit_is_condition(unit_text: str) -> bool:
    """原子单元的末句是否条件句（判断绑定要不要继续往下吞）。"""
    spans = _sentence_spans(str(unit_text))
    if not spans:
        return False
    start, end = spans[-1]
    return _is_condition_sentence(unit_text[start:end])


def _paragraph_units(text: str) -> list[tuple[str, bool]]:
    """把文本切成原子单元 [(单元文本, 是否条件绑定单元)]。

    单元文本首尾相接拼回原文一字不差。表格行 / 代码块行 / 列表行各成一个单元
    （绝不切半）；叙述行按句末成单元；约束词命中的条件句与其后至少一个单元绑定
    为一个单元（结论跟条件走，绑定链到「非条件句的尾巴」为止）。
    """
    raw: list[tuple[str, bool]] = []   # (单元文本, 是否结构化行单元)
    prose: list[str] = []

    def _flush_prose() -> None:
        if not prose:
            return
        blob = "".join(prose)
        prose.clear()
        for start, end in _sentence_spans(blob):
            raw.append((blob[start:end], False))

    for line in str(text).splitlines(keepends=True):
        if _is_structured_line(line):
            _flush_prose()
            raw.append((line, True))
        else:
            prose.append(line)
    _flush_prose()
    if not raw:
        return [(str(text), False)]

    units: list[tuple[str, bool, bool]] = []   # (文本, 是否条件绑定, 尾部是否结构化)
    for unit_text, is_struct in raw:
        if units and not units[-1][2] and _unit_is_condition(units[-1][0]):
            prev_text, prev_bound, _prev_struct = units[-1]
            # 条件单元与其后至少一个单元合并，绝不与结论分开
            units[-1] = (prev_text + unit_text, True, is_struct)
        else:
            units.append((unit_text, False, is_struct))
    return [(unit_text, bound) for unit_text, bound, _ in units]


def _group_ends_with_condition(text: str) -> bool:
    """段落组的末单元是否条件句（结构化行自带条件与结论语义，不算条件句）。"""
    units = _paragraph_units(str(text))
    last_text = units[-1][0]
    if _is_structured_line(last_text):
        return False
    return _unit_is_condition(last_text)


# ── 二次切分（标题块内） ───────────────────────────────────────────


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


def _hard_split_by_chars(text: str, limit: int) -> list[str]:
    """无换行的超长串按字符兜底硬切（字数上限），切点优先落句末与词界。"""
    pieces: list[str] = []
    pos = 0
    while pos < len(text):
        cut = _split_point(text, pos, limit)
        pieces.append(text[pos:cut])
        if cut >= len(text):
            break
        pos = cut
    return pieces or [str(text)]


def _hard_split_unit(text: str, limit: int) -> list[str]:
    """原子单元内部硬切（最后兜底）：优先按行累积（表格行 / 列表行 / 代码行不切半），
    单行超长才按字符位置硬切。"""
    pieces: list[str] = []
    buf = ""
    for line in str(text).split("\n"):
        if len(line) > limit:
            if buf:
                pieces.append(buf)
                buf = ""
            pieces.extend(_hard_split_by_chars(line, limit))
            continue
        candidate = line if not buf else f"{buf}\n{line}"
        if len(candidate) <= limit:
            buf = candidate
            continue
        pieces.append(buf)
        buf = line
    if buf:
        pieces.append(buf)
    return pieces or [str(text)]


def _split_paragraph(text: str, limit: int) -> list[str]:
    """单个段落组放不下时按原子单元切（约束词级断点）。

    单元整体搬不动才字数上限硬切；「条件+结论」绑定单元 ≤ 1200 字符时整体成块
    （宁可超字数也不劈开），超过 1200 才允许内部硬切。
    """
    units = _paragraph_units(str(text))
    pieces: list[str] = []
    buf = ""

    def _flush() -> None:
        nonlocal buf
        if buf:
            pieces.append(buf)
            buf = ""

    for unit_text, bound in units:
        if len(unit_text) > limit:
            _flush()
            if bound and len(unit_text) <= ATOMIC_UNIT_HARD_LIMIT:
                # 条件+结论单元整体成块：宁可块超长也不劈开条件与结论
                pieces.append(unit_text)
            else:
                pieces.extend(_hard_split_unit(unit_text, limit))
            continue
        if not buf:
            buf = unit_text
            continue
        if len(buf) + len(unit_text) <= limit:
            buf += unit_text
            continue
        _flush()
        buf = unit_text
    _flush()
    return pieces or [str(text)]


def _paragraph_groups(block: str) -> list[str]:
    """空行分段；末句是条件句的段与其后段粘连成组（段落边界也不许劈开条件与结论）。"""
    paragraphs = [p.strip() for p in _BLANK_LINE_RE.split(str(block)) if p.strip()]
    groups: list[str] = []
    index = 0
    while index < len(paragraphs):
        group = paragraphs[index]
        index += 1
        while index < len(paragraphs) and _group_ends_with_condition(group):
            group = f"{group}\n\n{paragraphs[index]}"
            index += 1
        groups.append(group)
    return groups


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


def _next_overlap(body: str) -> str:
    """下一块开头要重复的上一块尾部：结构化内容重叠为 0；叙事长文约 10%
    （上限 OVERLAP_CHARS），只取完整行。"""
    text = str(body)
    if _is_structured_text(text):
        return ""
    budget = min(OVERLAP_CHARS, int(len(text) * 0.1))
    return _tail_overlap(text, budget)


def _merge_tail(tail: str, body: str, limit: int) -> str:
    """把重叠尾拼到块开头；拼完超字数上限就逐行减尾，减无可减就不重叠。"""
    if not tail:
        return body
    while tail and len(tail) + 1 + len(body) > limit:
        tail = "\n".join(tail.split("\n")[1:])
    return f"{tail}\n{body}" if tail else body


def _split_long_block(block: str, limit: int = MAX_CHUNK_CHARS) -> list[str]:
    """超长基础块二次切分子块，断点优先级：段落 > 约束词（句末）> 字数上限。

    - 段落级：空行分段后按段打包，放不下就在段落边界落块；条件句收尾的段与其后
      段先粘连成组，段落边界也不劈开条件与结论。
    - 约束词级：单个段落组放不下时按原子单元打包（条件+结论绑定单元绝不切断）。
    - 字数上限：原子单元超 1200 字符（或无任何断点的超长内容）才内部硬切。
    相邻子块重叠见 _next_overlap（结构化 0、叙事约 10%，只取完整行）。
    """
    pieces: list[str] = []
    tail = ""
    buf = ""

    def _room() -> int:
        # 块长含重叠尾：给尾部留出余量，叙事重叠才拼得进去
        return limit - (len(tail) + 1 if tail else 0)

    def _emit(body: str) -> None:
        nonlocal tail
        pieces.append(_merge_tail(tail, body, limit))
        tail = _next_overlap(body)

    def _flush() -> None:
        nonlocal buf
        if buf:
            _emit(buf)
            buf = ""

    for group in _paragraph_groups(block):
        if len(group) > limit:
            # 段落组整体放不下：先落当前块，再按约束词级/字数上限切本组
            _flush()
            for part in _split_paragraph(group, limit):
                _emit(part)
            continue
        candidate = f"{buf}\n\n{group}" if buf else group
        if len(candidate) <= _room():
            buf = candidate
            continue
        _flush()
        buf = group
    _flush()
    return pieces or [str(block)]


# ── 标题结构 ───────────────────────────────────────────────────────


def _split_by_headings(body: str) -> list[tuple[list[str], str]]:
    """按 1-3 级标题行切分，返回 [(标题路径, 块正文)]，正文含该块的标题行。

    标题路径是自根标题到本块标题的完整路径（各级标题文本列表）；
    标题之前的引言块标题路径为空列表。
    """
    blocks: list[tuple[list[str], str]] = []
    stack: list[str] = []
    buf: list[str] = []
    started = False

    for line in body.splitlines():
        match = _HEADING_RE.match(line)
        if match:
            level = len(match.group(1))
            if started or buf:
                blocks.append((list(stack), "\n".join(buf).strip()))
            stack = stack[: level - 1] + [_clean_title(match.group(2))]
            buf = [line.strip()]
            started = True
        else:
            buf.append(line)

    if started or buf:
        blocks.append((list(stack), "\n".join(buf).strip()))
    return blocks


# ── 入口 ───────────────────────────────────────────────────────────


def split_document(text: str, doc_type: str, metadata: dict) -> list[dict]:
    """把文档切分为 chunk 列表（v4 切片规则，见模块 docstring）。

    Args:
        text: 文档全文。
        doc_type: "concept"（概念层，按标题切）| "paper"（论文层，整篇一块）|
            其余类型整篇兜底一块。
        metadata: 附加到每个 chunk 的元数据（如 layer / category / title）；
            显式给出 chunk_type 时按原值透传。

    Returns:
        [{"content": str, "metadata": {...含 chunk_index / section_title /
            heading_path / parent_id / chunk_type}}]
    """
    base_meta = dict(metadata or {})
    chunks: list[dict] = []

    def _header_for(levels: list[str]) -> tuple[str, str]:
        """拼 Contextual Header（`[领域] > [章节] > [条目]`，三段都带方括号）与 heading_path。"""
        domain = str(base_meta.get("category") or "").strip() or "通用"
        if len(levels) > 1:
            # 章节 = 标题路径去掉末级（多级用 > 连接，与 heading_path 同形）
            section = " > ".join(levels[:-1])
        else:
            section = str(base_meta.get("title") or "").strip() or "正文"
        entry = levels[-1] if levels else "正文"
        return f"[{domain}] > [{section}] > [{entry}]", " > ".join(levels)

    def _push(
        body: str, levels: list[str], parent_index: int | None, role: str
    ) -> int | None:
        body = body.strip()
        if not body:
            return None
        # 标题下面没写内容时，会切出「只有标题行」的空壳块（如 `### Key Features`）。
        # 这种块检索回来占名额、不给信息，直接不入库。
        non_empty_lines = [ln for ln in body.split("\n") if ln.strip()]
        if len(non_empty_lines) == 1 and non_empty_lines[0].lstrip().startswith("#"):
            return None
        # 只有 markdown 分隔线（--- / *** / ___）的块同样不入库：纯噪声，
        # 而且会因为「字符量大」在检索里拿到高分（曾是查询第一名）
        if all(re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", ln.strip()) for ln in non_empty_lines):
            return None
        header, heading_path = _header_for(levels)
        chunks.append(
            {
                "content": f"{header}\n\n{body}",
                "metadata": {
                    **base_meta,
                    "chunk_index": len(chunks),
                    "section_title": levels[-1] if levels else "",
                    "heading_path": heading_path,
                    "parent_id": str(parent_index) if parent_index is not None else "",
                    # 调用方显式给的 chunk_type 原样透传，否则按角色赋值
                    "chunk_type": str(base_meta.get("chunk_type") or role),
                },
            }
        )
        return len(chunks) - 1

    if doc_type == "concept":
        body = _strip_front_matter(text)
        for levels, block in _split_by_headings(body):
            header, _heading_path = _header_for(levels)
            # 字数上限按「块正文 + Contextual Header」总长计，给 header 留出余量
            limit = max(64, MAX_CHUNK_CHARS - len(header) - 2)
            if len(block) > limit:
                # 超长基础块：先入二次切出的子块（section，parent_id 指向父块），
                # 再入父块（parent）。子块在前，让参与检索的小块拿靠前的 chunk_index。
                parts = _split_long_block(block, limit)
                child_indexes = [
                    index
                    for index in (_push(part, levels, None, "section") for part in parts)
                    if index is not None
                ]
                parent_index = _push(block, levels, None, "parent")
                if parent_index is not None:
                    for index in child_indexes:
                        chunks[index]["metadata"]["parent_id"] = str(parent_index)
            else:
                # 未超长的基础块没有子块可指，自身就是可检索的小节块（section）
                _push(block, levels, None, "section")
    else:
        # paper 整篇一块（abstract）；未知类型整篇兜底一块（parent）
        _push(str(text), [], None, "abstract" if doc_type == "paper" else "parent")

    return chunks
