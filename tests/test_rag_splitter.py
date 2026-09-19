# RAG 基础框架自测 - 分块器（纯假文本，零 API）
"""split_document 零成本自测：只喂假文本，不触发任何网络/模型调用。

覆盖：
1. YAML front matter 剥离（含未闭合兜底）；
2. 1-3 级标题切分与 section_title 正确（含标题前引言 section_title 为空串）；
3. 超长基础块按空行段落二次切分（每块 ≤800 字符、相邻块重叠 100 字符）；
4. paper 整篇单块；
5. chunk_index 从 0 连续递增；
6. metadata 原样透传且不被改写。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_splitter.py -q
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kb.rag.splitter import MAX_CHUNK_CHARS, OVERLAP_CHARS, split_document  # noqa: E402


def _titles(chunks: list[dict]) -> list[str]:
    return [c["metadata"]["section_title"] for c in chunks]


def _indexes(chunks: list[dict]) -> list[int]:
    return [c["metadata"]["chunk_index"] for c in chunks]


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


# ── 2. 标题切分与 section_title ────────────────────────────────────

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
    # 标题行保留在该块正文中
    assert chunks[1]["content"].startswith("# 一级标题")


def test_heading_closure_hashes_cleaned():
    chunks = split_document("# 标题带闭合井号 ##\n正文\n", "concept", {})
    assert _titles(chunks) == ["标题带闭合井号"]


def test_four_level_heading_is_not_a_split_point():
    text = "# 一级\n正文一\n#### 四级不算标题\n正文二\n"
    chunks = split_document(text, "concept", {})

    assert len(chunks) == 1
    assert _titles(chunks) == ["一级"]
    assert "#### 四级不算标题" in chunks[0]["content"]


# ── 3. 超长块二次切分与 overlap ───────────────────────────────────

def test_long_block_secondary_split_with_overlap():
    paragraphs = [f"第{i}段" + "甲" * 200 for i in range(6)]
    text = "# 长节\n" + "\n\n".join(paragraphs) + "\n"
    assert len(text) > MAX_CHUNK_CHARS

    chunks = split_document(text, "concept", {"layer": "concept"})

    assert len(chunks) >= 2
    for chunk in chunks:
        assert len(chunk["content"]) <= MAX_CHUNK_CHARS
    # 相邻块重叠：后一块开头含前一块尾部 100 字符
    for prev, nxt in zip(chunks, chunks[1:]):
        assert prev["content"][-OVERLAP_CHARS:] in nxt["content"]
    # section_title 与 chunk_index 在子块上保持一致/连续
    assert _titles(chunks) == ["长节"] * len(chunks)
    assert _indexes(chunks) == list(range(len(chunks)))


def test_single_huge_paragraph_is_hard_split():
    text = "# 巨段\n" + "乙" * 2000 + "\n"
    chunks = split_document(text, "concept", {})

    assert len(chunks) >= 3
    for chunk in chunks:
        assert len(chunk["content"]) <= MAX_CHUNK_CHARS


def test_short_block_is_not_split():
    text = "# 小节\n很短的一段正文。\n"
    chunks = split_document(text, "concept", {})

    assert len(chunks) == 1
    assert chunks[0]["content"] == "# 小节\n很短的一段正文。"


# ── 4. paper 整篇单块 ─────────────────────────────────────────────

def test_paper_single_chunk():
    text = "摘要：本文提出一种方法。" + "丙" * 3000
    chunks = split_document(text, "paper", {"layer": "curated-paper", "arxiv_id": "2308.11432"})

    assert len(chunks) == 1
    assert chunks[0]["metadata"]["section_title"] == ""
    assert chunks[0]["metadata"]["chunk_index"] == 0
    assert chunks[0]["metadata"]["arxiv_id"] == "2308.11432"
    assert len(chunks[0]["content"]) > MAX_CHUNK_CHARS


# ── 5/6. chunk_index 连续 + metadata 透传 ─────────────────────────

def test_chunk_index_continuous_across_mixed_blocks():
    long_block = "\n\n".join(["丁" * 200 for _ in range(6)])
    text = f"引言\n\n# 一\n短块\n\n# 二\n{long_block}\n\n# 三\n收尾\n"
    chunks = split_document(text, "concept", {"doc_id": "d3"})

    assert len(chunks) >= 5
    assert _indexes(chunks) == list(range(len(chunks)))
    assert all(c["metadata"]["doc_id"] == "d3" for c in chunks)


def test_metadata_not_mutated():
    meta = {"layer": "concept", "category": "AgentHarness"}
    chunks = split_document("# 标题\n正文\n", "concept", meta)

    assert meta == {"layer": "concept", "category": "AgentHarness"}
    assert chunks[0]["metadata"] is not meta
    assert chunks[0]["metadata"]["category"] == "AgentHarness"


def test_empty_text_returns_no_chunk():
    assert split_document("", "concept", {}) == []
    assert split_document("   \n\n", "paper", {}) == []


