# RAG 双路召回自测 - 真实临时 ChromaDB + 方向可控假 embedder（零真实 API）
"""lexical.py + store.py 双路召回 / RRF / 查询输出契约的零成本自测。

假 embedder（与 test_rag_store 同一套思路）：每个关键词一个单位基向量，文本向量 =
命中基向量之和归一化，无命中回兜底方向。测试用它构造「向量路查不到、词法路必须命中」
的对照：把精确型号串（如 Qwen3-Embedding-8B）写进正文、同时让语料里塞满命中关键词的
干扰块，型号块向量相似度 0，被挤出向量候选；v1 的单路（向量候选内补关键词分）必然漏它，
v2 的词法全库召回必须命中。

覆盖：
1. 纯词法命中（v1 必漏那类）：型号块被向量路挤出候选，双路版命中、单路版复现漏检；
2. RRF 融合排序与公式：两路都命中的排在只单路命中之前，分数 = Σ 1/(k+rank)；
3. 质量乘数：user_input 1.5 / curated 1.2 / auto 1.0，缺 quality 键按 curated 布尔映射；
4. feishu_ref 命中加成 1.3；
5. 同源去重（默认每 source 最多 1 条，词法独中的候选同样受管）；
6. is_stale 软删过滤；
7. 查询输出契约：每条带 chunk_type / parent_id / heading_path；子块附 parent_content
   （父块全文），父块自身不附；
8. 词法索引自身：分词（英文数字原样）、userdict 缺失不报错、接口与落盘读回、纯函数性。

运行：
  PYTHONPATH=src python -m pytest tests/test_rag_lexical.py -q
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
from kb.rag.lexical import (  # noqa: E402
    LexicalIndex,
    chunk_index_text,
    default_userdict_path,
    load_userdict,
    tokenize,
)
from kb.rag.store import RAGStore  # noqa: E402

KEYWORDS = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta", "eta", "theta"]
# 精确型号串：v1 的向量候选内补关键词分永远看不到它
MODEL_STRING = "Qwen3-Embedding-8B"
# 检索结果必备字段（v1 键保留 + v2 新增：融合明细 + 查询输出契约）
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
    "bm25_rank",
    "vector_rank",
    "rrf_score",
    "chunk_type",
    "parent_id",
    "heading_path",
}


class DirectionalEmbedder:
    """方向可控假嵌入器：关键词 → 单位基向量，文本向量 = 命中基向量之和归一化。"""

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


def _config(tmp_path: Path, **retrieval_overrides) -> dict:
    retrieval = {
        "top_k": 5,
        "feishu_ref_bonus": 1.3,
        "min_vector_sim": 0.0,
        "max_per_source": 1,
    }
    retrieval.update(retrieval_overrides)
    return {
        "domain_kb": {
            "chroma_path": str(tmp_path / "chroma"),
            "embedding": {
                "provider": "siliconflow",
                "model": MODEL_STRING,
                "api_key_env": "RAG_LEXICAL_TEST_KEY",
                "api_base": "https://api.siliconflow.cn/v1",
                "dimensions": 4096,
                "batch_size": 32,
            },
            "retrieval": retrieval,
        }
    }


def _setup(tmp_path: Path, **overrides):
    """共享同一 chroma_path 的入库器与检索器（同一假 embedder 保证向量维度一致）。"""
    embedder = DirectionalEmbedder(KEYWORDS)
    config = _config(tmp_path, **overrides)
    ingest = RAGIngest(config, embedder=embedder)
    store = RAGStore(config, embedder=embedder)
    return ingest, store, embedder


def _add(
    ingest: RAGIngest,
    embedder: DirectionalEmbedder,
    chunk_id: str,
    content: str,
    source: str | None = None,
    title: str = "",
    quality: str | None = None,
    curated: bool | None = None,
    feishu_ref: str | None = None,
    chunk_type: str = "section",
    parent_id: str = "",
    heading_path: str = "",
    section_title: str = "",
    chunk_index: int = 0,
    is_stale=None,
    layer: str = "concept",
    category: str = "concepts",
) -> None:
    """直接往 collection 写一条人造 chunk（metadata 可控，便于构造对照）。"""
    metadata: dict = {
        "layer": layer,
        "category": category,
        "source": source if source is not None else chunk_id,
        "title": title,
        "section_title": section_title,
        "chunk_index": chunk_index,
        "heading_path": heading_path,
        "chunk_type": chunk_type,
        "parent_id": parent_id,
        "chunking_version": "v3",
        "source_category": "test",
    }
    # curated / quality / feishu_ref / is_stale 只在显式给出时写入（None 不落库）
    if quality is not None:
        metadata["quality"] = quality
    if curated is not None:
        metadata["curated"] = curated
    if feishu_ref is not None:
        metadata["feishu_ref"] = feishu_ref
    if is_stale is not None:
        metadata["is_stale"] = is_stale
    ingest.collection.add(
        ids=[chunk_id],
        embeddings=embedder.embed([content]),
        documents=[content],
        metadatas=[metadata],
    )


def _expected_rrf(item: dict, rrf_k: float) -> float:
    """按返回的 bm25_rank / vector_rank 复算 RRF 分（分数 = Σ 1/(k + rank)）。"""
    rrf = 0.0
    for rank in (item.get("vector_rank"), item.get("bm25_rank")):
        if rank:
            rrf += 1.0 / (rrf_k + float(rank))
    return rrf


def _find(results: list[dict], chunk_id: str) -> dict | None:
    for item in results:
        if item["id"] == chunk_id:
            return item
    return None


# ── 1. 纯词法命中：v1 的单路必然漏掉的那类 ────────────────────────


def test_pure_lexical_hit_recovers_what_vector_path_misses(tmp_path):
    """型号块向量相似度 0，被 51 个干扰块挤出向量 top 50；词法路必须把它捞回来。

    同时用 lexical_top_n=0 关掉词法路复现 v1 单路行为（对照：同样查询下类型号块查不到）。
    """
    ingest, store, embedder = _setup(tmp_path)
    # 51 个干扰块：都命中关键词 zeta（向量相似度 1.0），把向量候选塞满
    for index in range(51):
        _add(
            ingest,
            embedder,
            f"noise-{index}",
            f"zeta 干扰内容 {index}",
            source=f"noise/doc-{index}.md",
        )
    # 目标块：正文只有精确型号串，没有任何关键词 → 向量兜底方向、相似度 0
    _add(
        ingest,
        embedder,
        "target-model",
        f"推荐使用 {MODEL_STRING} 做中文检索，维度 4096。",
        source="concept/ReferenceArchitecture/model.md",
        heading_path="模型选型 > 嵌入模型",
    )

    results = store.search(f"zeta {MODEL_STRING}", top_k=60)

    target = _find(results, "target-model")
    assert target is not None, "纯词法命中被漏掉：双路召回没把它捞回来"
    assert target["vector_rank"] is None, "该块不应出现在向量候选里（构造前提）"
    assert target["bm25_rank"] is not None and target["bm25_rank"] >= 1
    assert target["rrf_score"] > 0
    # 干扰块确实占据了向量候选（说明向量 top N 是真被塞满的，不是没召回）
    assert any(
        item["vector_rank"] is not None and item["bm25_rank"] is None for item in results
    )
    assert max(item["vector_rank"] for item in results if item["vector_rank"]) == 50

    # 对照：关掉词法路（lexical_top_n=0）＝ v1 「只有向量路」的行为，型号块必然查不到
    _, vector_only_store, _ = _setup(tmp_path, lexical_top_n=0)
    vector_only = vector_only_store.search(f"zeta {MODEL_STRING}", top_k=60)
    assert _find(vector_only, "target-model") is None, "单路对照应复现漏检"


# ── 2. RRF 融合排序与公式 ────────────────────────────────────────


def test_rrf_ranks_two_path_hit_above_single_path_hit(tmp_path):
    """两路都命中 > 只单路命中；且分数严格等于 Σ 1/(k+rank)。"""
    ingest, store, embedder = _setup(tmp_path, rrf_k=10)
    # A：向量命中（"zetazeta" 含子串 zeta）但词法不命中（分词出一个 zetazeta token）
    _add(ingest, embedder, "vec-only", "zetazeta 的正文", source="doc-A")
    # B：两路都命中（zeta 既命中关键词、又能被 jieba 切出与查询一致的 token）
    _add(ingest, embedder, "both-paths", "zeta 的正文", source="doc-B")

    results = store.search("zeta", top_k=5)

    first, second = results[0], results[1]
    assert first["id"] == "both-paths"
    assert second["id"] == "vec-only"
    # 两路命中：两个 rank 都在
    assert first["bm25_rank"] == 1 and first["vector_rank"] in (1, 2)
    # 单路命中：词法 rank 缺省
    assert second["bm25_rank"] is None and second["vector_rank"] in (1, 2)
    # 公式核对：分数 = Σ 1/(k+rank)，浮点按返回精度比对
    for item in results:
        assert item["rrf_score"] == round(_expected_rrf(item, store.rrf_k), 6)
        assert item["score"] == round(item["rrf_score"], 4)
    assert first["rrf_score"] > second["rrf_score"]
    # k 生效：两路 rank1 的分 = 2/(10+1)，单路 rank1 的分 = 1/(10+1)
    if first["vector_rank"] == 1:
        assert first["rrf_score"] == round(2 / 11, 6)


def test_rrf_k_is_configurable(tmp_path):
    """rrf_k 可配：k 越小，名次差异越被放大（同样两路命中的相对差距变大）。"""
    ingest, store, embedder = _setup(tmp_path, rrf_k=1)
    _add(ingest, embedder, "vec-only", "zetazeta 的正文", source="doc-A")
    _add(ingest, embedder, "both-paths", "zeta 的正文", source="doc-B")

    results = store.search("zeta", top_k=5)

    assert store.rrf_k == 1
    for item in results:
        assert item["rrf_score"] == round(_expected_rrf(item, 1), 6)


# ── 3. 质量乘数 ──────────────────────────────────────────────────


def test_quality_multipliers_and_legacy_curated_fallback(tmp_path):
    """质量乘数：user_input 1.5 / curated 1.2 / auto 1.0；缺 quality 键按 curated 布尔映射。"""
    ingest, store, embedder = _setup(tmp_path)
    cases = [
        ("q-user", "alpha 内容", {"quality": "user_input"}, 1.5),
        ("q-curated", "beta 内容", {"quality": "curated"}, 1.2),
        ("q-auto", "gamma 内容", {"quality": "auto"}, 1.0),
        # 存量 chunk 没有 quality 键：curated=True → 1.2，curated=False → 1.0
        ("q-legacy-true", "delta 内容", {"curated": True}, 1.2),
        ("q-legacy-false", "epsilon 内容", {"curated": False}, 1.0),
    ]
    queries = {}
    for chunk_id, content, extra, multiplier in cases:
        _add(ingest, embedder, chunk_id, content, source=f"doc/{chunk_id}.md", **extra)
        queries[chunk_id] = (content.split()[0], multiplier)

    for chunk_id, (keyword, multiplier) in queries.items():
        results = store.search(keyword, top_k=5)
        item = _find(results, chunk_id)
        assert item is not None, f"{chunk_id} 未命中"
        expected = round(_expected_rrf(item, store.rrf_k) * multiplier, 4)
        assert item["score"] == expected, (
            f"{chunk_id} 质量乘数应为 {multiplier}：score={item['score']} 期望 {expected}"
        )

    # user_input 档排在同分的 auto 档之前
    results = store.search("alpha", top_k=5)
    assert results[0]["id"] == "q-user"


# ── 4. feishu_ref 加成 ──────────────────────────────────────────


def test_feishu_ref_bonus_applies_after_rrf(tmp_path):
    """命中用户飞书标注的条目 ×1.3；未命中列表里的不打折也不加成。"""
    ingest, store, embedder = _setup(tmp_path)
    _add(ingest, embedder, "ref-chunk", "zeta 内容", source="doc-ref", feishu_ref="ks_x")
    _add(ingest, embedder, "plain-chunk", "zeta 内容", source="doc-plain")

    without = store.search("zeta", top_k=5)
    for item in without:
        assert item["score"] == round(_expected_rrf(item, store.rrf_k), 4)

    with_refs = store.search("zeta", top_k=5, feishu_refs=["ks_x"])
    ref_item = _find(with_refs, "ref-chunk")
    plain_item = _find(with_refs, "plain-chunk")
    assert ref_item["score"] == round(_expected_rrf(ref_item, store.rrf_k) * 1.3, 4)
    assert plain_item["score"] == round(_expected_rrf(plain_item, store.rrf_k), 4)
    assert ref_item["score"] > plain_item["score"]

    # 别的 ref 列表不影响任何条目
    other = store.search("zeta", top_k=5, feishu_refs=["ks_other"])
    for item in other:
        assert item["score"] == round(_expected_rrf(item, store.rrf_k), 4)


# ── 5. 同源去重（含词法独中的候选） ──────────────────────────────


def test_same_source_dedup_applies_to_lexical_only_candidates(tmp_path):
    """每 source 最多 1 条：词法独中的候选同样受去重约束（v1 只对向量候选去重）。"""
    ingest, store, embedder = _setup(tmp_path, vector_top_n=0)  # 只留词法路，隔离变量
    for index in range(2):
        _add(
            ingest,
            embedder,
            f"dup-{index}",
            f"{MODEL_STRING} 的第 {index} 段说明",
            source="doc-A",
            chunk_index=index,
        )
    _add(ingest, embedder, "other", f"{MODEL_STRING} 另一份文档", source="doc-B")

    results = store.search(MODEL_STRING, top_k=5)

    sources = [item["source"] for item in results]
    assert sources.count("doc-A") == 1, f"doc-A 应只保留一条，实际 {sources}"
    assert "doc-B" in sources
    assert len(sources) == len(set(sources)) == 2

    # 上限可配：max_per_source=2 时 doc-A 允许两条
    _, store2, _ = _setup(tmp_path, vector_top_n=0, max_per_source=2)
    sources2 = [item["source"] for item in store2.search(MODEL_STRING, top_k=5)]
    assert sources2.count("doc-A") == 2


# ── 6. is_stale 软删过滤 ────────────────────────────────────────


def test_is_stale_chunks_are_filtered(tmp_path):
    """is_stale 为真的条目（布尔或字符串形态）都不进结果。"""
    ingest, store, embedder = _setup(tmp_path)
    _add(ingest, embedder, "keep", "zeta 有效内容", source="doc-keep")
    _add(ingest, embedder, "stale-bool", "zeta 已软删内容", source="doc-stale-1", is_stale=True)
    _add(
        ingest,
        embedder,
        "stale-str",
        "zeta 已软删内容",
        source="doc-stale-2",
        is_stale="True",
    )
    _add(
        ingest,
        embedder,
        "not-stale",
        "zeta 未软删内容",
        source="doc-false",
        is_stale=False,
    )

    ids = [item["id"] for item in store.search("zeta", top_k=5)]

    assert "keep" in ids and "not-stale" in ids
    assert "stale-bool" not in ids and "stale-str" not in ids


# ── 7. 查询输出契约：chunk_type / parent_id / heading_path / parent_content ──


def test_query_output_contract_attaches_parent_content_only_to_children(tmp_path):
    """子块附带父块全文；父块自身不附；每条都带三个契约字段。"""
    ingest, store, embedder = _setup(tmp_path, max_per_source=5)
    source = "concept/Concepts/parent-child.md"
    parent_content = f"父块整章全文：{MODEL_STRING} 的选型与部署总览。"
    child_content = f"子块细节：{MODEL_STRING} 的部署步骤与参数。"
    _add(
        ingest,
        embedder,
        "pc-parent",
        parent_content,
        source=source,
        title="嵌入模型",
        chunk_type="parent",
        parent_id="",
        heading_path="嵌入模型",
        section_title="嵌入模型",
        chunk_index=1,
    )
    _add(
        ingest,
        embedder,
        "pc-child",
        child_content,
        source=source,
        title="嵌入模型",
        chunk_type="section",
        parent_id="1",
        heading_path="嵌入模型 > 部署",
        section_title="部署",
        chunk_index=2,
    )

    results = store.search(MODEL_STRING, top_k=5)

    child = _find(results, "pc-child")
    parent = _find(results, "pc-parent")
    assert child is not None and parent is not None
    # 三个契约字段每条都有
    for item in results:
        assert {"chunk_type", "parent_id", "heading_path"} <= set(item)
    assert child["chunk_type"] == "section"
    assert child["parent_id"] == "1"
    assert child["heading_path"] == "嵌入模型 > 部署"
    assert child["parent_content"] == parent_content, "子块必须带上父块全文"
    # 父块自身结果不附 parent_content
    assert "parent_content" not in parent
    assert parent["chunk_type"] == "parent"
    assert set(parent) == RESULT_FIELDS
    assert set(child) == RESULT_FIELDS | {"parent_content"}


def test_parent_content_empty_when_parent_missing(tmp_path):
    """索引与库对不上（父块缺失）时子块仍带 parent_content 键，值为空串。"""
    ingest, store, embedder = _setup(tmp_path)
    _add(
        ingest,
        embedder,
        "orphan-child",
        f"孤儿子块：{MODEL_STRING} 的细节。",
        source="doc-orphan.md",
        chunk_type="section",
        parent_id="7",
        chunk_index=1,
    )

    results = store.search(MODEL_STRING, top_k=5)

    child = _find(results, "orphan-child")
    assert child is not None
    assert child["parent_content"] == ""


# ── 8. 词法索引自身：分词 / 词典 / 接口 / 落盘 ─────────────────────


def test_tokenize_keeps_english_and_digits(tmp_path):
    """英文与数字原样保留（小写归一），纯标点与空白 token 丢弃。"""
    tokens = tokenize(f"{MODEL_STRING} 支持 4096 维，部署 bm25s 索引。")

    assert "qwen3" in tokens and "embedding" in tokens and "8b" in tokens
    assert "4096" in tokens and "bm25s" in tokens
    assert "支持" in tokens and "维" in tokens
    assert "-" not in tokens and " " not in tokens and "，" not in tokens
    assert tokenize("") == []
    assert tokenize(None) == []


def test_userdict_missing_is_skipped(tmp_path):
    """自定义词典不存在：不报错，返回 False，索引照常可用。"""
    missing = default_userdict_path(tmp_path / "chroma")

    assert load_userdict(missing) is False
    index = LexicalIndex(
        [("c1", f"{MODEL_STRING} 型号说明", {})], userdict_path=missing
    )
    hits = index.search(MODEL_STRING, 5)
    assert [hit[0] for hit in hits] == ["c1"]


def test_userdict_present_keeps_model_string_as_one_token(tmp_path):
    """自定义词典命中时型号串被切成一个 token（型号/术语的整体召回）。"""
    userdict = tmp_path / "userdict.txt"
    userdict.write_text("AIPMZ9型号词 100 n\n", encoding="utf-8")

    assert load_userdict(userdict) is True
    assert tokenize("AIPMZ9型号词 是术语") [0] == "aipmz9型号词"


def test_index_text_includes_heading_path_so_title_only_hits_work(tmp_path):
    """索引文本包含 title / heading_path：型号只写在标题路径里也能被词法召回。"""
    text = chunk_index_text("正文不含型号", {"title": "模型选型", "heading_path": MODEL_STRING})

    assert MODEL_STRING.lower().replace("-", "") in text.lower().replace("-", "")
    index = LexicalIndex([("c1", text, {})])
    assert [hit[0] for hit in index.search(MODEL_STRING, 5)] == ["c1"]


def test_lexical_search_interface_and_purity(tmp_path):
    """接口契约：rank 从 1 起、按分数降序；同一批记录必得同一结果（纯函数）。"""
    records = [
        ("c1", f"{MODEL_STRING} 向量检索", {"layer": "concept"}),
        ("c2", "zeta 无关内容", {"layer": "concept"}),
        ("c3", f"另一处 {MODEL_STRING} 说明", {"layer": "concept"}),
    ]

    first = LexicalIndex(records)
    second = LexicalIndex(records)
    hits = first.search(MODEL_STRING, 5)

    assert [hit[2] for hit in hits] == list(range(1, len(hits) + 1))
    scores = [hit[1] for hit in hits]
    assert scores == sorted(scores, reverse=True)
    assert [(h[0], h[2]) for h in hits] == [(h[0], h[2]) for h in second.search(MODEL_STRING, 5)]
    assert first.search("", 5) == []
    assert first.search("完全不相干的词", 5) == []
    assert LexicalIndex([]).search("任意", 5) == []


def test_where_predicate_filters_lexical_candidates(tmp_path):
    """where 谓词过滤（两路共用同一过滤条件）：层过滤对词法路同样生效。"""
    records = [
        ("c1", f"{MODEL_STRING} 概念层", {"layer": "concept"}),
        ("c2", f"{MODEL_STRING} 论文层", {"layer": "curated-paper"}),
    ]
    index = LexicalIndex(records)

    hits = index.search(MODEL_STRING, 5, where_pred=lambda meta: meta.get("layer") == "concept")

    assert [hit[0] for hit in hits] == ["c1"]
    assert [hit[2] for hit in hits] == [1]


def test_store_filter_uses_same_condition_on_both_paths(tmp_path):
    """检索侧同一过滤条件吃两路：category 过滤下词法独中的候选也不越界。"""
    ingest, store, embedder = _setup(tmp_path, vector_top_n=0)
    # 走入库接口的 layer/category 参数落 metadata（不再用 collection.update 整表替换，
    # 后者会绕开 _add 的 schema 组装，掩盖真实入库路径）。
    _add(
        ingest,
        embedder,
        "in-scope",
        f"{MODEL_STRING} 概念层",
        source="doc-in",
        layer="concept",
        category="concepts",
    )
    _add(
        ingest,
        embedder,
        "out-scope",
        f"{MODEL_STRING} 别的分类",
        source="doc-out",
        layer="concept",
        category="planning",
    )

    scoped = store.search(MODEL_STRING, top_k=5, category="concepts")

    assert [item["id"] for item in scoped] == ["in-scope"]
    # 无过滤时两条都在（同源去重不生效：source 不同；顺序不固定故按集合断言）
    unfiltered = store.search(MODEL_STRING, top_k=5)
    assert {item["id"] for item in unfiltered} == {"in-scope", "out-scope"}
    assert [item["category"] for item in unfiltered if item["id"] == "out-scope"] == ["planning"]
