# [C 2026-09-12] RAG 子库 - 检索器（分层加权混合检索：向量召回 + 候选内关键词分）
"""RAGStore：在 agent_knowledge collection 上做分层加权混合检索。

检索流程（对应《RAG知识库实施规划》5.4）：
1. 嵌入 query（embedder.embed_query）；
2. 按 layers / category 构造 Chroma where 过滤（都给时用 $and；都不给不过滤）；
3. 向量召回 top_k*5 条候选，cosine distance → vector_sim = 1 - distance；
4. 候选集内做关键词打分：复用 kb/store.py 的 _tokenize，
   query token 在正文命中 ×1、在 title + section_title 命中 ×3，
   候选集内按最大关键词分归一化到 0-1；
5. 融合分 final = keyword_weight × keyword_norm + vector_weight × vector_sim；
6. 加权 score = final × layer_weight × curated_bonus × feishu_ref_bonus；
7. 按 score 降序取 top_k 返回。

权重全部从 config.yaml 的 domain_kb.retrieval 段读，缺省 0.4/0.6、层权 2.0/1.5/1.0、
bonus 1.2/1.3。嵌入通过 embedder 注入：测试传假 embedder，零真实 API。
"""
from __future__ import annotations

from collections import Counter
from pathlib import Path

import chromadb

from kb.rag.embeddings import EmbeddingModel
from kb.rag.ingest import COLLECTION_NAME
from kb.store import _tokenize

# retrieval 段缺省值（config 未给时使用）
DEFAULT_TOP_K = 5
DEFAULT_KEYWORD_WEIGHT = 0.4
DEFAULT_VECTOR_WEIGHT = 0.6
DEFAULT_LAYER_WEIGHTS = {"concept": 2.0, "curated-paper": 1.5, "tracked-paper": 1.0}
DEFAULT_CURATED_BONUS = 1.2
DEFAULT_FEISHU_REF_BONUS = 1.3
# 向量召回倍数：先捞 top_k * 5 条候选，再在候选内算关键词分
CANDIDATE_MULTIPLIER = 5


