# RAG 子库 - 检索器（双路召回：bm25s 词法 ∥ 向量 + RRF 融合 + 质量乘数）
"""RAGStore：在 agent_knowledge collection 上做双路召回 + RRF 融合检索。

检索流程（对应《RAG知识库实施规划》4.4 / 5.5 + 选型笔记「复审八/九」）：
1. 双路召回（同一 where 过滤下并行，各取 top N，默认 50）：
   - 词法路：bm25s 全库召回（lexical.LexicalIndex，旁路索引，见 lexical.py）；
   - 向量路：ChromaDB 向量召回，cosine distance → vector_sim = 1 - distance。
   v1 的「向量先捞 top_k×5 → 候选内补关键词分」是结构性缺陷：关键词精确命中但向量
   不在候选里的内容（型号、术语、错误码）永远出不来，v2 把词法升为独立全库召回。
2. RRF 融合：score = Σ 1/(k + rank_i)，k 默认 60（config 可覆盖）。免归一化、单参数。
3. 质量乘数（不做层权：三层权重在 v2 作废）：
   × quality 权重（user_input 1.5 / curated 1.2 / auto 1.0；缺 quality 键时按
   curated 布尔映射 true→curated 权重）× feishu_ref_bonus 1.3（命中用户飞书标注时）。
4. 过滤：is_stale 软删 → 相关性阈值（只作用于向量路候选）→ 同源去重
   （max_per_source，默认 1）→ 取 Top-K。
5. 查询输出契约（给下游 Agent 消费）：每条结果带 chunk_type / parent_id /
   heading_path；子块（chunk_type=section 且 parent_id 非空）额外附 parent_content
   = 同 source 内 chunk_type=parent 且 chunk_index == parent_id 的父块全文。

参数全部走代码默认值 + config domain_kb.retrieval 可选覆盖（不改 config.yaml）。
v1 的 keyword_weight / vector_weight / layer_weights 键在 v2 不再参与打分（保留在
config 里不影响运行）。嵌入通过 embedder 注入：测试传假 embedder，零真实 API。
"""
from __future__ import annotations

from pathlib import Path

import chromadb

from kb.rag.embeddings import EmbeddingModel
from kb.rag.ingest import COLLECTION_NAME
from kb.rag.lexical import (
    DEFAULT_TOP_N,
    LexicalIndex,
    default_lexical_dir,
    default_userdict_path,
)

# retrieval 段缺省值（config 未给时使用）
DEFAULT_TOP_K = 5
# 双路各召回候选数（RRF 之前的名次池）
DEFAULT_CANDIDATE_TOP_N = DEFAULT_TOP_N
# RRF 平滑常数：k=60（ES/OpenSearch/Weaviate/Qdrant 默认）
DEFAULT_RRF_K = 60
# 质量乘数（统一 metadata schema 4.3 的 quality 键）
DEFAULT_QUALITY_WEIGHTS = {"user_input": 1.5, "curated": 1.2, "auto": 1.0}
# 缺 quality 键时按 curated 布尔映射到的档位
DEFAULT_LEGACY_QUALITY_KEY = "curated"
DEFAULT_FEISHU_REF_BONUS = 1.3
# 相关性阈值（vector_sim 低于此值视为与需求语义距离太远，过滤掉；0 = 不过滤）
DEFAULT_MIN_VECTOR_SIM = 0.0
# 同一 source 文档最多保留几条（默认 1 = 去重，保证 Top-K 覆盖不同来源）
DEFAULT_MAX_PER_SOURCE = 1
# 子块结果的类型标记（附 parent_content 的判据）
CHILD_CHUNK_TYPE = "section"
PARENT_CHUNK_TYPE = "parent"


