# RAG 检索器自测 - 真实临时 ChromaDB + 方向可控假 embedder（零真实 API）
"""RAGStore 零成本自测：ChromaDB 用 tmp_path 下的真实嵌入式实例，嵌入走方向可控假 embedder。

假 embedder 设计：每个关键词一个单位基向量（最后一维为"无关键词"兜底方向），
文本的向量 = 命中关键词的基向量之和归一化。测试据此构造"共享关键词 → 余弦最近"，
并把关键词只写进 title metadata（不进正文）来构造"关键词强、向量弱"的对照。

覆盖：
1. 基础 top1 相关命中；
2. layers（$in）/ category / 两者叠加过滤；
3. 层权重（concept 2.0 > tracked 1.0）与 curated_bonus（1.2）；
4. 融合排序：向量强（0.6 权重）压过关键词强（0.4 权重）；
5. feishu_ref_bonus（1.3）；
6. 结果字段完整（11 键）、score 降序、top_k 截断、空 query / 空库返回 []。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_store.py -q
"""
from __future__ import annotations

import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from kb.rag.ingest import RAGIngest  # noqa: E402
from kb.rag.store import RAGStore  # noqa: E402

KEYWORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"]
RESULT_FIELDS = {
    "id",
    "content",
    "score",
    "layer",
    "category",
    "source",
    "title",
    "section_title",
    "chunk_index",
    "vector_sim",
    "keyword_norm",
}


class DirectionalEmbedder:
    """方向可控假嵌入器：关键词 → 单位基向量，文本向量 = 命中基向量之和归一化。

    无任何关键词命中时返回兜底方向（与所有关键词基向量正交），
    因此"正文不含关键词的 chunk"与"含关键词的 query"余弦相似度为 0。
    """

    def __init__(self, keywords: list[str]):
        self.keywords = list(keywords)
        self.dim = len(self.keywords) + 1
        self.fallback_index = len(self.keywords)
        self._basis = {
            keyword: self._unit(index) for index, keyword in enumerate(self.keywords)
        }

    def _unit(self, index: int) -> list[float]:
        vector = [0.0] * self.dim
        vector[index] = 1.0
        return vector

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vector(text)

    def _vector(self, text: str) -> list[float]:
        text = str(text)
        vector = [0.0] * self.dim
        hit = False
        for keyword, basis in self._basis.items():
            if keyword in text:
                hit = True
                for index in range(self.dim):
                    vector[index] += basis[index]
        if not hit:
            vector = self._unit(self.fallback_index)
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def _config(tmp_path: Path) -> dict:
    return {
        "domain_kb": {
            "chroma_path": str(tmp_path / "chroma"),
            "embedding": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-Embedding-8B",
                "api_key_env": "RAG_STORE_TEST_KEY",
                "api_base": "https://api.siliconflow.cn/v1",
                "dimensions": 4096,
                "batch_size": 32,
            },
            "retrieval": {
                "top_k": 5,
                "keyword_weight": 0.4,
                "vector_weight": 0.6,
                "layer_weights": {
                    "concept": 2.0,
                    "curated-paper": 1.5,
                    "tracked-paper": 1.0,
                },
                "curated_bonus": 1.2,
                "feishu_ref_bonus": 1.3,
            },
        }
    }


def _add_chunk(
    ingest: RAGIngest,
    embedder: DirectionalEmbedder,
    chunk_id: str,
    content: str,
    title: str = "",
    layer: str = "concept",
    category: str = "concepts",
    curated: bool = False,
    feishu_ref: str | None = None,
) -> None:
    """直接往 collection 写一条人造 chunk（metadata 可控，便于构造对照）。"""
    metadata = {
        "layer": layer,
        "category": category,
        "source": chunk_id,
        "title": title,
        "curated": curated,
        "chunk_index": 0,
        "section_title": "",
        "chunking_version": "v1",
        "source_category": "test",
    }
    if feishu_ref is not None:
        metadata["feishu_ref"] = feishu_ref
    ingest.collection.add(
        ids=[chunk_id],
        embeddings=embedder.embed([content]),
        documents=[content],
        metadatas=[metadata],
    )


def _setup(tmp_path: Path) -> tuple[RAGIngest, RAGStore, DirectionalEmbedder]:
    """共享同一 chroma_path 的入库器与检索器（同一假 embedder 保证向量维度一致）。"""
    embedder = DirectionalEmbedder(KEYWORDS)
    config = _config(tmp_path)
    ingest = RAGIngest(config, embedder=embedder)
    store = RAGStore(config, embedder=embedder)
    return ingest, store, embedder


# ── 1. 基础相关命中 ──────────────────────────────────────────────