class RAGStore:
    """领域知识库检索器（分层加权混合检索）。

    Args:
        config: config.yaml 根 dict，内部取 config["domain_kb"]。
        embedder: 可选嵌入器；传入即用（测试注入假 embedder），未传则用
            EmbeddingModel(domain_kb["embedding"])（真实调用硅基流动 API）。
    """

    def __init__(self, config: dict, embedder=None):
        domain_kb = dict((config or {}).get("domain_kb") or {})
        retrieval = dict(domain_kb.get("retrieval") or {})
        self.domain_kb = domain_kb
        self.chroma_path = Path(str(domain_kb.get("chroma_path") or "./domain_kb/chroma"))

        self.default_top_k = int(retrieval.get("top_k") or DEFAULT_TOP_K)
        self.keyword_weight = float(retrieval.get("keyword_weight", DEFAULT_KEYWORD_WEIGHT))
        self.vector_weight = float(retrieval.get("vector_weight", DEFAULT_VECTOR_WEIGHT))
        layer_weights = retrieval.get("layer_weights")
        self.layer_weights = (
            {str(k): float(v) for k, v in layer_weights.items()}
            if isinstance(layer_weights, dict) and layer_weights
            else dict(DEFAULT_LAYER_WEIGHTS)
        )
        self.curated_bonus = float(retrieval.get("curated_bonus", DEFAULT_CURATED_BONUS))
        self.feishu_ref_bonus = float(retrieval.get("feishu_ref_bonus", DEFAULT_FEISHU_REF_BONUS))

        self.client = chromadb.PersistentClient(path=str(self.chroma_path))
        self.collection = self.client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = EmbeddingModel(domain_kb.get("embedding") or {})

    # ── 检索 ──────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        layers: list[str] | None = None,
        category: str | None = None,
        feishu_refs: list[str] | None = None,
    ) -> list[dict]:
        """分层加权混合检索，返回按加权总分降序的结果列表（空 query / 无候选返回 []）。"""
        if not str(query or "").strip() or top_k <= 0:
            return []

        query_vector = self.embedder.embed_query(str(query))

        # 1. 向量召回（含 metadata 过滤）
        where = self._build_where(layers, category)
        query_kwargs = {
            "query_embeddings": [query_vector],
            "n_results": top_k * CANDIDATE_MULTIPLIER,
        }
        if where is not None:
            query_kwargs["where"] = where
        result = self.collection.query(**query_kwargs)

        ids = (result.get("ids") or [[]])[0]
        if not ids:
            return []
        distances = (result.get("distances") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

        # 2. 候选内关键词打分（未归一化）
        query_tokens = _tokenize(str(query))
        raw_keyword_scores: list[int] = []
        for index, _ in enumerate(ids):
            document = documents[index] if index < len(documents) else ""
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata or {}
            head_text = f"{metadata.get('title', '')} {metadata.get('section_title', '')}"
            head_counter = Counter(_tokenize(head_text))
            body_counter = Counter(_tokenize(document or ""))
            score = 0
            for token in query_tokens:
                score += head_counter.get(token, 0) * 3
                score += body_counter.get(token, 0)
            raw_keyword_scores.append(score)

        max_keyword = max(raw_keyword_scores) if raw_keyword_scores else 0

        feishu_watch = list(feishu_refs) if feishu_refs else None

        # 3. 融合 + 分层加权
        scored: list[tuple[float, dict]] = []
        for index, chunk_id in enumerate(ids):
            metadata = metadatas[index] if index < len(metadatas) else {}
            metadata = metadata or {}
            document = documents[index] if index < len(documents) else ""

            distance = distances[index] if index < len(distances) else 0.0
            vector_sim = 1.0 - float(distance) if distance is not None else 0.0
            keyword_norm = (
                raw_keyword_scores[index] / max_keyword if max_keyword > 0 else 0.0
            )
            final = self.keyword_weight * keyword_norm + self.vector_weight * vector_sim

            layer = str(metadata.get("layer") or "")
            weight = self.layer_weights.get(layer, 1.0)
            if metadata.get("curated"):
                weight *= self.curated_bonus
            if feishu_watch and metadata.get("feishu_ref") in feishu_watch:
                weight *= self.feishu_ref_bonus
            total_score = final * weight

            scored.append(
                (
                    total_score,
                    {
                        "id": chunk_id,
                        "content": document or "",
                        "score": round(total_score, 4),
                        "layer": layer,
                        "category": str(metadata.get("category") or ""),
                        "source": str(metadata.get("source") or ""),
                        "title": str(metadata.get("title") or ""),
                        "section_title": str(metadata.get("section_title") or ""),
                        "chunk_index": int(metadata.get("chunk_index") or 0),
                        "vector_sim": round(vector_sim, 4),
                        "keyword_norm": round(keyword_norm, 4),
                    },
                )
            )

        # 4. 排序（分数降序；同分保持向量召回顺序，sorted 稳定）→ 截断 top_k
        scored.sort(key=lambda item: item[0], reverse=True)
        return [item[1] for item in scored[:top_k]]

    # ── 内部：where 构造 ──────────────────────────────────────────

    @staticmethod
    def _build_where(layers: list[str] | None, category: str | None) -> dict | None:
        """构造 Chroma metadata 过滤条件：both → $and；单个 → 单列；都无 → None。"""
        layer_list = [str(layer) for layer in layers] if layers else []
        category_value = str(category) if category is not None and str(category) != "" else None

        if layer_list and category_value:
            return {
                "$and": [
                    {"layer": {"$in": layer_list}},
                    {"category": category_value},
                ]
            }
        if layer_list:
            return {"layer": {"$in": layer_list}}
        if category_value:
            return {"category": category_value}
        return None


# [C 2026-09-12 by pi-deepseek-v4-flash]
