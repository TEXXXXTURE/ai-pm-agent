# RAG 入库器自测 - 真实临时 ChromaDB + 假 embedder（零真实 API）
"""RAGIngest 零成本自测：ChromaDB 用 tmp_path 下的真实嵌入式实例，嵌入全部走假 embedder。

覆盖：
1. 概念层目录遍历：跳过 assets / 点开头目录 / 非目录 / 根目录零散文件；
2. 返回 chunk 数与分块器实际分块数一致（超长文档多 chunk）；
3. metadata 字段值（layer/category/title/curated/chunk_index/section_title/
   chunking_version/source_category/source）正确，且不含 feishu_ref；
4. chunk ID 规则 concept-{md5(source)[:12]}-{chunk_index}；
5. 幂等：同目录重复 ingest，chunk 总数不翻倍；
6. delete_by_source / delete_by_layer 返回计数正确、删后 get 为空；
7. 论文层：abstract 缺失/过短跳过、source=arxiv_id、ID 前缀 curated-、
   category 映射、is_starter_kit 字符串与真布尔两种形态；
8. 未命中映射表的目录名不抛异常、按 architecture 兜底并告警。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_ingest.py -q
"""
from __future__ import annotations

import hashlib
import math
import sys
import warnings
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import pytest  # noqa: E402

from kb.rag.ingest import RAGIngest  # noqa: E402
from kb.rag.splitter import split_document  # noqa: E402

VECTOR_DIM = 16


class FakeEmbedder:
    """确定性假嵌入器：md5 摘要字节展开归一化，同文本两次结果一致，零 API。"""

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.md5(str(text).encode("utf-8")).digest()
        vector = [float(byte) for byte in digest[:VECTOR_DIM]]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def _config(tmp_path: Path) -> dict:
    """最小 config：chroma 落在 tmp_path，embedding 段给占位值（实际用注入的假 embedder）。"""
    return {
        "domain_kb": {
            "chroma_path": str(tmp_path / "chroma"),
            "source_path": str(tmp_path / "concept"),
            "embedding": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-Embedding-8B",
                "api_key_env": "RAG_INGEST_TEST_KEY",
                "api_base": "https://api.siliconflow.cn/v1",
                "dimensions": 4096,
                "batch_size": 32,
            },
        }
    }


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


LONG_PARAGRAPH = "记忆层负责把对话历史压缩成可检索的长期表示。" * 60
LONG_DOC_TEXT = (
    "---\ntitle: Memory Overview\ntags: [memory]\n---\n"
    "# Memory Overview\n\n" + LONG_PARAGRAPH + "\n"
)


def _build_concept_tree(root: Path) -> Path:
    """造一个假概念层目录：3 个分类子目录 + assets + 未命中目录 + 根目录零散文件。"""
    concept_root = root / "concept"
    _write(
        concept_root / "Concepts" / "agent-definition.md",
        "---\ntype: Concept\ntitle: Agent Definition\ntags: [concepts, agentic-ai]\n---\n"
        "# Agent Definition\n\nAgent 是能感知环境并自主行动的软件实体。\n",
    )
    _write(
        concept_root / "Concepts" / "no-title-doc.md",
        "# Untitled Doc\n\n这篇文档没有 front matter，标题应回落到文件名。\n",
    )
    _write(concept_root / "AgentMemory" / "memory-overview.md", LONG_DOC_TEXT)
    _write(concept_root / "assets" / "logo-note.md", "# 资源目录说明\n\n不应被入库。\n")
    (concept_root / "assets").mkdir(parents=True, exist_ok=True)
    (concept_root / "assets" / "logo.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    _write(
        concept_root / "FooBar" / "foo.md",
        "---\ntitle: Foo Doc\n---\n# Foo\n\n未命中分类映射表的内容。\n",
    )
    _write(concept_root / "README.md", "# 根目录零散文件\n\n不应被入库。\n")
    return concept_root


def _expected_concept_total() -> int:
    """按分块器口径算期望 chunk 数（元数据不影响切分结果）。"""
    fixed = 3  # agent-definition(1) + no-title-doc(1) + FooBar/foo.md(1)
    long_chunks = len(split_document(LONG_DOC_TEXT, "concept", {}))
    return fixed + long_chunks


# ── 1-4. 概念层：遍历、计数、metadata、ID ─────────────────────────

def test_ingest_concept_dir_count_and_skip(tmp_path):
    concept_root = _build_concept_tree(tmp_path)
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        total = ingest.ingest_concept_dir(str(concept_root))

    # 计数与分块器一致，且超长文档确实切成多块
    assert total == _expected_concept_total()
    assert len(split_document(LONG_DOC_TEXT, "concept", {})) >= 2
    assert warnings_contain(caught, "FooBar")

    stored_ids = ingest.collection.get()["ids"]
    assert len(stored_ids) == total

    # assets 目录与根目录零散文件都不入库
    assert ingest.collection.get(where={"source_category": "assets"})["ids"] == []
    sources = ingest.collection.get()["metadatas"]
    assert all("assets" not in meta["source"] for meta in sources)
    assert all("README.md" not in meta["source"] for meta in sources)