def test_top1_matches_query_keyword(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "chunk-alpha", "alpha 的正文内容", title="Alpha 文档")
    _add_chunk(ingest, embedder, "chunk-beta", "beta 的正文内容", title="Beta 文档")
    _add_chunk(ingest, embedder, "chunk-gamma", "gamma 的正文内容", title="Gamma 文档")

    results = store.search("alpha")

    assert results[0]["id"] == "chunk-alpha"
    assert results[0]["vector_sim"] == 1.0
    assert results[0]["keyword_norm"] == 1.0


# ── 2. metadata 过滤 ─────────────────────────────────────────────

def test_layers_and_category_filters(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "c-alpha", "alpha 概念内容", layer="concept", category="concepts")
    _add_chunk(
        ingest, embedder, "p-alpha", "alpha 精选内容", layer="curated-paper", category="architecture"
    )
    _add_chunk(
        ingest, embedder, "t-alpha", "alpha 追踪内容", layer="tracked-paper", category="planning"
    )

    by_layers = store.search("alpha", layers=["curated-paper", "tracked-paper"])
    assert [item["id"] for item in by_layers] == ["p-alpha", "t-alpha"] or sorted(
        item["id"] for item in by_layers
    ) == ["p-alpha", "t-alpha"]
    assert {item["layer"] for item in by_layers} == {"curated-paper", "tracked-paper"}

    by_category = store.search("alpha", category="planning")
    assert [item["id"] for item in by_category] == ["t-alpha"]
    assert by_category[0]["category"] == "planning"

    both = store.search("alpha", layers=["tracked-paper"], category="planning")
    assert [item["id"] for item in both] == ["t-alpha"]

    empty = store.search("alpha", layers=["concept"], category="planning")
    assert empty == []


# ── 3. 层权重与 curated bonus ───────────────────────────────────

def test_layer_weight_and_curated_bonus(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "c-gamma", "gamma 概念内容", layer="concept")
    _add_chunk(ingest, embedder, "t-gamma", "gamma 追踪内容", layer="tracked-paper")

    results = store.search("gamma")
    assert results[0]["id"] == "c-gamma"
    assert results[0]["score"] == round(results[1]["score"] * 2.0, 4)

    _add_chunk(
        ingest, embedder, "p-curated", "delta 精选内容", layer="curated-paper", curated=True
    )
    _add_chunk(
        ingest, embedder, "p-plain", "delta 精选内容", layer="curated-paper", curated=False
    )

    curated_results = store.search("delta")
    assert curated_results[0]["id"] == "p-curated"
    assert curated_results[1]["id"] == "p-plain"
    assert curated_results[0]["score"] == round(curated_results[1]["score"] * 1.2, 4)


# ── 4. 融合排序：向量强 vs 关键词强 ─────────────────────────────

