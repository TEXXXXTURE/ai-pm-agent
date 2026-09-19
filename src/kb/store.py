# 知识库接入 - 本地档案检索（纯标准库关键词打分）
"""KBStore：本地知识库检索。

- index.json 位于 store_path 根下，结构：
  {"decision": {id: {path, title, tags, updated}}, "eval": {...}, "case": {...}}
- 检索为纯标准库关键词打分：中文按字符二元组、英文按小写词；
  title/tags 命中权重 x3，正文命中权重 x1。
- 文件不存在 / 索引损坏一律视为空库，绝不抛异常。
"""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

# 三类档案
ARCHIVE_TYPES = ("decision", "eval", "case")

_EN_WORD_RE = re.compile(r"[a-z0-9]+")
_HAN_RE = re.compile(r"[\u4e00-\u9fff]")


def read_index(index_path: Path) -> dict:
    """读取 index.json；文件不存在或内容损坏时返回空库结构（不抛异常）。"""
    empty: dict = {t: {} for t in ARCHIVE_TYPES}
    try:
        if not Path(index_path).exists():
            return empty
        data = json.loads(Path(index_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty
    for t in ARCHIVE_TYPES:
        if not isinstance(data.get(t), dict):
            data[t] = {}
    return data


def _tokenize(text: str) -> list[str]:
    """中英文混合分词：英文按小写词，中文按字符二元组。"""
    text = str(text)
    tokens: list[str] = _EN_WORD_RE.findall(text.lower())
    han = _HAN_RE.findall(text)
    tokens.extend(han[i] + han[i + 1] for i in range(len(han) - 1))
    return tokens


def _body_snippet(file_text: str, limit: int = 120) -> str:
    """提取档案正文前 ~limit 字（跳过文件头 HTML 注释与 # 标题行）。"""
    kept: list[str] = []
    for line in str(file_text).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("<!--") or stripped.startswith("#"):
            continue
        kept.append(stripped)
    return "\n".join(kept).strip()[:limit]


class KBStore:
    """本地知识库检索器。

    Args:
        store_path: 知识库根目录（index.json 与 decision/eval/case 子目录均在其下）。
    """

    def __init__(self, store_path: str):
        self.store_path = Path(store_path)
        self.index_path = self.store_path / "index.json"

    def _archive_path(self, rel_path: str) -> Path:
        """索引中记录的相对路径以 store_path 为基准解析；绝对路径原样返回。"""
        p = Path(rel_path)
        if p.is_absolute():
            return p
        candidate = self.store_path / p
        return candidate if candidate.exists() else p

    def retrieve_relevant(
        self,
        query: str,
        types: list[str] | None = None,
        top_k: int = 3,
    ) -> dict:
        """按关键词打分检索相关档案。

        Returns:
            命中时：{"status": "ok", "query": query, "archives": [...]}，
            archives 按 score 降序取 top_k，每项含
            id/type/title/path/score/snippet（正文前 ~120 字）；
            无档案或全部 0 分：{"status": "empty", "query": query, "archives": []}。
        """
        index = read_index(self.index_path)
        types = types or list(ARCHIVE_TYPES)
        query_tokens = _tokenize(query)

        archives: list[dict] = []
        for archive_type in types:
            for archive_id, meta in index.get(archive_type, {}).items():
                file_path = self._archive_path(meta.get("path", ""))
                try:
                    file_text = file_path.read_text(encoding="utf-8")
                except OSError:
                    file_text = ""

                head_text = f"{meta.get('title', '')} {' '.join(meta.get('tags') or [])}"
                head_counter = Counter(_tokenize(head_text))
                body_counter = Counter(_tokenize(file_text))

                score = 0
                for token in query_tokens:
                    score += head_counter.get(token, 0) * 3
                    score += body_counter.get(token, 0)
                if score <= 0:
                    continue

                archives.append(
                    {
                        "id": archive_id,
                        "type": archive_type,
                        "title": meta.get("title", ""),
                        "path": str(file_path),
                        "score": score,
                        "snippet": _body_snippet(file_text),
                    }
                )

        if not archives:
            return {"status": "empty", "query": query, "archives": []}

        archives.sort(key=lambda item: item["score"], reverse=True)
        return {"status": "ok", "query": query, "archives": archives[:top_k]}

    def query_eval(self, query: str, top_k: int = 3) -> dict:
        """限定 eval（评测）档案的检索包装。"""
        return self.retrieve_relevant(query, types=["eval"], top_k=top_k)

    def query_case(self, query: str, top_k: int = 3) -> dict:
        """限定 case（案例）档案的检索包装。"""
        return self.retrieve_relevant(query, types=["case"], top_k=top_k)


