# RAG 增量入库自测 - 格式契约 + 幂等入库 + CLI/inbox 入口（零真实 API、零真实 Chroma 之外的依赖）
"""增量入库（阶段二）零成本自测：ChromaDB 用 tmp_path 下的真实嵌入式实例，嵌入走假 embedder。

覆盖：
1. 格式契约：frontmatter 必填齐全省略校验通过、tags 可选、YAML 日期归一化；
2. 必填缺失 / updated 形态不合法 → DocumentFormatError，消息与 problems 都是缺项清单；
3. 纯文本自动包壳：source 取内容 hash、type 取 note、标题按 title → 首个 # 标题 → 文件名回落；
   显式 title/source/doc_type 覆盖 frontmatter 同名字段；
4. ingest_document：确定性 chunk ID 形态「doc_id:strategy_version:chunk_index」、
   metadata 字段齐全、入库摘要打印；
5. 幂等：同一文档入库两次 chunk 数与 ID 集合不变（不重复）；
6. hash 跳过：content_hash + strategy_version 均未变 → skipped，且不调用嵌入；
7. strategy_version 变更 → 不跳过、ID 中段变化、旧块清理；
8. 修改文档只影响该 source：另一 source 的 ID/正文/向量不动；
9. 孤儿清理：新版块数少于旧版时旧块被清掉（含绕过管道直接写库的残块）；
10. 状态表落盘 domain_kb/state/ingest_state.json（content_hash / strategy_version / chunk_ids）；
11. CLI --incremental --file / --text（含格式契约失败退出 1、参数错误退出 2）；
12. CLI --inbox（入库/跳过计数、第二次全跳过、改文件后只重入该篇、缺目录退出 1、
    单篇坏文件只记失败不中断）。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_incremental.py -q
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _candidate in (str(SRC_DIR), str(REPO_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import pytest  # noqa: E402

from kb.rag.contract import (  # noqa: E402
    DocumentFormatError,
    auto_source,
    parse_document,
    split_front_matter,
)
from kb.rag.ingest import (  # noqa: E402
    CHUNKING_VERSION,
    IngestState,
    RAGIngest,
    chunk_id_for,
    doc_id_for_source,
)
from kb.rag.splitter import split_document  # noqa: E402

VECTOR_DIM = 16


class CountingEmbedder:
    """确定性假嵌入器（零 API）：记录 embed 调用次数，用于验证「跳过不嵌入」。"""

    def __init__(self):
        self.embed_calls = 0
        self.embedded_texts = 0

    def embed(self, texts):
        texts = list(texts)
        self.embed_calls += 1
        self.embedded_texts += len(texts)
        return [self._vector(text) for text in texts]

    def embed_query(self, text):
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        digest = hashlib.md5(str(text).encode("utf-8")).digest()
        vector = [float(byte) for byte in digest[:VECTOR_DIM]]
        norm = math.sqrt(sum(value * value for value in vector)) or 1.0
        return [value / norm for value in vector]


def load_cli(name: str, path: Path):
    """按文件路径加载 scripts/ 下的 CLI 模块（加载过程不执行 main）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _config(tmp_path: Path, strategy_version: str = "v2") -> dict:
    """最小 config：chroma / state 都落 tmp_path，embedding 段给占位值（实际注入假 embedder）。"""
    return {
        "domain_kb": {
            "chroma_path": str(tmp_path / "chroma"),
            "state_path": str(tmp_path / "state"),
            "source_path": str(tmp_path / "source"),
            "strategy_version": strategy_version,
            "embedding": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-Embedding-8B",
                "api_key_env": "RAG_INCREMENTAL_TEST_KEY",
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


# ── 固定文档样例 ─────────────────────────────────────────────────

FULL_DOC = (
    "---\n"
    "title: 记忆层设计笔记\n"
    "source: notes/memory-a.md\n"
    "type: note\n"
    "updated: 2026-10-15\n"
    "tags: [memory, rag]\n"
    "---\n"
    "# 记忆层设计笔记\n\n"
    "## 一、目标\n\n"
    + "记忆层要把对话历史压成可检索的长期表示。" * 60
    + "\n\n## 二、口径\n\n"
    + "长对话按主题切段，段内保留原话，不做摘要改写。" * 60
    + "\n"
)

SHORT_DOC = (
    "---\n"
    "title: 记忆层设计笔记\n"
    "source: notes/memory-a.md\n"
    "type: note\n"
    "updated: 2026-10-16\n"
    "tags: [memory]\n"
    "---\n"
    "# 记忆层设计笔记\n\n"
    "## 一、目标\n\n改短了：只留一句结论。\n"
)

OTHER_DOC = (
    "---\n"
    "title: 上下文工程笔记\n"
    "source: notes/context-b.md\n"
    "type: doc\n"
    "updated: 2026-10-15\n"
    "---\n"
    "# 上下文工程笔记\n\n"
    "## 一、要点\n\n上下文窗口是最贵的资源，先砍无关内容。\n"
)

PLAIN_TEXT = "抓手是先把字段契约定死，再谈切片策略。"


def _ingester(tmp_path: Path, embedder=None, strategy_version: str = "v2") -> RAGIngest:
    return RAGIngest(
        _config(tmp_path, strategy_version), embedder=embedder or CountingEmbedder()
    )


def _source_ids(ingester: RAGIngest, source: str) -> list[str]:
    return list(ingester.collection.get(where={"source": source}).get("ids") or [])


def _source_documents(ingester: RAGIngest, source: str) -> list[str]:
    return list(ingester.collection.get(where={"source": source}).get("documents") or [])


# ── 1-3. 格式契约 ────────────────────────────────────────────────


def test_front_matter_required_fields_and_tags():
    meta = parse_document(FULL_DOC)

    assert meta["title"] == "记忆层设计笔记"
    assert meta["source"] == "notes/memory-a.md"
    assert meta["type"] == "note"
    # YAML 会把 2026-10-15 解析成 date，契约要求归一化为字符串
    assert meta["updated"] == "2026-10-15"
    assert meta["tags"] == ["memory", "rag"]
    assert meta["auto_wrapped"] is False
    assert meta["body"].lstrip().startswith("# 记忆层设计笔记")
    assert "---" not in meta["body"]


def test_front_matter_missing_required_lists_problems():
    text = "---\ntitle: 只有标题\nsource: notes/x.md\n---\n# 正文\n"

    with pytest.raises(DocumentFormatError) as excinfo:
        parse_document(text)

    error = excinfo.value
    assert "type" in error.problems
    assert "updated" in error.problems
    assert "title" not in error.problems and "source" not in error.problems
    message = str(error)
    assert "缺项清单" in message
    assert "type" in message and "updated" in message


def test_front_matter_blank_value_counts_as_missing():
    text = "---\ntitle: '   '\nsource: notes/x.md\ntype: note\nupdated: 2026-10-15\n---\n# 正文\n"

    with pytest.raises(DocumentFormatError) as excinfo:
        parse_document(text)

    assert excinfo.value.problems == ["title"]


def test_front_matter_invalid_updated_format_raises():
    text = "---\ntitle: T\nsource: s.md\ntype: note\nupdated: 昨天\n---\n# 正文\n"

    with pytest.raises(DocumentFormatError) as excinfo:
        parse_document(text)

    assert len(excinfo.value.problems) == 1
    assert excinfo.value.problems[0].startswith("updated(")
    assert "YYYY-MM-DD" in str(excinfo.value)


def test_front_matter_tags_optional_and_comma_string():
    text = "---\ntitle: T\nsource: s.md\ntype: snippet\nupdated: 2026-10-15\ntags: 竞品, 定价\n---\n# 正文\n"

    meta = parse_document(text)

    assert meta["tags"] == ["竞品", "定价"]
    assert meta["type"] == "snippet"


def test_plain_text_auto_wrap_uses_hash_source_and_note_type():
    meta = parse_document(PLAIN_TEXT)

    assert meta["auto_wrapped"] is True
    assert meta["type"] == "note"
    assert meta["source"] == auto_source(PLAIN_TEXT)
    assert meta["source"].startswith("hash:")
    assert len(meta["source"]) == len("hash:") + 16
    assert meta["title"] == "未命名文档"
    assert meta["body"] == PLAIN_TEXT


def test_plain_text_auto_wrap_title_fallback_chain():
    # 显式 title 优先
    assert parse_document(PLAIN_TEXT, title="手输笔记")["title"] == "手输笔记"
    # 其次取首个 # 标题
    assert parse_document("# 抓来的标题\n\n正文。")["title"] == "抓来的标题"
    # 再次取调用方给的兜底（如文件名）
    assert parse_document(PLAIN_TEXT, title_fallback="inbox-note")["title"] == "inbox-note"


def test_explicit_args_override_front_matter():
    meta = parse_document(FULL_DOC, title="改后的标题", source="notes/override.md", doc_type="doc")

    assert meta["title"] == "改后的标题"
    assert meta["source"] == "notes/override.md"
    assert meta["type"] == "doc"
    assert meta["updated"] == "2026-10-15"


def test_split_front_matter_edge_cases():
    assert split_front_matter("没有 frontmatter 的正文") == ({}, "没有 frontmatter 的正文")
    # 未闭合：整篇当正文，不进契约校验
    assert split_front_matter("---\ntitle: X\n")[0] == {}
    # YAML 顶层不是映射：当没有 frontmatter
    assert split_front_matter("---\n- a\n- b\n---\n正文")[0] == {}


# ── 4-5. ingest_document 基本行为与幂等 ──────────────────────────


def test_ingest_document_writes_deterministic_ids_and_metadata(tmp_path, capsys):
    ingester = _ingester(tmp_path)
    expected_chunks = len(split_document(FULL_DOC, "concept", {}))
    assert expected_chunks >= 3  # 样例文档足以切出多块（孤儿清理要用）

    stats = ingester.ingest_document(
        FULL_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记", updated="2026-10-15",
        tags=["memory", "rag"],
    )
    out = capsys.readouterr().out

    assert stats["skipped"] is False
    assert stats["chunks"] == expected_chunks
    assert stats["written"] == expected_chunks
    assert stats["removed"] == 0
    assert stats["strategy_version"] == "v2"
    assert "入库摘要" in out and "跳过=否" in out

    doc_id = doc_id_for_source("notes/memory-a.md")
    assert doc_id == hashlib.md5("notes/memory-a.md".encode("utf-8")).hexdigest()[:12]
    assert stats["doc_id"] == doc_id

    ids = _source_ids(ingester, "notes/memory-a.md")
    assert sorted(ids) == sorted(chunk_id_for(doc_id, "v2", index) for index in range(expected_chunks))
    assert all(chunk_id.count(":") == 2 for chunk_id in ids)
    assert all(chunk_id.split(":")[1] == "v2" for chunk_id in ids)

    meta = ingester.collection.get(where={"source": "notes/memory-a.md"})["metadatas"][0]
    # layer 是 v1 遗留桶（v2 schema 用 domain + quality 表达身份与质量），
    # 增量文档沿用 concept 这个 store.py 认得的层值，避免分层过滤漏掉增量文档
    assert meta["layer"] == "concept"
    assert meta["domain"] == "increment"
    assert meta["doc_type"] == "note"
    assert meta["chunk_type"] == "section"
    assert meta["quality"] == "user_input"
    assert meta["title"] == "记忆层设计笔记"
    assert meta["updated"] == "2026-10-15"
    assert meta["tags"] == "memory,rag"
    assert meta["strategy_version"] == "v2"
    assert meta["chunking_version"] == "v2"
    assert meta["content_hash"] == hashlib.sha256(FULL_DOC.encode("utf-8")).hexdigest()
    assert meta["source"] == "notes/memory-a.md"
    assert meta["doc_id"] == doc_id


def test_ingest_document_requires_source(tmp_path):
    ingester = _ingester(tmp_path)
    with pytest.raises(ValueError):
        ingester.ingest_document(PLAIN_TEXT, "note", "   ")


def test_reingest_same_document_is_idempotent(tmp_path, capsys):
    ingester = _ingester(tmp_path)

    first = ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记")
    ids_first = sorted(_source_ids(ingester, "notes/memory-a.md"))
    second = ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记")
    ids_second = sorted(_source_ids(ingester, "notes/memory-a.md"))
    out = capsys.readouterr().out

    assert first["skipped"] is False
    assert second["skipped"] is True
    assert second["written"] == 0
    assert second["chunks"] == 0  # 跳过：本次写入 0 块
    assert second["source_chunks"] == first["chunks"]  # 库内条数沿用上次结果，不翻倍
    assert ids_first == ids_second
    assert len(ids_second) == len(set(ids_second))  # 无重复 ID
    assert len(ingester.collection.get()["ids"]) == first["chunks"]
    assert "跳过=是" in out


def test_hash_skip_does_not_call_embedder(tmp_path):
    embedder = CountingEmbedder()
    ingester = _ingester(tmp_path, embedder=embedder)

    first = ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")
    calls_after_first = embedder.embed_calls
    assert calls_after_first == 1
    assert embedder.embedded_texts == first["chunks"]

    second = ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")

    assert second["skipped"] is True
    assert embedder.embed_calls == calls_after_first  # 跳过路径一次嵌入都没调
    assert embedder.embedded_texts == first["chunks"]


def test_state_table_persisted_under_state_dir(tmp_path):
    ingester = _ingester(tmp_path)
    ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")

    state_file = tmp_path / "state" / "ingest_state.json"
    assert state_file.is_file()
    payload = json.loads(state_file.read_text(encoding="utf-8"))
    entry = payload["sources"]["notes/memory-a.md"]

    assert entry["content_hash"]
    assert entry["strategy_version"] == "v2"
    assert len(entry["chunk_ids"]) >= 3
    assert entry["chunks"] == len(entry["chunk_ids"])
    assert entry["doc_type"] == "note"

    # 新实例（新进程等价）读同一状态表：直接跳过
    reopened = IngestState(tmp_path / "state")
    assert reopened.is_unchanged("notes/memory-a.md", entry["content_hash"], "v2") is True
    assert reopened.sources() == ["notes/memory-a.md"]


def test_strategy_version_change_triggers_reingest(tmp_path):
    ingester_v2 = _ingester(tmp_path, strategy_version="v2")
    ingester_v2.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")
    old_ids = _source_ids(ingester_v2, "notes/memory-a.md")
    assert all(chunk_id.split(":")[1] == "v2" for chunk_id in old_ids)

    ingester_v3 = _ingester(tmp_path, strategy_version="v3")
    stats = ingester_v3.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")
    new_ids = _source_ids(ingester_v3, "notes/memory-a.md")

    assert stats["skipped"] is False
    assert all(chunk_id.split(":")[1] == "v3" for chunk_id in new_ids)
    assert set(old_ids) & set(new_ids) == set()
    assert _source_ids(ingester_v3, "notes/memory-a.md") == new_ids


# ── 6-8. 只影响该 source / 孤儿清理 ──────────────────────────────


def test_modify_document_only_affects_its_own_source(tmp_path):
    ingester = _ingester(tmp_path)
    ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记")
    ingester.ingest_document(OTHER_DOC, "doc", "notes/context-b.md", title="上下文工程笔记")

    def _snapshot(source: str) -> dict:
        result = ingester.collection.get(where={"source": source}, include=["embeddings"])
        return {
            chunk_id: [round(float(value), 6) for value in vector]
            for chunk_id, vector in zip(result["ids"], result["embeddings"])
        }

    other_ids_before = sorted(_source_ids(ingester, "notes/context-b.md"))
    other_docs_before = list(_source_documents(ingester, "notes/context-b.md"))
    other_vectors_before = _snapshot("notes/context-b.md")
    assert other_vectors_before

    modified = FULL_DOC + "\n## 三、新增\n\n又加了一节结论。\n"
    stats = ingester.ingest_document(
        modified, "note", "notes/memory-a.md", title="记忆层设计笔记"
    )

    assert stats["skipped"] is False
    assert len(_source_ids(ingester, "notes/memory-a.md")) == stats["chunks"]

    # 另一个 source 的 ID、正文、向量都没动
    assert sorted(_source_ids(ingester, "notes/context-b.md")) == other_ids_before
    assert list(_source_documents(ingester, "notes/context-b.md")) == other_docs_before
    assert _snapshot("notes/context-b.md") == other_vectors_before


def test_orphan_cleanup_when_chunk_count_drops(tmp_path):
    ingester = _ingester(tmp_path)
    first = ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记")
    old_ids = set(_source_ids(ingester, "notes/memory-a.md"))
    assert len(old_ids) == first["chunks"] >= 3

    stats = ingester.ingest_document(SHORT_DOC, "note", "notes/memory-a.md", title="记忆层设计笔记")
    remaining = set(_source_ids(ingester, "notes/memory-a.md"))

    assert stats["chunks"] == 1
    assert stats["removed"] == len(old_ids)
    assert len(remaining) == 1
    # 新版只剩 1 块：索引 0 的 ID 设计上复用（邻居变化该块保留原 ID），
    # 其余旧块是孤儿，必须从库里消失
    assert remaining == {chunk_id_for(doc_id_for_source("notes/memory-a.md"), "v2", 0)}
    assert len(old_ids - remaining) == len(old_ids) - 1 >= 2


def test_orphan_cleanup_removes_residue_written_outside_the_pipeline(tmp_path):
    """状态表没记录、库里却挂在该 source 下的残块（旧策略/人工写入）也要清掉。"""
    ingester = _ingester(tmp_path)
    ingester.ingest_document(OTHER_DOC, "doc", "notes/context-b.md", title="上下文笔记")

    residue_id = f"{doc_id_for_source('notes/context-b.md')}:v1:99"
    ingester.collection.add(
        ids=[residue_id],
        embeddings=[[0.1] * VECTOR_DIM],
        documents=["上一个策略版本留下的残块"],
        metadatas=[{"source": "notes/context-b.md", "layer": "increment", "chunk_index": 99}],
    )
    assert residue_id in _source_ids(ingester, "notes/context-b.md")

    ingester.ingest_document(
        OTHER_DOC.replace("先砍无关内容。", "先砍无关内容，再谈排序。"),
        "doc",
        "notes/context-b.md",
        title="上下文笔记",
    )

    assert residue_id not in _source_ids(ingester, "notes/context-b.md")
    assert all(
        chunk_id.split(":")[1] == "v2" for chunk_id in _source_ids(ingester, "notes/context-b.md")
    )


def test_empty_document_clears_previous_chunks(tmp_path):
    """新版切不出块（例如正文被删空）时旧块也要清掉，不能留孤儿。"""
    ingester = _ingester(tmp_path)
    ingester.ingest_document(FULL_DOC, "note", "notes/memory-a.md", title="T")
    assert _source_ids(ingester, "notes/memory-a.md")

    stats = ingester.ingest_document("---\ntitle: T\nsource: notes/memory-a.md\ntype: note\nupdated: 2026-10-16\n---\n", "note", "notes/memory-a.md", title="T")

    assert stats["chunks"] == 0
    assert stats["removed"] >= 1
    assert _source_ids(ingester, "notes/memory-a.md") == []


def test_existing_concept_and_paper_paths_untouched(tmp_path):
    """本任务不动存量两条路径：ingest_concept_dir / ingest_papers 行为不变、不写状态表。"""
    ingester = _ingester(tmp_path)
    _write(
        tmp_path / "concept" / "Concepts" / "a.md",
        "# A\n\n概念层文档正文。\n",
    )
    total = ingester.ingest_concept_dir(str(tmp_path / "concept"))

    assert total == 1
    assert not (tmp_path / "state" / "ingest_state.json").exists()
    meta = ingester.collection.get(where={"source": "concept/Concepts/a.md"})["metadatas"][0]
    # 存量概念层走 CHUNKING_VERSION：该常量值随切片规则变更而升（切分策略变更即
    # 全库重建信号），故断言对常量本身，不写死具体版本号
    assert meta["chunking_version"] == CHUNKING_VERSION
    assert "strategy_version" not in meta
    assert "doc_id" not in meta


# ── 9-11. CLI：--incremental ────────────────────────────────────


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """scripts/rag_ingest.py 模块对象 + 注入 tmp 配置与真 RAGIngest（假 embedder）。"""
    module = load_cli(f"rag_ingest_incremental_{tmp_path.name}", SCRIPTS_DIR / "rag_ingest.py")
    cfg = _config(tmp_path)
    monkeypatch.setattr(module, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(
        module, "build_ingester", lambda config: RAGIngest(config, embedder=CountingEmbedder())
    )
    module._cfg = cfg  # 便于用例取路径
    return module


def _inbox_dir(tmp_path: Path) -> Path:
    return tmp_path / "source" / "inbox"


def test_cli_incremental_file_ingests_and_reads_back(cli, tmp_path, capsys):
    path = _write(tmp_path / "inbox_src" / "memory-note.md", FULL_DOC)

    code = cli.main(["--incremental", "--file", str(path)])
    out = capsys.readouterr().out

    assert code == 0
    assert "模式: 增量单文档" in out
    assert "source: notes/memory-a.md" in out
    assert "入库摘要" in out and "跳过=否" in out
    assert f"写入 chunk 总数: {len(split_document(FULL_DOC, 'concept', {}))}" in out

    reopened = RAGIngest(cli._cfg, embedder=CountingEmbedder())
    assert len(_source_ids(reopened, "notes/memory-a.md")) == len(
        split_document(FULL_DOC, "concept", {})
    )

    # 再跑一次：内容未变 → 跳过，退出码仍为 0
    code_again = cli.main(["--incremental", "--file", str(path)])
    out_again = capsys.readouterr().out
    assert code_again == 0
    assert "跳过: 是" in out_again
    assert "写入 chunk 总数: 0" in out_again
    assert "库内已有 chunk" in out_again


def test_cli_incremental_file_missing_required_returns_1(cli, tmp_path, capsys):
    path = _write(
        tmp_path / "bad.md",
        "---\ntitle: 缺 updated\nsource: notes/bad.md\ntype: note\n---\n# 正文\n\n内容。\n",
    )

    code = cli.main(["--incremental", "--file", str(path)])
    err = capsys.readouterr().err

    assert code == 1
    assert "入库失败" in err
    assert "缺项清单" in err and "updated" in err
    # 契约失败发生在构造入库器之前：不建库、不入库
    assert not (tmp_path / "chroma").exists()


def test_cli_incremental_file_not_found_returns_1(cli, tmp_path, capsys):
    code = cli.main(["--incremental", "--file", str(tmp_path / "不存在.md")])

    assert code == 1
    assert "入库失败" in capsys.readouterr().err


def test_cli_incremental_text_auto_wraps(cli, capsys):
    code = cli.main(["--incremental", "--text", PLAIN_TEXT, "--title", "手输笔记"])
    out = capsys.readouterr().out

    assert code == 0
    assert "source: hash:" in out
    assert "文档类型: note（自动包壳: 是）" in out
    assert "跳过: 否" in out


def test_cli_incremental_argument_errors_exit_2(cli):
    for argv in (
        ["--incremental"],  # 既没 --file 也没 --text
        ["--incremental", "--file", "a.md", "--text", "文本"],  # 两者都给
        [],  # 一个入口都没给（存量批量的 --layer 也缺）
        ["--layer", "concept", "--incrementaal"],  # 未知参数
    ):
        with pytest.raises(SystemExit) as excinfo:
            cli.main(argv)
        assert excinfo.value.code == 2


# ── 12. CLI：--inbox ────────────────────────────────────────────


def test_cli_inbox_ingests_then_skips(cli, tmp_path, capsys):
    inbox = _inbox_dir(tmp_path)
    _write(inbox / "memory-note.md", FULL_DOC)
    _write(inbox / "手输片断.txt", PLAIN_TEXT)

    code = cli.main(["--inbox", str(inbox)])
    out = capsys.readouterr().out

    assert code == 0
    assert "模式: inbox 目录投递" in out
    assert "扫描文件: 2" in out
    assert "入库: 2" in out
    assert "跳过: 0" in out
    assert "失败: 0" in out

    code_again = cli.main(["--inbox", str(inbox)])
    out_again = capsys.readouterr().out
    assert code_again == 0
    assert "入库: 0" in out_again
    assert "跳过: 2" in out_again

    # 改其中一篇（有 frontmatter、source 稳定）：只重入该篇，另一篇按 hash 跳过
    _write(inbox / "memory-note.md", FULL_DOC.replace("## 二、口径", "## 二、口径（改）") + "\n补充一行。\n")
    code_third = cli.main(["--inbox", str(inbox)])
    out_third = capsys.readouterr().out
    assert code_third == 0
    assert "入库: 1" in out_third
    assert "跳过: 1" in out_third


def test_cli_inbox_default_dir_uses_source_path(cli, tmp_path, capsys):
    inbox = _inbox_dir(tmp_path)
    _write(inbox / "a.md", OTHER_DOC)

    code = cli.main(["--inbox"])
    out = capsys.readouterr().out

    assert code == 0
    assert f"目录: {inbox}" in out
    assert "入库: 1" in out


def test_cli_inbox_missing_dir_returns_1(cli, tmp_path, capsys):
    code = cli.main(["--inbox", str(tmp_path / "没有这个目录")])

    assert code == 1
    assert "入库失败" in capsys.readouterr().err


def test_cli_inbox_counts_bad_file_as_failed_and_continues(cli, tmp_path, capsys):
    inbox = _inbox_dir(tmp_path)
    _write(inbox / "good.md", OTHER_DOC)
    _write(inbox / "bad.md", "---\ntitle: 缺字段\n---\n# 正文\n")

    code = cli.main(["--inbox", str(inbox)])
    captured = capsys.readouterr()

    assert code == 0
    assert "扫描文件: 2" in captured.out
    assert "入库: 1" in captured.out
    assert "失败: 1" in captured.out
    assert "单篇失败" in captured.err


def test_cli_inbox_scan_ignores_hidden_and_other_suffixes(cli, tmp_path, capsys):
    inbox = _inbox_dir(tmp_path)
    _write(inbox / "keep.md", OTHER_DOC)
    _write(inbox / ".hidden.md", PLAIN_TEXT)
    _write(inbox / "_draft.md", PLAIN_TEXT)
    _write(inbox / "notes.json", "{}")
    _write(inbox / ".hidden_dir" / "inside.md", PLAIN_TEXT)

    code = cli.main(["--inbox", str(inbox)])
    out = capsys.readouterr().out

    assert code == 0
    assert "扫描文件: 1" in out
    assert "入库: 1" in out
