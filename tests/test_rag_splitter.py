# RAG 基础框架自测 - 分块器（纯假文本，零 API）
"""split_document 零成本自测：只喂假文本，不触发任何网络/模型调用。

覆盖（v4 切片规则）：
1. YAML front matter 剥离（含未闭合兜底）；
2. 1-3 级标题切分与 section_title / heading_path 正确；
3. Contextual Header 格式 `[领域] > [章节] > [条目]` 与取值规则；
4. 断点优先级：标题 > 段落 > 约束词（句末）> 字数上限；
5. 条件句与结论绝不切断（含跨段落绑定、超 1200 字原子单元才允许内部硬切）；
5b. 英文条件句保护（v4 新增）：英文约束词整词命中、大小写不敏感、词缀不误命中，
   英文条件句与其结论同块；
6. 父子块：父块 parent / 子块 section / paper abstract、parent_id 链接、chunk_index 连续；
7. overlap：结构化 0、叙事约 10%（上限 100，只取完整行）；
8. 保护逻辑：空壳块 / 纯分隔线块剔除、表格行不切半、单行超长不死循环；
9. 确定性：同输入同参数两次运行输出完全一致。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_splitter.py -q
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kb.rag.splitter import (  # noqa: E402
    ATOMIC_UNIT_HARD_LIMIT,
    MAX_CHUNK_CHARS,
    OVERLAP_CHARS,
    split_document,
)
# 私有判定函数：英文约束词的「整词命中」口径只能在匹配器这一层钉住，故直接测它
from kb.rag.splitter import _group_ends_with_condition, _is_condition_sentence  # noqa: E402


def _titles(chunks: list[dict]) -> list[str]:
    return [c["metadata"]["section_title"] for c in chunks]


def _indexes(chunks: list[dict]) -> list[int]:
    return [c["metadata"]["chunk_index"] for c in chunks]


def _children(chunks: list[dict]) -> list[dict]:
    return [c for c in chunks if c["metadata"]["chunk_type"] == "section"]


def _body(chunk: dict) -> str:
    """去掉 Contextual Header 后的块正文（header 占一行 + 一个空行）。"""
    return chunk["content"].split("\n\n", 1)[1]


def _header(chunk: dict) -> str:
    return chunk["content"].split("\n\n", 1)[0]


def _overlap_len(prev_body: str, nxt_body: str) -> int:
    """相邻块的实际重叠字符数（上一块尾部整行出现在下一块开头的最长匹配）。"""
    prev_lines = prev_body.rstrip().split("\n")
    nxt_lines = nxt_body.split("\n")
    for k in range(min(len(prev_lines), len(nxt_lines)), 0, -1):
        if nxt_lines[:k] == prev_lines[-k:]:
            return len("\n".join(prev_lines[-k:]))
    return 0


# ── 1. front matter ────────────────────────────────────────────────

def test_front_matter_stripped():
    text = (
        "---\n"
        "type: Playbook\n"
        "title: Agent Harness\n"
        "tags: [agent-harness, engineering]\n"
        "---\n"
        "# 什么是 Harness\n"
        "Harness 是包在模型外面的一切代码与配置。\n"
    )
    chunks = split_document(text, "concept", {"layer": "concept"})

    assert len(chunks) == 1
    assert "type: Playbook" not in chunks[0]["content"]
    assert "tags:" not in chunks[0]["content"]
    assert "Harness 是包在模型外面的一切代码与配置。" in chunks[0]["content"]
    assert chunks[0]["metadata"]["section_title"] == "什么是 Harness"


def test_front_matter_without_closing_marker_kept_as_is():
    text = "---\ntype: Playbook\n没有闭合分隔符的正文"
    chunks = split_document(text, "concept", {})

    assert len(chunks) == 1
    assert "type: Playbook" in chunks[0]["content"]


# ── 2. 标题切分与 section_title / heading_path ─────────────────────

def test_multi_level_heading_split_and_section_title():
    text = (
        "开篇引言段落。\n"
        "\n"
        "# 一级标题\n"
        "一级正文。\n"
        "\n"
        "## 二级标题\n"
        "二级正文。\n"
        "\n"
        "### 三级标题\n"
        "三级正文。\n"
    )
    chunks = split_document(text, "concept", {"doc_id": "d1"})

    assert _titles(chunks) == ["", "一级标题", "二级标题", "三级标题"]
    assert _indexes(chunks) == [0, 1, 2, 3]
    assert "开篇引言段落。" in chunks[0]["content"]
    assert "三级正文。" in chunks[3]["content"]
    # 标题行保留在该块正文中，Contextual Header 在正文之前
    assert chunks[1]["content"].endswith("# 一级标题\n一级正文。")
    assert _body(chunks[1]).startswith("# 一级标题")
    assert chunks[3]["metadata"]["heading_path"] == "一级标题 > 二级标题 > 三级标题"
    assert chunks[0]["metadata"]["heading_path"] == ""


def test_heading_closure_hashes_cleaned():
    chunks = split_document("# 标题带闭合井号 ##\n正文\n", "concept", {})
    assert _titles(chunks) == ["标题带闭合井号"]


def test_four_level_heading_is_not_a_split_point():
    text = "# 一级\n正文一\n#### 四级不算标题\n正文二\n"
    chunks = split_document(text, "concept", {})

    assert len(chunks) == 1
    assert _titles(chunks) == ["一级"]
    assert "#### 四级不算标题" in chunks[0]["content"]


# ── 3. Contextual Header（硬约束 C）────────────────────────────────

def test_contextual_header_format_and_values():
    text = (
        "# 概览\n"
        "介绍。\n"
        "\n"
        "## 设计\n"
        "设计正文。\n"
        "\n"
        "### 细节\n"
        "细节正文。\n"
    )
    chunks = split_document(
        text, "concept", {"category": "architecture", "title": "Agent Harness"}
    )

    headers = [_header(c) for c in chunks]
    assert headers == [
        # 领域 = category；条目只有一级时章节回落到文档 title；多级时章节 = 上级路径（> 连接）
        "[architecture] > [Agent Harness] > [概览]",
        "[architecture] > [概览] > [设计]",
        "[architecture] > [概览 > 设计] > [细节]",
    ]
    # header 写进 content：第一行是 header，空一行后才是正文（参与嵌入与词法索引）
    for chunk, header in zip(chunks, headers):
        assert chunk["content"] == f"{header}\n\n{_body(chunk)}"
    # metadata 同时存完整标题路径（> 连接）
    assert [c["metadata"]["heading_path"] for c in chunks] == [
        "概览",
        "概览 > 设计",
        "概览 > 设计 > 细节",
    ]
    # 格式不变量：三段都带方括号，恰好两个 "] > [" 分隔（回归：曾退化成无方括号）
    for header in headers:
        assert header.startswith("[") and header.endswith("]")
        assert header.count("] > [") == 2


def test_contextual_header_fallbacks():
    # 无 category → 领域「通用」；无上级且无 title → 章节「正文」；无标题 → 条目「正文」
    intro = split_document("只有引言没有标题。\n", "concept", {})[0]
    assert _header(intro) == "[通用] > [正文] > [正文]"

    one_level = split_document("# 只有一级\n正文。\n", "concept", {})[0]
    assert _header(one_level) == "[通用] > [正文] > [只有一级]"

    no_category = split_document(
        "# 小节\n正文。\n", "concept", {"title": "文档题"}
    )[0]
    assert _header(no_category) == "[通用] > [文档题] > [小节]"


# ── 4. 断点优先级（硬约束 A）──────────────────────────────────────

def test_break_priority_paragraph_over_sentence():
    """段落级断点优先：长块的子块按整段拼，不会从段落中间下刀。"""
    paragraphs = [
        "".join(f"第{i}段第{j}句的内容写在这里。" for j in range(10)) for i in range(8)
    ]
    text = "# 长节\n\n" + "\n\n".join(paragraphs) + "\n"
    assert len(text) > MAX_CHUNK_CHARS

    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for child in children:
        for para in _body(child).split("\n\n"):
            # 块标题行（# 长节）随首个段落组落块，与库内实况一致；其余必须是整段，不许出碎片
            if para == "# 长节":
                continue
            assert para in paragraphs, f"子块出现段落碎片: {para[:30]!r}"


def test_break_priority_sentence_inside_long_paragraph():
    """段落放不下才落句末断点：超长单段的子块在句末收尾。"""
    paragraph = "".join(f"这是超长段落里的第{j}句话内容。" for j in range(60))
    text = "# 长段\n\n" + paragraph + "\n"
    assert len(paragraph) > MAX_CHUNK_CHARS
    assert "\n\n" not in paragraph  # 单段无段落级断点，只能落句末

    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for child in children:
        assert _body(child).endswith("。"), f"子块没落在句末: {_body(child)[-20:]!r}"


def test_char_limit_is_last_resort():
    """前三种断点都放不下（单行超长无句末）才按字数上限兜底硬切。"""
    text = "# 长行\na " + "乙" * 3000 + "\n"
    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 3
    for child in children:
        assert len(_body(child)) <= MAX_CHUNK_CHARS


# ── 5. 条件句与结论不切断（硬约束 B）──────────────────────────────

def test_condition_sentence_never_split_from_conclusion():
    """条件句必须和紧随其后的至少一个句子落在同一个块（含跨段落绑定）。"""
    lines_a = []
    lines_b = []
    for i in range(20):
        lines_a.append(f"第{i}条：如果当前状态{i}满足前置校验{i}，系统必须进入处理分支{i}。")
        lines_b.append(f"随后输出结果{i}并记录日志{i}，同时写入审计轨迹{i}。")
    # 每对「条件句 + 结论」逐对相邻：条件句必须与紧随其后的句子同块。
    # 第 6 对的条件句（a6）收尾本段、结论句（b6）被空行推到下一段——
    # 段落边界也不许把条件与结论劈开，这正是「条件并块」判据要挡住的场景。
    head = [x for pair in zip(lines_a[:6], lines_b[:6]) for x in pair] + [lines_a[6]]
    tail = [lines_b[6]] + [x for pair in zip(lines_a[7:], lines_b[7:]) for x in pair]
    text = "# 规则集\n\n" + "\n".join(head) + "\n\n" + "\n".join(tail) + "\n"
    chunks = split_document(text, "concept", {"category": "concepts"})
    assert len(chunks) >= 2  # 确实发生了切分

    for i in range(20):
        cond_chunks = [c for c in chunks if lines_a[i] in c["content"]]
        assert cond_chunks, f"条件句 {i} 丢失"
        for chunk in cond_chunks:
            assert lines_b[i] in chunk["content"], (
                f"条件句 {i} 与结论被切开（chunk_index="
                f"{chunk['metadata']['chunk_index']}）"
            )


def test_condition_unit_kept_whole_even_over_char_limit():
    """条件+结论原子单元 ≤ 1200 字符时整体成块，宁可超字数上限也不劈开。"""
    cond = "如果输入校验通过，则进入主流程。"
    concl = "系统会依次完成" + "长" * 860 + "的处理。"
    assert MAX_CHUNK_CHARS < len(cond) + len(concl) <= ATOMIC_UNIT_HARD_LIMIT
    text = "# 示例\n\n" + cond + concl + "\n"

    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    whole = [c for c in children if cond in _body(c)]
    assert whole, "条件单元丢失"
    for chunk in whole:
        assert concl in _body(chunk), "条件与结论被劈开"
        assert len(_body(chunk)) <= ATOMIC_UNIT_HARD_LIMIT


def test_condition_unit_over_1200_is_hard_split_last_resort():
    """只有原子单元自身超 1200 字符才允许内部硬切（最后兜底）。"""
    cond = "如果输入校验通过，则进入主流程。"
    concl = "系统会依次完成" + "长" * 1250 + "的处理。"
    assert len(cond) + len(concl) > ATOMIC_UNIT_HARD_LIMIT
    text = "# 示例\n\n" + cond + concl + "\n"

    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for child in children:
        assert len(_body(child)) <= MAX_CHUNK_CHARS
    assert any(cond in _body(c) for c in children), "条件句丢失"


# ── 5b. 英文条件句保护（v4 新增）──────────────────────────────────
# 背景：约束词表原先是全中文，而知识库语料 209 篇都是英文技术文档，
# 这条保护从未触发过。v4 起补英文对照词 + 整词匹配。


def test_english_condition_sentence_never_split_from_conclusion():
    """英文条件句必须和紧随其后的结论句同块（含跨段落绑定）。

    对应用例 5 的英文版：第 6 对的条件句收尾本段、结论句被空行推到下一段，
    段落边界也不许把两者劈开。
    """
    lines_a = []
    lines_b = []
    for i in range(20):
        lines_a.append(
            f"Rule {i}: if the current state {i} passes the pre-check {i}, "
            f"the agent must enter branch {i}."
        )
        lines_b.append(
            f"It then emits result {i}, records log {i} and writes audit trail {i}."
        )
    head = [x for pair in zip(lines_a[:6], lines_b[:6]) for x in pair] + [lines_a[6]]
    tail = [lines_b[6]] + [x for pair in zip(lines_a[7:], lines_b[7:]) for x in pair]
    text = "# Rules\n\n" + "\n".join(head) + "\n\n" + "\n".join(tail) + "\n"
    chunks = split_document(text, "concept", {"category": "concepts"})
    assert len(chunks) >= 2  # 确实发生了切分

    for i in range(20):
        cond_chunks = [c for c in chunks if lines_a[i] in c["content"]]
        assert cond_chunks, f"英文条件句 {i} 丢失"
        for chunk in cond_chunks:
            assert lines_b[i] in chunk["content"], (
                f"英文条件句 {i} 与其结论被切开（chunk_index="
                f"{chunk['metadata']['chunk_index']}）"
            )


def test_english_condition_unit_kept_whole_inside_long_paragraph():
    """长英文段落里条件句与结论同属一个原子单元，块边界不落在两者之间。"""
    pairs: list[str] = []
    for i in range(12):
        pairs.append(
            f"If validator {i} rejects the payload, the pipeline must halt "
            f"and emit diagnostic {i}."
        )
        pairs.append(
            f"The operator then receives alert {i} together with the recorded trace {i}."
        )
    paragraph = " ".join(pairs)
    assert len(paragraph) > MAX_CHUNK_CHARS  # 单段超长，必然要二次切分

    text = "# Rules\n\n" + paragraph + "\n"
    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for i in range(12):
        cond, concl = pairs[2 * i], pairs[2 * i + 1]
        holders = [c for c in children if cond in _body(c)]
        assert holders, f"英文条件句 {i} 丢失"
        for chunk in holders:
            assert concl in _body(chunk), f"英文条件句 {i} 与其结论被切开"


def test_english_constraint_words_match_whole_word_only():
    """英文约束词按整词命中、大小写不敏感：词缀里的 must / if 不算条件句。

    若照搬中文表的子串匹配，if 会命中 verify / specific / modify，
    must 会命中 mustering —— 整库绝大多数句子都会被误判成条件句。
    """
    # 命中：单词与词组、忽略大小写
    assert _is_condition_sentence("The agent MUST abort the run.")
    assert _is_condition_sentence("Unless the flag is set, the run is skipped.")
    assert _is_condition_sentence("Run the pre-check only when the cache is cold.")
    assert _is_condition_sentence("The agent shall refuse the request.")
    # 不命中：词缀误命中（这些是本用例真正要挡的）
    assert not _is_condition_sentence("The fleet is mustering at dawn.")
    assert not _is_condition_sentence("Please verify the output and modify the config.")
    assert not _is_condition_sentence("The sixth option is specific to this shell.")
    assert not _is_condition_sentence("The requirement list is long.")
    # 中文词表行为不变：仍是子串匹配、宁多勿漏
    assert _is_condition_sentence("系统必须进入处理分支。")


def test_english_affix_words_do_not_trigger_condition_binding():
    """行为口径：含词缀（mustering / verify）的段落不被当成条件句而粘连下一段。"""
    assert _group_ends_with_condition("The fleet is mustering at dawn.") is False
    assert _group_ends_with_condition("Please verify the output.") is False
    assert _group_ends_with_condition("If the run fails, the agent must retry.") is True


# ── 6. 父子块与 parent_id（硬约束 D）──────────────────────────────

def test_parent_child_blocks_and_parent_id():
    paragraphs = [f"第{i}段" + "甲" * 30 for i in range(40)]
    text = "# 长节\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = split_document(text, "concept", {"category": "rag", "title": "切片"})

    parents = [c for c in chunks if c["metadata"]["chunk_type"] == "parent"]
    children = _children(chunks)
    assert len(parents) == 1
    assert children
    # 父块、子块都入库且都带 Contextual Header（三段都带方括号）
    assert _header(parents[0]) == "[rag] > [切片] > [长节]"
    assert _header(children[0]) == "[rag] > [切片] > [长节]"
    # parent_id 链接：子块指向父块，父块自身为空串
    parent_index = parents[0]["metadata"]["chunk_index"]
    assert parents[0]["metadata"]["parent_id"] == ""
    for child in children:
        assert child["metadata"]["parent_id"] == str(parent_index)
    # 块 ID 沿用现行实现：ID 由 chunk_index 拼出，parent_id 可还原父块 ID
    assert int(children[0]["metadata"]["parent_id"]) == parent_index
    # chunk_index 全文档连续递增（父子块都算）
    assert _indexes(chunks) == list(range(len(chunks)))
    # 子块内容取自父块（父子同标题路径）
    assert children[0]["metadata"]["heading_path"] == parents[0]["metadata"]["heading_path"]


def test_chunk_type_values():
    # 父块 parent、子块 section、paper 整篇 abstract、未知类型兜底 parent
    paragraphs = [f"第{i}段" + "甲" * 30 for i in range(40)]
    long_text = "# 长节\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = split_document(long_text, "concept", {})
    assert {c["metadata"]["chunk_type"] for c in chunks} == {"parent", "section"}

    paper = split_document("摘要：本文提出一种方法。", "paper", {"arxiv_id": "1"})
    assert paper[0]["metadata"]["chunk_type"] == "abstract"

    other = split_document("没有标题的整篇内容。", "thread", {})
    assert other[0]["metadata"]["chunk_type"] == "parent"

    # 调用方显式给出的 chunk_type 原样透传（增量入库按统一 schema 4.3 定值）
    forced = split_document("# 小节\n正文。\n", "concept", {"chunk_type": "section"})
    assert forced[0]["metadata"]["chunk_type"] == "section"


# ── 7. overlap 规则（硬约束 E）────────────────────────────────────

def test_structured_content_overlap_zero():
    rows = [f"| {i} | 名称{i} | " + ("能力说明第%d行" % i) * 12 + " | 场景{i} |" for i in range(8)]
    text = "# 成熟度分级\n\n" + "\n".join(rows) + "\n"
    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for prev, nxt in zip(children, children[1:]):
        assert _overlap_len(_body(prev), _body(nxt)) == 0, "结构化内容不应有重叠"


def test_narrative_overlap_about_ten_percent():
    paragraphs = [f"第{i}段" + "甲" * 40 for i in range(40)]
    text = "# 长节\n\n" + "\n\n".join(paragraphs) + "\n"
    chunks = split_document(text, "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    for prev, nxt in zip(children, children[1:]):
        overlap = _overlap_len(_body(prev), _body(nxt))
        # 有重叠、不超上限、且约 10%（整行取整允许偏差）
        assert 0 < overlap <= OVERLAP_CHARS
        assert overlap <= len(_body(prev)) * 0.2
        # 重叠只取完整行：上一块最后一行整体出现在下一块开头
        last_line = _body(prev).rstrip().split("\n")[-1]
        assert _body(nxt).startswith(last_line)


# ── 8. 超长块二次切分（适配 v2 用例）───────────────────────────────

def test_long_block_secondary_split_with_overlap():
    paragraphs = [f"第{i}段" + "甲" * 20 for i in range(60)]
    text = "# 长节\n\n" + "\n\n".join(paragraphs) + "\n"
    assert len(text) > MAX_CHUNK_CHARS

    chunks = split_document(text, "concept", {"layer": "concept"})
    children = _children(chunks)
    assert len(children) >= 2
    for child in children:
        assert len(_body(child)) <= MAX_CHUNK_CHARS
    # 相邻子块重叠取「完整行」：上一块最后一行必须出现在下一块里，跨块内容不丢。
    # （曾按字符截尾部 100 字符，切出 `ation |` 这种半截表格行）
    for prev, nxt in zip(children, children[1:]):
        last_line = _body(prev).rstrip().split("\n")[-1]
        assert last_line in _body(nxt)
    # section_title 与 chunk_index 在子块上保持一致/连续
    assert _titles(children) == ["长节"] * len(children)
    assert _indexes(chunks) == list(range(len(chunks)))


def test_single_huge_paragraph_is_hard_split():
    text = "# 巨段\n" + "乙" * 2000 + "\n"
    chunks = split_document(text, "concept", {})

    children = _children(chunks)
    assert len(children) >= 3
    for child in children:
        assert len(_body(child)) <= MAX_CHUNK_CHARS


def test_short_block_is_not_split():
    text = "# 小节\n很短的一段正文。\n"
    chunks = split_document(text, "concept", {})

    assert len(chunks) == 1
    assert _body(chunks[0]) == "# 小节\n很短的一段正文。"


# ── 9. paper 整篇单块 ─────────────────────────────────────────────

def test_paper_single_chunk():
    text = "摘要：本文提出一种方法。" + "丙" * 3000
    chunks = split_document(
        text,
        "paper",
        {
            "layer": "curated-paper",
            "category": "surveys",
            "title": "A Survey on Agent Memory",
            "arxiv_id": "2308.11432",
        },
    )

    assert len(chunks) == 1
    assert chunks[0]["metadata"]["section_title"] == ""
    assert chunks[0]["metadata"]["chunk_index"] == 0
    assert chunks[0]["metadata"]["arxiv_id"] == "2308.11432"
    assert chunks[0]["metadata"]["chunk_type"] == "abstract"
    assert chunks[0]["metadata"]["parent_id"] == ""
    assert len(_body(chunks[0])) > MAX_CHUNK_CHARS
    # 领域 = category；整篇块没有标题路径 → 章节用文档 title、条目用「正文」
    assert chunks[0]["content"].startswith(
        "[surveys] > [A Survey on Agent Memory] > [正文]\n\n"
    )


# ── 10. chunk_index 连续 + metadata 透传 ──────────────────────────

def test_chunk_index_continuous_across_mixed_blocks():
    long_block = "\n\n".join(["丁" * 200 for _ in range(6)])
    text = f"引言\n\n# 一\n短块\n\n# 二\n{long_block}\n\n# 三\n收尾\n"
    chunks = split_document(text, "concept", {"doc_id": "d3"})

    assert len(chunks) >= 5
    assert _indexes(chunks) == list(range(len(chunks)))
    assert all(c["metadata"]["doc_id"] == "d3" for c in chunks)
    # 每块 metadata 都带三个新键
    for chunk in chunks:
        meta = chunk["metadata"]
        assert isinstance(meta["heading_path"], str)
        assert isinstance(meta["parent_id"], str)
        assert meta["chunk_type"] in {"parent", "section", "abstract"}


def test_metadata_not_mutated():
    meta = {"layer": "concept", "category": "AgentHarness"}
    chunks = split_document("# 标题\n正文\n", "concept", meta)

    assert meta == {"layer": "concept", "category": "AgentHarness"}
    assert chunks[0]["metadata"] is not meta
    assert chunks[0]["metadata"]["category"] == "AgentHarness"


def test_empty_text_returns_no_chunk():
    assert split_document("", "concept", {}) == []
    assert split_document("   \n\n", "paper", {}) == []


# ── 11. 保护逻辑（硬约束 F，v2 保留）──────────────────────────────

def test_table_rows_are_not_split_mid_row():
    """表格行与行之间没有空行，不能把刀砍在行中间。

    回归：曾切出 `ion with guardrails |`（`automation` 被劈成两半），
    检索回来没有列头，读者不知道这行在说谁。
    """
    rows = [f"| {i} | 名称{i} | " + ("能力说明第%d行" % i) * 12 + " | 场景{i} |" for i in range(8)]
    text = "# 成熟度分级\n\n" + "\n".join(rows) + "\n"
    chunks = split_document(text, "concept", {})
    assert len(chunks) >= 2
    for c in chunks:
        for line in c["content"].split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            assert line.startswith("|"), f"出现半截表格行: {line[:60]!r}"
            assert line.endswith("|"), f"表格行未闭合: {line[:60]!r}"


def test_heading_only_block_is_dropped():
    """标题下面没写内容时切出的空壳块不入库（占名额、不给信息）。"""
    text = "# 一级\n\n### 空标题\n\n### 有内容\n正文在这。\n"
    chunks = split_document(text, "concept", {})
    for c in chunks:
        body = [ln for ln in _body(c).split("\n") if ln.strip()]
        assert not (len(body) == 1 and body[0].lstrip().startswith("#")), c["content"]


def test_separator_only_block_is_dropped():
    """只有 markdown 分隔线（--- / ***）的块不入库——纯噪声，还会在检索里拿到高分。"""
    text = "# 一级\n\n---\n\n### 有内容\n正文在这。\n\n---\n\n## 二级\n正文二。\n"
    chunks = split_document(text, "concept", {})
    for c in chunks:
        body = [ln.strip() for ln in _body(c).split("\n") if ln.strip()]
        assert not all(re.fullmatch(r"(-{3,}|\*{3,}|_{3,})", ln) for ln in body), c["content"]


def test_single_huge_line_never_loops():
    """退重叠时若切点离得太近，必须继续前进，否则死循环。"""
    text = "a " + "乙" * 3000
    chunks = split_document("# 长行\n" + text + "\n", "concept", {})
    children = _children(chunks)
    assert len(children) >= 2
    assert all(len(_body(c)) <= MAX_CHUNK_CHARS for c in children)


# ── 12. 确定性（纯函数）──────────────────────────────────────────

def test_deterministic_pure_function():
    """同输入同参数两次运行输出完全一致（切片是单文档纯函数）。"""
    paragraphs = [f"第{i}段" + "甲" * 30 for i in range(20)]
    text = (
        "---\ntitle: T\n---\n"
        "# 根\n\n"
        + "\n\n".join(paragraphs)
        + "\n\n## 表格\n\n"
        + "\n".join(f"| {i} | " + "乙" * 40 + " |" for i in range(6))
        + "\n\n## 条件\n\n"
        "如果出错，则重试三次。随后记录失败日志。\n"
    )
    meta = {"category": "rag", "title": "T", "layer": "concept"}
    first = split_document(text, "concept", meta)
    second = split_document(text, "concept", meta)

    assert first == second
    assert first, "混合语料应切出至少一块"
