# RAG 子库 - 词法索引（bm25s 全库召回 + jieba 分词）
"""lexical.py — 词法召回路（双路召回之一：bm25s 词法 ∥ ChromaDB 向量）。

设计（《RAG知识库实施规划》4.4 / 5.4 + 选型笔记「复审八」）：
- 索引 = 全库 chunk 的纯函数：同一批 (chunk_id, 文本, metadata) 必得同一索引；
  每批入库后整体重建（本规模毫秒级），不维护增量。
- 分词：jieba 切中文，英文/数字 token 原样保留（统一小写归一）；
  型号/术语靠自定义词典 domain_kb/lexical/userdict.txt 补词，文件不存在直接跳过。
- 接口：search(query, n) -> [(chunk_id, bm25_score, rank)]，rank 从 1 起。
- 与 Chroma 的关系：词法索引是旁路（ChromaDB 保留不动），全量重建型，不存在半新半旧；
  检索时按 where 谓词在结果上过滤，保证两路在同一个过滤条件下并行召回。
- 词法索引的文本 = title + section_title + heading_path + content（标题路径是
  chunk 的自包含信号，v1 的「标题命中加倍」在这里以「进索引」的方式延续）。
"""
from __future__ import annotations

import json
import re
import warnings
from pathlib import Path

import bm25s
import jieba

# 词法索引目录名（与 Chroma 同级的旁路目录：<chroma 同级>/lexical）
DEFAULT_LEXICAL_DIR_NAME = "lexical"
# jieba 自定义词典文件名（型号/术语；不存在则跳过，不报错）
DEFAULT_USERDICT_NAME = "userdict.txt"
# 词法召回默认候选数（与向量路同值，config 可覆盖）
DEFAULT_TOP_N = 50
# BM25 参数（bm25s 默认值，集中在此便于调参）
BM25_K1 = 1.5
BM25_B = 0.75

# 只保留「含字母/数字/汉字」的 token：纯标点与空白的 token 一律丢弃
_TOKEN_KEEP_RE = re.compile(r"[0-9A-Za-z\u4e00-\u9fff]")
# 已加载过的自定义词典（jieba 的词典是进程级全局状态，重复加载没有意义）
_LOADED_USERDICTS: set[str] = set()


def default_lexical_dir(chroma_path) -> Path:
    """词法索引目录缺省位置：与 Chroma 目录同级（chroma → 同级/lexical）。

    放在 Chroma 同级而不是写死 ./domain_kb/lexical：测试用 tmp 目录建库时，
    词法索引自动落在同一个 tmp 目录里，不会污染项目目录。
    """
    return Path(str(chroma_path)).parent / DEFAULT_LEXICAL_DIR_NAME


def default_userdict_path(chroma_path) -> Path:
    """jieba 自定义词典缺省路径：<词法目录>/userdict.txt。"""
    return default_lexical_dir(chroma_path) / DEFAULT_USERDICT_NAME


def load_userdict(path) -> bool:
    """加载 jieba 自定义词典（型号/术语）；文件不存在返回 False 且不报错。

    路径为空、不是文件、读取失败都按「没有自定义词典」处理：词法路降级为
    只用 jieba 默认词典，绝不因为词典缺失而中断检索。
    """
    if not path:
        return False
    candidate = Path(str(path))
    key = str(candidate)
    if key in _LOADED_USERDICTS:
        return True
    if not candidate.is_file():
        return False
    try:
        jieba.load_userdict(key)
    except Exception as exc:  # pragma: no cover - 词典损坏时不阻断检索
        warnings.warn(f"jieba 自定义词典加载失败，按默认词典继续：{key}（{exc}）", UserWarning, stacklevel=2)
        return False
    _LOADED_USERDICTS.add(key)
    return True


def tokenize(text) -> list[str]:
    """jieba 分词 → 词法 token 列表（小写归一，丢弃纯标点/空白 token）。

    英文与数字原样保留（`Qwen3-Embedding-8B` 默认切成 qwen3 / embedding / 8b，
    加进 userdict 后可整体成一个 token；查询与语料走同一分词器，两侧一致）。
    """
    tokens: list[str] = []
    for raw in jieba.lcut(str(text if text is not None else "")):
        token = raw.strip()
        if not token or not _TOKEN_KEEP_RE.search(token):
            continue
        tokens.append(token.lower())
    return tokens


def chunk_index_text(content, metadata: dict | None = None) -> str:
    """词法索引用的 chunk 文本：标题 + 章节标题 + 标题路径 + 正文。"""
    metadata = metadata or {}
    parts = [
        str(metadata.get("title") or ""),
        str(metadata.get("section_title") or ""),
        str(metadata.get("heading_path") or ""),
        str(content if content is not None else ""),
    ]
    return " ".join(part.strip() for part in parts if part and part.strip())


