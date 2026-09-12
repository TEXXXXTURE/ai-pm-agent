# [C 2026-09-12] R3 知识库 CLI 自测 - build_ingester / build_store 整体替换为假对象（零真实 API、零真实 ChromaDB）
"""rag_ingest.py / rag_query.py 零成本自测。

做法：用 importlib 按文件路径加载 scripts/ 下的两个 CLI 模块（scripts 不在 src/
目录下，不能直接 import），再 monkeypatch 模块内的 build_ingester / build_store
返回假对象，从而完全不触发嵌入请求与 ChromaDB 实例化。

覆盖（共 8 个用例）：
1. ingest concept 默认：调到 ingest_concept_dir，stdout 含层名/源路径/chunk 数/耗时，退出 0；
2. ingest curated-paper：读 tmp JSON 后调到 ingest_papers 且 layer 正确；
3. --clean：delete_by_layer 被调一次且输出含清空条数；
4. --source 路径不存在：退出码 1；
5. 缺 --layer：退出码 2（argparse 默认行为）；
6. query 默认文本输出：含 title、[layer/category]、source，退出 0；
7. query --json：可 json.loads 还原且 11 字段齐全，--layers 拆列表、--category/--top-k 透传；
8. query 无结果：退出 0，文本模式含"未检索到"。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_cli.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
# 脚本在 scripts/ 不在 src/，项目根与 src 都要进 sys.path
for _candidate in (str(SRC_DIR), str(REPO_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import pytest  # noqa: E402


def _load_module(name: str, path: Path):
    """按文件路径加载 scripts/ 下的 CLI 模块（加载过程不执行 main）。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def ingest_cli():
    """scripts/rag_ingest.py 模块对象。"""
    return _load_module("rag_ingest_under_test", SCRIPTS_DIR / "rag_ingest.py")


@pytest.fixture
def query_cli():
    """scripts/rag_query.py 模块对象。"""
    return _load_module("rag_query_under_test", SCRIPTS_DIR / "rag_query.py")


# 检索结果固定样例：11 个字段与 RAGStore.search 输出一致
QUERY_RESULT = {
    "id": "curated-2401.00001",
    "content": "ReAct 让 Agent 交替进行推理与行动。",
    "score": 1.2345,
    "layer": "concept",
    "category": "planning",
    "source": "concept/Concepts/react.md",
    "title": "ReAct 综述",
    "section_title": "定义",
    "chunk_index": 0,
    "vector_sim": 0.8123,
    "keyword_norm": 0.5,
}


class FakeIngester:
    """假入库器：记录调用参数，返回固定计数，不碰 ChromaDB 与嵌入 API。"""

    def __init__(self):
        self.calls = []

    def ingest_concept_dir(self, dir_path, progress_every=0):
        self.calls.append(("ingest_concept_dir", dir_path, progress_every))
        return 12

    def ingest_papers(self, papers, layer="curated-paper", progress_every=0):
        self.calls.append(("ingest_papers", papers, layer, progress_every))
        return 7

    def delete_by_layer(self, layer):
        self.calls.append(("delete_by_layer", layer))
        return 3


class FakeStore:
    """假检索器：记录 search kwargs，返回构造时给定的结果列表。"""

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def search(self, **kwargs):
        self.calls.append(kwargs)
        return list(self.results)


@pytest.fixture
def fake_ingester(ingest_cli, monkeypatch):
    """把 build_ingester 换成返回假入库器的函数。"""
    fake = FakeIngester()
    monkeypatch.setattr(ingest_cli, "build_ingester", lambda config: fake)
    return fake


@pytest.fixture
def make_fake_store(query_cli, monkeypatch):
    """返回工厂：make_fake_store([结果...]) 装上假 store 并返回它。"""

    def _make(results):
        fake = FakeStore(results)
        monkeypatch.setattr(query_cli, "build_store", lambda config: fake)
        return fake

    return _make


# ── 1-5. 入库 CLI ────────────────────────────────────────────────