def test_hybrid_fusion_prefers_strong_vector(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    # 关键词强 / 向量弱：关键词只出现在 title（×3 计入关键词分），正文无关键词 → 向量兜底方向
    _add_chunk(
        ingest,
        embedder,
        "kw-strong",
        "正文不含任何关键词",
        title="epsilon epsilon epsilon",
        layer="tracked-paper",
    )
    # 向量强 / 关键词弱：正文含关键词一次（向量最近、关键词分仅正文 ×1），title 无关键词
    _add_chunk(
        ingest,
        embedder,
        "vec-strong",
        "epsilon 正文内容",
        title="无关标题",
        layer="tracked-paper",
    )

    results = store.search("epsilon")

    assert results[0]["id"] == "vec-strong"
    assert results[1]["id"] == "kw-strong"
    # 关键词原始分：kw-strong = title 3 次 ×3 = 9（正文无命中）；vec-strong = 正文 1 次 ×1 = 1
    # 归一化后 1/9 vs 9/9=1.0
    assert results[0]["keyword_norm"] == round(1 / 9, 4)
    assert results[0]["vector_sim"] == 1.0
    assert results[1]["keyword_norm"] == 1.0
    assert results[1]["vector_sim"] == 0.0
    # 0.4×1/9 + 0.6×1.0 = 0.6444 > 0.4×1.0 + 0.6×0 = 0.4 → 向量强的一条在前
    assert results[0]["score"] == round(0.4 / 9 + 0.6, 4)
    assert results[1]["score"] == 0.4


# ── 5. feishu_ref bonus ─────────────────────────────────────────

def test_feishu_ref_bonus(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "zeta-ref", "zeta 内容", feishu_ref="ks_x")
    _add_chunk(ingest, embedder, "zeta-plain", "zeta 内容")

    without = store.search("zeta")
    assert without[0]["score"] == without[1]["score"]  # 同分对照

    with_refs = store.search("zeta", feishu_refs=["ks_x"])
    assert with_refs[0]["id"] == "zeta-ref"
    assert with_refs[0]["score"] == round(with_refs[1]["score"] * 1.3, 4)

    other_refs = store.search("zeta", feishu_refs=["ks_other"])
    assert other_refs[0]["score"] == other_refs[1]["score"]


# ── 6. 结果字段、排序、边界 ──────────────────────────────────────

def test_result_fields_order_and_top_k(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    for index in range(4):
        _add_chunk(
            ingest,
            embedder,
            f"chunk-{index}",
            f"alpha 内容 {index}",
            title=f"Alpha 文档 {index}",
        )

    results = store.search("alpha", top_k=3)

    assert len(results) == 3
    assert set(results[0].keys()) == RESULT_FIELDS
    scores = [item["score"] for item in results]
    assert scores == sorted(scores, reverse=True)
    assert results[0]["source"] == results[0]["id"]
    assert results[0]["chunk_index"] == 0
    assert results[0]["section_title"] == ""

    assert len(store.search("alpha", top_k=1)) == 1


def test_empty_query_and_empty_collection(tmp_path):
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "chunk-alpha", "alpha 内容")

    assert store.search("") == []
    assert store.search("   ") == []

    empty_root = tmp_path / "empty"
    empty_root.mkdir()
    empty_store = RAGStore(_config(empty_root), embedder=embedder)
    assert empty_store.search("alpha") == []


# ── 7. 检索条例：同源去重 + 相关性阈值 ────────────────


def _add_chunk_with_source(
    ingest: RAGIngest,
    embedder: DirectionalEmbedder,
    chunk_id: str,
    content: str,
    source: str,
    title: str = "",
    chunk_index: int = 0,
) -> None:
    """写一条 chunk，source 与 chunk_id 分离（用于构造「同一文档多个片段」）。"""
    metadata = {
        "layer": "concept",
        "category": "concepts",
        "source": source,
        "title": title,
        "curated": False,
        "chunk_index": chunk_index,
        "section_title": "",
        "chunking_version": "v1",
        "source_category": "test",
    }
    ingest.collection.add(
        ids=[chunk_id],
        embeddings=embedder.embed([content]),
        documents=[content],
        metadatas=[metadata],
    )


def test_same_source_dedup_keeps_one(tmp_path):
    """同源去重（默认 max_per_source=1）：同一文档的多个片段只保留最高分一条。"""
    ingest, store, embedder = _setup(tmp_path)
    # 同一份文档 doc-A 的 3 个片段，都命中 alpha
    for index in range(3):
        _add_chunk_with_source(
            ingest, embedder, f"a-{index}", f"alpha 片段 {index}", source="doc-A", chunk_index=index
        )
    # 另一份文档 doc-B 的 1 个片段，也命中 alpha
    _add_chunk_with_source(ingest, embedder, "b-0", "alpha 片段 B", source="doc-B")

    results = store.search("alpha", top_k=5)

    sources = [item["source"] for item in results]
    assert sources.count("doc-A") == 1, f"doc-A 应只保留一条，实际 {sources}"
    assert "doc-B" in sources
    # 同分下顺序按召回序，不固定：真正的契约是「来源不重复」
    assert len(sources) == len(set(sources)) == 2, f"每个来源最多一条，实际 {sources}"


def test_max_per_source_configurable(tmp_path):
    """同源上限可配：max_per_source=2 时同一文档允许留两条。"""
    ingest, store, embedder = _setup(tmp_path)
    config = _config(tmp_path)
    config["domain_kb"]["retrieval"]["max_per_source"] = 2
    store2 = RAGStore(config, embedder=embedder)
    for index in range(3):
        _add_chunk_with_source(
            ingest, embedder, f"c-{index}", f"alpha 片段 {index}", source="doc-C", chunk_index=index
        )

    results = store2.search("alpha", top_k=5)

    assert [item["source"] for item in results].count("doc-C") == 2


def test_min_vector_sim_filters_weak(tmp_path):
    """相关性阈值：vector_sim 低于阈值的结果被过滤掉。"""
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "strong", "alpha 正文", title="Alpha")  # vector_sim = 1.0
    _add_chunk(ingest, embedder, "weak", "正文不含任何关键词", title="无关")  # vector_sim = 0.0

    # 默认阈值 0 = 不过滤，两条都在
    assert len(store.search("alpha", top_k=5)) == 2

    config = _config(tmp_path)
    config["domain_kb"]["retrieval"]["min_vector_sim"] = 0.5
    store2 = RAGStore(config, embedder=embedder)
    filtered = store2.search("alpha", top_k=5)

    assert [item["id"] for item in filtered] == ["strong"]


def test_search_reads_new_params_defaults(tmp_path):
    """未配置新参数时用默认值（0.0 不过滤 / 1 去重），不报错。"""
    ingest, store, embedder = _setup(tmp_path)
    _add_chunk(ingest, embedder, "x-1", "beta 正文")

    assert store.min_vector_sim == 0.0
    assert store.max_per_source == 1
    assert len(store.search("beta")) == 1