class LexicalIndex:
    """bm25s 词法索引（全量重建型，与 Chroma 的 chunk id 一一对齐）。

    Args:
        records: [(chunk_id, 索引文本, metadata), ...]，顺序即索引顺序。
        userdict_path: jieba 自定义词典路径；不存在则跳过。
        path: 可选，落盘目录（传了就顺手存一份索引文件）。
    """

    def __init__(self, records=None, userdict_path=None, path=None):
        self.userdict_path = str(userdict_path) if userdict_path else ""
        self.path = Path(str(path)) if path else None
        load_userdict(self.userdict_path)
        self.chunk_ids: list[str] = []
        self.metadatas: list[dict] = []
        self.texts: list[str] = []
        self.retriever = bm25s.BM25(k1=BM25_K1, b=BM25_B)
        self._index_records(list(records or []))

    # ── 构建 ──────────────────────────────────────────────────────

    def _index_records(self, records: list[tuple]) -> None:
        """按 (chunk_id, 文本, metadata) 记录重建整个索引（纯函数，可反复调用）。"""
        self.chunk_ids = []
        self.metadatas = []
        self.texts = []
        corpus_tokens: list[list[str]] = []
        for record in records:
            chunk_id, text, metadata = (list(record) + [None, None, None])[:3]
            if not str(chunk_id or "").strip():
                continue
            self.chunk_ids.append(str(chunk_id))
            self.metadatas.append(dict(metadata or {}))
            self.texts.append(str(text if text is not None else ""))
            corpus_tokens.append(tokenize(text))
        if not corpus_tokens:
            self.retriever = None
            return
        self.retriever = bm25s.BM25(k1=BM25_K1, b=BM25_B)
        self.retriever.index(corpus_tokens, show_progress=False)

    @classmethod
    def from_collection(cls, collection, userdict_path=None, path=None, batch_size: int = 1000):
        """从 Chroma collection 全量取 chunk 建索引（不带任何 where：全库索引）。

        分页读取（每批 batch_size 条）：本库规模几千条，一次读完也行，分页只是为了
        库长大以后不一次性吃满内存。
        """
        records: list[tuple] = []
        offset = 0
        while True:
            page = collection.get(
                limit=batch_size,
                offset=offset,
                include=["documents", "metadatas"],
            )
            ids = list(page.get("ids") or [])
            if not ids:
                break
            documents = list(page.get("documents") or [])
            metadatas = list(page.get("metadatas") or [])
            for index, chunk_id in enumerate(ids):
                document = documents[index] if index < len(documents) else ""
                metadata = metadatas[index] if index < len(metadatas) else {}
                metadata = dict(metadata or {})
                records.append((chunk_id, chunk_index_text(document, metadata), metadata))
            offset += len(ids)
            if len(ids) < batch_size:
                break
        return cls(records, userdict_path=userdict_path, path=path)

    # ── 检索 ──────────────────────────────────────────────────────

    def search(self, query: str, n: int = DEFAULT_TOP_N, where_pred=None) -> list[tuple]:
        """词法召回：返回 [(chunk_id, bm25_score, rank)]，rank 从 1 起。

        Args:
            query: 查询文本（与索引走同一分词器）。
            n: 需要的候选条数。
            where_pred: 可选谓词 metadata -> bool（两路共用的同一过滤条件的词法版）；
                给了谓词时按「全库打分后过滤」而不是「先截断再过滤」，
                避免被过滤掉的条目在截断里占据名次。
        """
        if self.retriever is None or not self.chunk_ids:
            return []
        query = str(query if query is not None else "")
        if not query.strip():
            return []
        tokens = tokenize(query)
        if not tokens:
            return []
        n = max(1, int(n))
        k = len(self.chunk_ids) if where_pred is not None else min(n, len(self.chunk_ids))
        indices, scores = self.retriever.retrieve([tokens], k=k, show_progress=False)

        results: list[tuple] = []
        for position, raw_index in enumerate(indices[0]):
            score = float(scores[0][position])
            if score <= 0.0:
                # 完全不命中的条目 bm25s 会给 0 分，不进候选
                continue
            index = int(raw_index)
            if index < 0 or index >= len(self.chunk_ids):
                continue
            if where_pred is not None and not where_pred(self.metadatas[index]):
                continue
            results.append((self.chunk_ids[index], score, len(results) + 1))
            if len(results) >= n:
                break
        return results

    # ── 落盘（索引文件；检索侧默认在内存重建，见 store.py 的说明） ──

    def save(self, path=None) -> Path:
        """把索引与 chunk 记录落盘到目录（bm25s 索引文件 + corpus.jsonl）。"""
        target = Path(str(path if path is not None else self.path))
        target.mkdir(parents=True, exist_ok=True)
        if self.retriever is not None:
            corpus = [
                {"id": chunk_id, "text": self.texts[index], "metadata": self.metadatas[index]}
                for index, chunk_id in enumerate(self.chunk_ids)
            ]
            self.retriever.save(str(target), corpus=corpus, show_progress=False)
        (target / "index_meta.json").write_text(
            json.dumps(
                {
                    "chunks": len(self.chunk_ids),
                    "userdict": self.userdict_path,
                    "k1": BM25_K1,
                    "b": BM25_B,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return target

    @classmethod
    def load(cls, path, userdict_path=None):
        """从落盘目录读回索引；目录不存在或读不出来时返回空索引（不抛错）。"""
        target = Path(str(path))
        if not target.is_dir():
            return cls([], userdict_path=userdict_path)
        try:
            retriever = bm25s.BM25.load(str(target), load_corpus=True, show_progress=False)
        except Exception as exc:
            warnings.warn(f"词法索引读取失败，按空索引处理：{target}（{exc}）", UserWarning, stacklevel=2)
            return cls([], userdict_path=userdict_path)
        instance = cls([], userdict_path=userdict_path)
        records = []
        for entry in list(getattr(retriever, "corpus", None) or []):
            if isinstance(entry, dict):
                records.append((entry.get("id"), entry.get("text"), entry.get("metadata")))
        instance._index_records(records)
        return instance

    def __len__(self) -> int:
        return len(self.chunk_ids)