def test_ingest_concept_default(ingest_cli, fake_ingester, tmp_path, capsys):
    source = tmp_path / "concept"
    source.mkdir()

    code = ingest_cli.main(["--layer", "concept", "--source", str(source)])
    out = capsys.readouterr().out

    assert code == 0
    assert fake_ingester.calls == [("ingest_concept_dir", str(source), 20)]
    assert "层名: concept" in out
    assert f"源路径: {source}" in out
    assert "写入 chunk 总数: 12" in out
    assert "耗时:" in out and "秒" in out


def test_ingest_curated_paper_from_json(ingest_cli, fake_ingester, tmp_path, capsys):
    papers = [{"arxiv_id": "2401.00001", "title": "T", "abstract": "A" * 60}]
    json_path = tmp_path / "papers_with_abstract.json"
    json_path.write_text(json.dumps(papers), encoding="utf-8")

    code = ingest_cli.main(
        ["--layer", "curated-paper", "--source", str(json_path)]
    )
    out = capsys.readouterr().out

    assert code == 0
    name, passed_papers, layer, progress_every = fake_ingester.calls[0]
    assert name == "ingest_papers"
    assert passed_papers == papers
    assert layer == "curated-paper"
    assert progress_every == 50
    assert "写入 chunk 总数: 7" in out


def test_ingest_clean_calls_delete_by_layer(ingest_cli, fake_ingester, tmp_path, capsys):
    source = tmp_path / "concept"
    source.mkdir()

    code = ingest_cli.main(
        ["--layer", "concept", "--source", str(source), "--clean"]
    )
    out = capsys.readouterr().out

    assert code == 0
    assert fake_ingester.calls.count(("delete_by_layer", "concept")) == 1
    assert "清空条数: 3" in out
    # 清空在入库之前
    assert fake_ingester.calls[0] == ("delete_by_layer", "concept")


def test_ingest_missing_source_returns_1(ingest_cli, fake_ingester, tmp_path, capsys):
    missing = tmp_path / "not-exist"

    code = ingest_cli.main(["--layer", "concept", "--source", str(missing)])
    captured = capsys.readouterr()

    assert code == 1
    assert captured.err.strip() != ""
    assert "入库失败" in captured.err
    # 路径校验在构造入库器之前，不应触发任何入库调用
    assert fake_ingester.calls == []


def test_ingest_missing_layer_returns_2(ingest_cli):
    with pytest.raises(SystemExit) as excinfo:
        ingest_cli.main([])

    assert excinfo.value.code == 2


# ── 6-8. 检索 CLI ────────────────────────────────────────────────

def test_query_text_output(query_cli, make_fake_store, capsys):
    fake = make_fake_store([dict(QUERY_RESULT)])

    code = query_cli.main(["--query", "什么是 ReAct"])
    out = capsys.readouterr().out

    assert code == 0
    assert "ReAct 综述" in out
    assert "[concept/planning]" in out
    assert "score=1.2345" in out
    assert "source: concept/Concepts/react.md" in out
    assert "ReAct 让 Agent 交替进行推理与行动。" in out
    # 未给 --top-k 时用 config 的 retrieval.top_k（config.yaml 为 5）
    assert fake.calls[0]["query"] == "什么是 ReAct"
    assert fake.calls[0]["top_k"] == 5
    assert fake.calls[0]["layers"] is None
    assert fake.calls[0]["category"] is None


def test_query_json_and_filter_passthrough(query_cli, make_fake_store, capsys):
    fake = make_fake_store([dict(QUERY_RESULT)])

    code = query_cli.main(
        [
            "--query", "planning",
            "--layers", "a,b",
            "--category", "planning",
            "--top-k", "3",
            "--json",
        ]
    )
    out = capsys.readouterr().out

    assert code == 0
    payload = json.loads(out)
    assert len(payload) == 1
    assert payload[0]["title"] == "ReAct 综述"
    assert len(payload[0]) == 11
    assert set(payload[0]) == set(QUERY_RESULT)

    kwargs = fake.calls[0]
    assert kwargs["layers"] == ["a", "b"]
    assert kwargs["category"] == "planning"
    assert kwargs["top_k"] == 3


def test_query_no_result_returns_0(query_cli, make_fake_store, capsys):
    make_fake_store([])

    code = query_cli.main(["--query", "不存在的主题"])
    out = capsys.readouterr().out

    assert code == 0
    assert "未检索到" in out


# [C 2026-09-12 by pi-deepseek-v4-flash]