class RAGStore:
    """领域知识库检索器（双路召回 + RRF 融合）。

    Args:
        config: config.yaml 根 dict，内部取 config["domain_kb"]。
        embedder: 可选嵌入器；传入即用（测试注入假 embedder），未传则用
            EmbeddingModel(domain_kb["embedding"])（真实调用嵌入 API）。
    """

    def __init__(self, config: dict, embedder=None):
        domain_kb = dict((config or {}).get("domain_kb") or {})
        retrieval = dict(domain_kb.get("retrieval") or {})
        lexical_cfg = dict(domain_kb.get("lexical") or {})
        self.domain_kb = domain_kb
        self.chroma_path = Path(str(domain_kb.get("chroma_path") or "./domain_kb/chroma"))

        self.default_top_k = _config_number(retrieval.get("top_k"), DEFAULT_TOP_K, int)
        # 双路候选数：top_n 为共同缺省，lexical_top_n / vector_top_n 可单独覆盖
        # （显式给 0 = 关闭该路召回，因此这里按「键缺席才用默认值」解析）
        self.top_n = _config_number(retrieval.get("top_n"), DEFAULT_CANDIDATE_TOP_N, int)
        self.lexical_top_n = _config_number(retrieval.get("lexical_top_n"), self.top_n, int)
        self.vector_top_n = _config_number(retrieval.get("vector_top_n"), self.top_n, int)
        self.rrf_k = _config_number(retrieval.get("rrf_k"), DEFAULT_RRF_K, float)
        self.feishu_ref_bonus = _config_number(
            retrieval.get("feishu_ref_bonus"), DEFAULT_FEISHU_REF_BONUS, float
        )
        # 相关性阈值 + 同源去重上限（默认 1 = 同一文档最多 1 条）
        self.min_vector_sim = _config_number(
            retrieval.get("min_vector_sim"), DEFAULT_MIN_VECTOR_SIM, float
        )
        self.max_per_source = _config_number(
            retrieval.get("max_per_source"), DEFAULT_MAX_PER_SOURCE, int
        )
        # 质量乘数：config 可整体覆盖（如加新质量档）
        quality_weights = retrieval.get("quality_weights")
        self.quality_weights = (
            {str(key): float(value) for key, value in quality_weights.items()}
            if isinstance(quality_weights, dict) and quality_weights
            else dict(DEFAULT_QUALITY_WEIGHTS)
        )
        # 词法索引位置与 jieba 自定义词典（缺省都落在 Chroma 同级目录下）
        self.lexical_path = Path(
            str(
                domain_kb.get("lexical_path")
                or lexical_cfg.get("path")
                or default_lexical_dir(self.chroma_path)
            )
        )
        self.userdict_path = Path(
            str(
                lexical_cfg.get("user_dict")
                or lexical_cfg.get("userdict")
                or default_userdict_path(self.chroma_path)
            )
        )
        self.lexical_enabled = True
        if lexical_cfg.get("enabled") is not None:
            self.lexical_enabled = bool(lexical_cfg.get("enabled"))
        self._lexical: LexicalIndex | None = None
        self._lexical_ready = False

        self.client = chromadb.PersistentClient(path=str(self.chroma_path))
        self.collection = self.client.get_or_create_collection(
            COLLECTION_NAME, metadata={"hnsw:space": "cosine"}
        )
        if embedder is not None:
            self.embedder = embedder
        else:
            self.embedder = EmbeddingModel(domain_kb.get("embedding") or {})

    # ── 词法索引（懒加载：只在真正检索时按需全量重建） ──────────────

    @property
    def lexical_index(self) -> LexicalIndex | None:
        """词法索引（首次访问时从 Chroma 全量重建；失败降级为「无词法路」）。

        刻意在内存里按库内容重建而不是读 domain_kb/lexical 的落盘索引：
        词法索引必须与 Chroma 严格同步，读磁盘文件会引入「Chroma 更新了、
        索引文件没更新」的窗口；本规模毫秒级，宁可贵一点保正确。
        """
        if not self._lexical_ready:
            self._lexical_ready = True
            if self.lexical_enabled and self.collection.count() > 0:
                try:
                    self._lexical = LexicalIndex.from_collection(
                        self.collection, userdict_path=self.userdict_path
                    )
                except Exception as exc:  # pragma: no cover - 缺 bm25s/jieba 时降级
                    self._lexical = None
                    print(f"[rag_store] 词法索引构建失败，本次检索降级为单路向量召回：{exc}")
        return self._lexical

    def rebuild_lexical_index(self) -> int:
        """强制按当前库内容重建词法索引，返回索引条数（失败抛错，由调用方决定）。"""
        self._lexical = LexicalIndex.from_collection(
            self.collection, userdict_path=self.userdict_path
        )
        self._lexical_ready = True
        return len(self._lexical)

    # ── 检索 ──────────────────────────────────────────────────────

    def search(
        self,
        query: str,
        top_k: int = 5,
        layers: list[str] | None = None,
        category: str | None = None,
        feishu_refs: list[str] | None = None,
    ) -> list[dict]:
        """双路召回 + RRF 融合检索，返回按最终分降序的结果列表（空 query / 无候选返回 []）。"""
        if not str(query or "").strip() or top_k <= 0:
            return []

        # 1. 两路共用同一个过滤条件：where 给 Chroma，谓词给词法索引
        where = self._build_where(layers, category)
        where_pred = _where_predicate(where)

        # 2. 双路召回
        vector_rows = self._vector_recall(str(query), where)
        lexical_rows = self._lexical_recall(str(query), where_pred)
        if not vector_rows and not lexical_rows:
            return []

        # 3. 合并候选 + RRF 融合 + 质量乘数
        candidates = self._merge_candidates(vector_rows, lexical_rows)
        feishu_watch = list(feishu_refs) if feishu_refs else None
        max_bm25 = max((row.get("bm25_score") or 0.0 for row in candidates), default=0.0)

        scored: list[tuple[float, dict]] = []
        for candidate in candidates:
            rrf = 0.0
            vector_rank = candidate.get("vector_rank")
            bm25_rank = candidate.get("bm25_rank")
            if vector_rank:
                rrf += 1.0 / (self.rrf_k + float(vector_rank))
            if bm25_rank:
                rrf += 1.0 / (self.rrf_k + float(bm25_rank))

            metadata = candidate.get("metadata") or {}
            weight = _quality_weight(metadata, self.quality_weights)
            if feishu_watch and metadata.get("feishu_ref") in feishu_watch:
                weight *= self.feishu_ref_bonus
            total = rrf * weight

            keyword_norm = (
                (candidate.get("bm25_score") or 0.0) / max_bm25 if max_bm25 > 0 else 0.0
            )
            item = self._result_item(candidate, rrf, total, keyword_norm)
            scored.append((total, item))

        # 4. 排序（分数降序；同分保持候选合并顺序，sorted 稳定）
        scored.sort(key=lambda entry: entry[0], reverse=True)

        # 5. 过滤：is_stale 软删 → 相关性阈值 → 同源去重（每 source 最多 max_per_source 条）
        #    阈值只作用于「向量路召回过的」候选：纯词法命中的条目本来就没有向量相似度，
        #    拿向量阈值去卡它等于把 v2 要修的那类命中重新丢掉。
        filtered: list[dict] = []
        per_source: dict[str, int] = {}
        for _, item in scored:
            if _is_stale(item.get("_metadata") or {}):
                continue
            if item.get("vector_rank") is not None and float(item.get("vector_sim") or 0.0) < self.min_vector_sim:
                continue
            source = str(item.get("source") or "")
            if per_source.get(source, 0) >= self.max_per_source:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            filtered.append(item)
            if len(filtered) >= top_k:
                break

        # 6. 查询输出契约：子块附带父块全文（父块自身不附）
        self._attach_parent_context(filtered)
        for item in filtered:
            item.pop("_metadata", None)
            item.pop("_has_vector", None)
            # _score_raw 也是内部字段（排序早已用 total 完成），必须一并剔除：
            # 否则结果项会多出第 18 个键，破坏「子块 17 键 + parent_content」的输出契约。
            item.pop("_score_raw", None)
        return filtered

    # ── 内部：两路召回 ────────────────────────────────────────────

    def _vector_recall(self, query: str, where: dict | None) -> list[dict]:
        """向量路召回：返回 [{id, rank, vector_sim, content, metadata}]（rank 从 1 起）。"""
        if self.vector_top_n <= 0:
            return []
        query_vector = self.embedder.embed_query(query)
        query_kwargs = {
            "query_embeddings": [query_vector],
            "n_results": self.vector_top_n,
        }
        if where is not None:
            query_kwargs["where"] = where
        result = self.collection.query(**query_kwargs)

        ids = (result.get("ids") or [[]])[0]
        distances = (result.get("distances") or [[]])[0]
        documents = (result.get("documents") or [[]])[0]
        metadatas = (result.get("metadatas") or [[]])[0]

        rows: list[dict] = []
        for index, chunk_id in enumerate(ids):
            distance = distances[index] if index < len(distances) else None
            vector_sim = 1.0 - float(distance) if distance is not None else 0.0
            rows.append(
                {
                    "id": str(chunk_id),
                    "rank": len(rows) + 1,
                    "vector_sim": vector_sim,
                    "content": documents[index] if index < len(documents) else "",
                    "metadata": (metadatas[index] if index < len(metadatas) else {}) or {},
                }
            )
        return rows

    def _lexical_recall(self, query: str, where_pred) -> list[dict]:
        """词法路召回：返回 [{id, rank, bm25_score}]（rank 从 1 起）。"""
        if self.lexical_top_n <= 0:
            return []
        index = self.lexical_index
        if index is None or len(index) == 0:
            return []
        hits = index.search(query, self.lexical_top_n, where_pred=where_pred)
        return [
            {"id": str(chunk_id), "rank": rank, "bm25_score": float(score)}
            for chunk_id, score, rank in hits
        ]

    def _merge_candidates(self, vector_rows: list[dict], lexical_rows: list[dict]) -> list[dict]:
        """按 chunk id 合并两路候选：向量路带正文与 metadata，词法独中的补一次 get。"""
        merged: dict[str, dict] = {}
        for row in vector_rows:
            merged[row["id"]] = {
                "id": row["id"],
                "content": row.get("content") or "",
                "metadata": row.get("metadata") or {},
                "vector_rank": row.get("rank"),
                "vector_sim": row.get("vector_sim"),
                "bm25_rank": None,
                "bm25_score": 0.0,
            }

        missing = [row["id"] for row in lexical_rows if row["id"] not in merged]
        fetched = self._fetch_chunks(missing)
        for row in lexical_rows:
            entry = merged.get(row["id"])
            if entry is None:
                payload = fetched.get(row["id"])
                if payload is None:
                    # 词法索引与库里对不上（极端情况）：跳过，不编造内容
                    continue
                entry = {
                    "id": row["id"],
                    "content": payload.get("content") or "",
                    "metadata": payload.get("metadata") or {},
                    "vector_rank": None,
                    "vector_sim": 0.0,
                    "bm25_rank": None,
                    "bm25_score": 0.0,
                }
                merged[row["id"]] = entry
            entry["bm25_rank"] = row.get("rank")
            entry["bm25_score"] = float(row.get("bm25_score") or 0.0)
        return list(merged.values())

    def _fetch_chunks(self, ids: list[str]) -> dict[str, dict]:
        """按 id 批量取正文与 metadata（只给词法独中的候选补数据）。"""
        if not ids:
            return {}
        result = self.collection.get(ids=list(ids), include=["documents", "metadatas"])
        found: dict[str, dict] = {}
        got_ids = list(result.get("ids") or [])
        documents = list(result.get("documents") or [])
        metadatas = list(result.get("metadatas") or [])
        for index, chunk_id in enumerate(got_ids):
            found[str(chunk_id)] = {
                "content": documents[index] if index < len(documents) else "",
                "metadata": (metadatas[index] if index < len(metadatas) else {}) or {},
            }
        return found

    # ── 内部：结果组装与查询输出契约 ───────────────────────────────

    @staticmethod
    def _result_item(candidate: dict, rrf: float, total: float, keyword_norm: float) -> dict:
        """把候选拼成结果条目（保留 v1 全部键 + 新增 v2 明细与契约字段）。"""
        metadata = candidate.get("metadata") or {}
        vector_rank = candidate.get("vector_rank")
        return {
            "id": candidate["id"],
            "content": candidate.get("content") or "",
            # score = RRF 分 × 质量乘数 × 飞书加成（排序依据）
            "score": round(total, 4),
            "layer": str(metadata.get("layer") or ""),
            "category": str(metadata.get("category") or ""),
            "source": str(metadata.get("source") or ""),
            "title": str(metadata.get("title") or ""),
            "section_title": str(metadata.get("section_title") or ""),
            "chunk_index": int(metadata.get("chunk_index") or 0),
            "vector_sim": round(float(candidate.get("vector_sim") or 0.0), 4),
            # v1 键保留：本批候选内 bm25 分归一（无词法命中为 0.0），供对照与调参
            "keyword_norm": round(keyword_norm, 4),
            # v2 融合明细
            "bm25_rank": candidate.get("bm25_rank"),
            "vector_rank": vector_rank,
            "rrf_score": round(rrf, 6),
            # 查询输出契约：给下游 Agent 消费
            "chunk_type": str(metadata.get("chunk_type") or ""),
            "parent_id": str(metadata.get("parent_id") or ""),
            "heading_path": str(metadata.get("heading_path") or ""),
            # 内部字段：排序用的最终分与过滤用的 metadata（返回前剔除）
            "_score_raw": rrf,
            "_metadata": metadata,
            "_has_vector": vector_rank is not None,
        }

    def _attach_parent_context(self, items: list[dict]) -> None:
        """给子块结果附 parent_content（父块全文）；父块自身结果不附。

        定位方式（splitter 约定）：子块 metadata.parent_id 存父块的 chunk_index，
        因此父块 = 同一 source 内 chunk_type=parent 且 chunk_index == parent_id 的块。
        按 source 归组，一个 source 只查一次库。
        """
        need: dict[str, set[int]] = {}
        for item in items:
            if str(item.get("chunk_type") or "") != CHILD_CHUNK_TYPE:
                continue
            parent_id = str(item.get("parent_id") or "").strip()
            if not parent_id:
                continue
            try:
                parent_index = int(parent_id)
            except (TypeError, ValueError):
                continue
            need.setdefault(str(item.get("source") or ""), set()).add(parent_index)
        if not need:
            return

        parent_text: dict[tuple[str, int], str] = {}
        for source, indexes in need.items():
            try:
                result = self.collection.get(
                    where={"$and": [{"source": source}, {"chunk_type": PARENT_CHUNK_TYPE}]},
                    include=["documents", "metadatas"],
                )
            except Exception:  # pragma: no cover - 取不到父块不该让检索失败
                continue
            documents = list(result.get("documents") or [])
            metadatas = list(result.get("metadatas") or [])
            for index, metadata in enumerate(metadatas):
                try:
                    chunk_index = int((metadata or {}).get("chunk_index"))
                except (TypeError, ValueError):
                    continue
                if chunk_index in indexes:
                    parent_text[(source, chunk_index)] = (
                        documents[index] if index < len(documents) else ""
                    )

        for item in items:
            if str(item.get("chunk_type") or "") != CHILD_CHUNK_TYPE:
                continue
            parent_id = str(item.get("parent_id") or "").strip()
            if not parent_id:
                continue
            try:
                parent_index = int(parent_id)
            except (TypeError, ValueError):
                continue
            # 找不到父块时给空串：契约要求「子块必有 parent_content 键」，缺父块也不缺键
            item["parent_content"] = parent_text.get(
                (str(item.get("source") or ""), parent_index), ""
            )

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