def test_ingest_concept_metadata_fields(tmp_path):
    concept_root = _build_concept_tree(tmp_path)
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    ingest.ingest_concept_dir(str(concept_root))

    result = ingest.collection.get(where={"source": "concept/Concepts/agent-definition.md"})
    assert len(result["ids"]) == 1
    meta = result["metadatas"][0]

    assert meta["layer"] == "concept"
    assert meta["category"] == "concepts"
    assert meta["source"] == "concept/Concepts/agent-definition.md"
    assert meta["title"] == "Agent Definition"
    assert meta["curated"] is True
    assert meta["chunk_index"] == 0
    assert meta["section_title"] == "Agent Definition"
    assert meta["chunking_version"] == "v1"
    assert meta["source_category"] == "Concepts"
    # 本期不写飞书对应关系：None 会撑爆 ChromaDB metadata 类型
    assert "feishu_ref" not in meta
    # 概念层 metadata 共 9 个键（论文层多 arxiv_id 共 10 个）
    assert len(meta) == 9
    assert "Agent 是能感知环境" in result["documents"][0]

    memory_result = ingest.collection.get(where={"source": "concept/AgentMemory/memory-overview.md"})
    memory_metas = memory_result["metadatas"]
    assert len(memory_metas) >= 2
    assert {meta["category"] for meta in memory_metas} == {"memory"}
    assert {meta["source_category"] for meta in memory_metas} == {"AgentMemory"}
    assert [meta["chunk_index"] for meta in memory_metas] == list(range(len(memory_metas)))

    # 缺 title 时回落到文件名（去 .md）
    untitled = ingest.collection.get(where={"source": "concept/Concepts/no-title-doc.md"})
    assert untitled["metadatas"][0]["title"] == "no-title-doc"


def test_concept_chunk_id_rule(tmp_path):
    concept_root = _build_concept_tree(tmp_path)
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    ingest.ingest_concept_dir(str(concept_root))

    source = "concept/Concepts/agent-definition.md"
    expected_id = f"concept-{hashlib.md5(source.encode('utf-8')).hexdigest()[:12]}-0"
    result = ingest.collection.get(where={"source": source})
    assert result["ids"] == [expected_id]

    long_source = "concept/AgentMemory/memory-overview.md"
    long_prefix = f"concept-{hashlib.md5(long_source.encode('utf-8')).hexdigest()[:12]}-"
    long_ids = ingest.collection.get(where={"source": long_source})["ids"]
    assert all(chunk_id.startswith(long_prefix) for chunk_id in long_ids)


def test_unmapped_dir_falls_back_to_architecture(tmp_path):
    concept_root = tmp_path / "concept"
    _write(concept_root / "FooBar" / "foo.md", "---\ntitle: Foo Doc\n---\n# Foo\n\n兜底内容。\n")
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    with pytest.warns(UserWarning):
        total = ingest.ingest_concept_dir(str(concept_root))

    assert total == 1
    result = ingest.collection.get(where={"source": "concept/FooBar/foo.md"})
    assert result["metadatas"][0]["category"] == "architecture"
    assert result["metadatas"][0]["source_category"] == "FooBar"


# ── 5. 幂等 ──────────────────────────────────────────────────────

def test_reingest_is_idempotent(tmp_path):
    concept_root = _build_concept_tree(tmp_path)
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        first = ingest.ingest_concept_dir(str(concept_root))
        second = ingest.ingest_concept_dir(str(concept_root))

    assert first == second
    assert len(ingest.collection.get()["ids"]) == first


# ── 6. 删除 ──────────────────────────────────────────────────────

def test_delete_by_source_and_layer(tmp_path):
    concept_root = _build_concept_tree(tmp_path)
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        total = ingest.ingest_concept_dir(str(concept_root))

    source = "concept/Concepts/agent-definition.md"
    assert ingest.delete_by_source(source) == 1
    assert ingest.collection.get(where={"source": source})["ids"] == []
    assert ingest.delete_by_source(source) == 0
    assert ingest.delete_by_source("不存在的 source") == 0

    assert ingest.delete_by_layer("concept") == total - 1
    assert ingest.collection.get()["ids"] == []
    assert ingest.delete_by_layer("concept") == 0


# ── 7. 论文层 ────────────────────────────────────────────────────

PAPERS = [
    {
        "arxiv_id": "2401.00001",
        "title": "Starter Paper",
        "category": "architectures",
        "is_starter_kit": "True",
        "abstract": "A" * 80,
    },
    {
        "arxiv_id": "2401.00002",
        "title": "Normal Paper",
        "category": "applications",
        "is_starter_kit": "False",
        "abstract": "B" * 80,
    },
    {
        "arxiv_id": "2401.00003",
        "title": "Empty Abstract Paper",
        "category": "planning",
        "is_starter_kit": "False",
        "abstract": "   ",
    },
]