# ── 模块级工具：配置解析 / 质量乘数 / 软删 / where 谓词 ─────────────


def _config_number(value, default, cast):
    """config 取值：键缺席（None）用默认值；显式给的数字照用（含 0）。

    0 在两处是有意义的值（lexical_top_n / vector_top_n = 0 表示关闭该路），
    因此不能用 `value or default` 那种写法把它吞掉。
    """
    if value is None:
        return cast(default)
    try:
        return cast(value)
    except (TypeError, ValueError):
        return cast(default)


def _quality_weight(metadata: dict, weights: dict) -> float:
    """质量乘数：优先读 quality 键；没有该键时按 curated 布尔映射。

    映射值取 weights 里的 curated 档（默认 1.2），与统一 schema 4.3 的
    「quality 取代 v1 层权」一致：存量 chunk 只有 curated 布尔，靠这条映射过渡。
    """
    metadata = metadata or {}
    quality = metadata.get("quality")
    if quality is not None and str(quality).strip():
        return float(weights.get(str(quality).strip(), 1.0))
    legacy_key = DEFAULT_LEGACY_QUALITY_KEY
    if _is_true(metadata.get("curated")):
        return float(weights.get(legacy_key, 1.2))
    return float(weights.get("auto", 1.0))


def _is_true(value) -> bool:
    """宽松真值判断：兼容布尔与 Chroma 里存成的字符串（"True"/"1"/"yes"）。"""
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    if isinstance(value, (int, float)):
        return value != 0
    return str(value).strip().lower() in {"true", "1", "yes", "y"}


def _is_stale(metadata: dict) -> bool:
    """is_stale 软删标记：为真则该 chunk 不参与结果（字段缺失即未软删）。"""
    return _is_true((metadata or {}).get("is_stale"))


def _where_predicate(where: dict | None):
    """把 Chroma where 条件翻译成 metadata 谓词，供词法路做同一过滤。"""
    if not where:
        return None

    def predicate(metadata: dict) -> bool:
        return _match_where(metadata or {}, where)

    return predicate


def _match_where(metadata: dict, where: dict) -> bool:
    """metadata 是否满足 where 条件（支持 $and 与 $in / $eq / 直接等值）。"""
    if "$and" in where:
        return all(_match_where(metadata, sub) for sub in (where.get("$and") or []))
    for key, condition in where.items():
        value = metadata.get(key)
        if isinstance(condition, dict):
            if "$in" in condition:
                wanted = {str(item) for item in (condition.get("$in") or [])}
                if str(value) not in wanted:
                    return False
                continue
            if "$eq" in condition:
                if str(value) != str(condition.get("$eq")):
                    return False
                continue
            # 未知算子：不认识的过滤条件一律放过，避免词法路被静默清空
            continue
        if str(value) != str(condition):
            return False
    return True