def test_ingest_papers_metadata_and_skip(tmp_path):
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    assert ingest.ingest_papers(PAPERS) == 2
    result = ingest.collection.get(where={"layer": "curated-paper"})

    assert sorted(result["ids"]) == ["curated-2401.00001", "curated-2401.00002"]
    by_source = {meta["source"]: meta for meta in result["metadatas"]}

    starter = by_source["2401.00001"]
    assert starter["category"] == "architecture"
    assert starter["curated"] is True
    assert starter["layer"] == "curated-paper"
    assert starter["title"] == "Starter Paper"
    assert starter["chunk_index"] == 0
    assert starter["section_title"] == ""
    assert starter["chunking_version"] == "v1"
    assert starter["source_category"] == "architectures"
    assert starter["arxiv_id"] == "2401.00001"
    assert len(starter) == 10  # 论文层比概念层多 arxiv_id

    normal = by_source["2401.00002"]
    assert normal["category"] == "applications-other"
    assert normal["curated"] is False
    assert normal["source_category"] == "applications"

    # abstract 为空串的跳过，未写入任何层
    assert ingest.collection.get(where={"source": "2401.00003"})["ids"] == []

    # 正文为 "# 标题 + 空行 + abstract"
    documents = ingest.collection.get(where={"source": "2401.00001"})["documents"]
    assert documents[0].startswith("# Starter Paper")
    assert "A" * 80 in documents[0]


def test_papers_reingest_and_delete_by_layer(tmp_path):
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    assert ingest.ingest_papers(PAPERS) == 2
    assert ingest.ingest_papers(PAPERS) == 2  # 同 arxiv_id 先删后插，不翻倍
    assert len(ingest.collection.get()["ids"]) == 2

    assert ingest.delete_by_layer("curated-paper") == 2
    assert ingest.collection.get()["ids"] == []


def test_ingest_papers_tracked_layer_prefix(tmp_path):
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    tracked = [dict(PAPERS[0], is_starter_kit=False)]

    assert ingest.ingest_papers(tracked, layer="tracked-paper") == 1
    result = ingest.collection.get(where={"layer": "tracked-paper"})
    assert result["ids"] == ["tracked-2401.00001"]
    assert result["metadatas"][0]["layer"] == "tracked-paper"
    assert result["metadatas"][0]["curated"] is False


def test_ingest_papers_bool_is_starter_kit(tmp_path):
    """当前 papers_with_abstract.json 的 is_starter_kit 是真布尔，需与字符串形态同样识别。"""
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    papers = [
        dict(PAPERS[0], arxiv_id="2401.00010", is_starter_kit=True),
        dict(PAPERS[1], arxiv_id="2401.00011", is_starter_kit=False),
    ]

    assert ingest.ingest_papers(papers) == 2
    curated_flags = {
        meta["source"]: meta["curated"]
        for meta in ingest.collection.get(where={"layer": "curated-paper"})["metadatas"]
    }
    assert curated_flags == {"2401.00010": True, "2401.00011": False}


def test_ingest_papers_rejects_unknown_layer(tmp_path):
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())
    with pytest.raises(ValueError):
        ingest.ingest_papers(PAPERS, layer="bad-layer")


def warnings_contain(caught, keyword: str) -> bool:
    """catch_warnings 结果里是否有包含关键词的告警。"""
    return any(keyword in str(item.message) for item in caught)


# ── 9. 入库进度参数（R3 新增，默认行为零变化） ───────────────────────

def test_progress_every_zero_keeps_stderr_silent(tmp_path, capsys):
    """progress_every=0（默认）时不打进度，stderr 为空。"""
    concept_root = tmp_path / "concept"
    _write(concept_root / "Concepts" / "a.md", "# A\n\n概念层文档正文。\n")
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    assert ingest.ingest_concept_dir(str(concept_root)) == 1
    assert ingest.ingest_concept_dir(str(concept_root), progress_every=0) == 1
    assert ingest.ingest_papers(PAPERS, progress_every=0) == 2

    assert capsys.readouterr().err == ""


def test_progress_every_one_prints_progress_lines(tmp_path, capsys):
    """progress_every=1 时每个源单位打一行到 stderr；论文行含跳过计数。"""
    concept_root = tmp_path / "concept"
    _write(concept_root / "Concepts" / "a.md", "# A\n\n概念层文档正文。\n")
    _write(concept_root / "Concepts" / "b.md", "# B\n\n第二篇概念文档正文。\n")
    ingest = RAGIngest(_config(tmp_path), embedder=FakeEmbedder())

    assert ingest.ingest_concept_dir(str(concept_root), progress_every=1) == 2
    concept_err = capsys.readouterr().err
    assert "已处理 1 个文件，累计 1 chunk" in concept_err
    assert "已处理 2 个文件，累计 2 chunk" in concept_err

    # PAPERS 3 条：2 条入库、1 条 abstract 过短跳过
    assert ingest.ingest_papers(PAPERS, progress_every=1) == 2
    paper_err = capsys.readouterr().err
    assert "跳过" in paper_err
    assert "已处理 3/3 条，入库 2 chunk，跳过 1 条" in paper_err


