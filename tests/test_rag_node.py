# 领域知识库检索接入 kb_lookup 节点自测
"""R02 自测：kb_lookup 节点接领域知识库（kb.rag）+ 两个 prompt 的领域库条件块 + 启动装配函数。

零网络、零真实模型 API、零真实 chroma 实例：
- 节点侧用 types.SimpleNamespace(kb=..., rag=...) 构造依赖容器，直接调 make_kb_lookup(deps)(state)，
  检索器用 FakeRAG（记录参数、可注入异常）；
- prompt 侧只用 jinja2.Template 渲染磁盘模板，不调模型；
- build_rag_store_if_available 的存在性分支用 monkeypatch 替换 kb.rag.store.RAGStore 为假类，
  config={} 分支在临时工作目录下执行以避开项目内已建成的 domain_kb/chroma（保证不触碰真实向量库）。

覆盖 8 个用例，见下方 test_ 函数名。
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Template

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from components.registry import ComponentRegistry  # noqa: E402
from kb.rag import build_rag_store_if_available  # noqa: E402
from nodes.exploration import make_kb_lookup  # noqa: E402

COMPONENTS_DIR = SRC_DIR / "components"

# 罐头检索结果：title/content/layer/category 键齐全（模板渲染只依赖这几个键）
CANNED_RESULTS = [
    {
        "id": "concept-1",
        "content": "提示工程是通过设计输入文本引导大模型稳定产出的方法-AAA。",
        "score": 0.91,
        "layer": "concept",
        "category": "prompt",
        "source": "domain_kb/source/concept.md",
        "title": "提示工程",
        "section_title": "定义",
        "chunk_index": 0,
        "vector_sim": 0.82,
        "keyword_norm": 0.75,
    },
    {
        "id": "paper-1",
        "content": "本文提出一种检索增强生成流水线-XXX。",
        "score": 0.77,
        "layer": "curated-paper",
        "category": "rag",
        "source": "domain_kb/source/paper.md",
        "title": "检索增强生成综述",
        "section_title": "摘要",
        "chunk_index": 1,
        "vector_sim": 0.7,
        "keyword_norm": 0.6,
    },
]


class FakeKB:
    """业务档案库（KBStore）替身：记录查询、返回罐头结果。"""

    def __init__(self, result=None):
        self.result = result if result is not None else {"archives": ["kb-1"]}
        self.calls: list[str] = []

    def retrieve_relevant(self, query: str):
        self.calls.append(query)
        return self.result


class FakeRAG:
    """领域知识库（RAGStore）替身：记录检索参数，可注入异常。"""

    def __init__(self, results=None, error=None, default_top_k=5):
        self.results = list(results) if results is not None else []
        self.error = error
        self.default_top_k = default_top_k
        self.calls: list[dict] = []

    def search(self, query: str, top_k: int = 5, **kwargs):
        self.calls.append({"query": query, "top_k": top_k})
        if self.error is not None:
            raise self.error
        return list(self.results)


def make_deps(kb=None, rag=None):
    """按节点接线方式构造最小依赖容器（R02 后 NodeDeps 多一个 rag 字段）。"""
    return SimpleNamespace(kb=kb if kb is not None else FakeKB(), rag=rag)


def read_prompt(name: str) -> str:
    """从 src/components 读原始 prompt 文本。"""
    registry = ComponentRegistry(str(COMPONENTS_DIR))
    return registry.read_prompt(name)


# ────────────────────────── 1. 节点：正常检索 ──────────────────────────


def test_kb_lookup_calls_rag_and_stores_results():
    fake_kb = FakeKB(result={"archives": ["kb-1"]})
    fake_rag = FakeRAG(results=CANNED_RESULTS, default_top_k=5)
    deps = make_deps(kb=fake_kb, rag=fake_rag)

    out = make_kb_lookup(deps)({"raw_requirement": "做会议纪要总结工具"})

    assert out["domain_kb_context"] == CANNED_RESULTS
    assert out["kb_context"] == {"archives": ["kb-1"]}
    assert out["current_stage"] == "探索"
    assert len(fake_rag.calls) == 1
    assert fake_rag.calls[0]["query"] == "做会议纪要总结工具"
    assert fake_rag.calls[0]["top_k"] == 5
    assert fake_rag.calls[0]["top_k"] == fake_rag.default_top_k


# ────────────────────────── 2. 节点：不接领域库 ──────────────────────────


def test_kb_lookup_rag_none_returns_empty():
    fake_kb = FakeKB(result={"archives": ["kb-2"]})
    deps = make_deps(kb=fake_kb, rag=None)

    out = make_kb_lookup(deps)({"raw_requirement": "做账号登录"})

    assert out["domain_kb_context"] == []
    assert out["kb_context"] == {"archives": ["kb-2"]}
    assert out["current_stage"] == "探索"


# ────────────────────────── 3. 节点：检索异常降级 ──────────────────────────


def test_kb_lookup_rag_exception_degrades():
    fake_rag = FakeRAG(error=RuntimeError("向量库炸了"))
    deps = make_deps(rag=fake_rag)

    out = make_kb_lookup(deps)({"raw_requirement": "做会议纪要总结工具"})

    assert out["domain_kb_context"] == []
    assert out["kb_context"] == {"archives": ["kb-1"]}
    assert len(fake_rag.calls) == 1


# ────────────────────────── 4. 节点：空 query 仍调用检索 ──────────────────────────


def test_kb_lookup_empty_query_still_calls():
    fake_rag = FakeRAG(results=[])  # 空 query 时真实检索器返回 []
    deps = make_deps(rag=fake_rag)

    out = make_kb_lookup(deps)({"raw_requirement": ""})

    assert "domain_kb_context" in out
    assert out["domain_kb_context"] == []
    assert fake_rag.calls[0]["query"] == ""


# ────────────────────────── 5/6. needs_discovery 模板条件块 ──────────────────────────


def test_needs_discovery_prompt_renders_block_when_results():
    raw = read_prompt("needs_discovery")
    rendered = Template(raw).render(
        domain_kb_context=CANNED_RESULTS,
        confirmed_requirement="做会议纪要总结工具",
    )

    assert "AI 领域知识库参考" in rendered
    assert "提示工程" in rendered
    assert "检索增强生成综述" in rendered
    assert "材料未覆盖的方面仍按需求本身推断" in rendered


def test_needs_discovery_prompt_hides_block_when_empty():
    raw = read_prompt("needs_discovery")
    rendered = Template(raw).render(
        domain_kb_context=[],
        confirmed_requirement="做会议纪要总结工具",
    )

    assert "AI 领域知识库参考" not in rendered
    assert "提示工程" not in rendered
    assert "## 思考方向" in rendered


# ────────────────────────── 7. feasibility_check 模板条件块 ──────────────────────────


def test_feasibility_prompt_renders_and_hides():
    raw = read_prompt("feasibility_check")
    common = {
        "requirement_name": "demo-req",
        "confirmed_requirement": "做会议纪要总结工具",
        "ai_triage": {},
    }

    rendered_with = Template(raw).render(
        domain_kb_context=CANNED_RESULTS[:1], **common
    )
    assert "AI 领域知识库参考" in rendered_with
    assert "提示工程" in rendered_with
    assert "参考材料只作为能力点判断依据之一" in rendered_with

    rendered_empty = Template(raw).render(domain_kb_context=[], **common)
    assert "AI 领域知识库参考" not in rendered_empty
    # 第 1 步标题由"三色判断"改为"三方对照"，同步断言
    assert "## 第 1 步：关键能力点三方对照" in rendered_empty


# ────────────────────────── 8. build_rag_store_if_available 装配分支 ──────────────────────────


class _FakeRAGStoreWithCount:
    """假 RAGStore：构造只收 config，collection.count() 返回固定值。"""

    count_value = 0
    instances: list["_FakeRAGStoreWithCount"] = []

    def __init__(self, config):
        self.config = config
        self.collection = SimpleNamespace(count=lambda: type(self).count_value)
        type(self).instances.append(self)


def test_build_rag_store_if_available_paths(tmp_path, monkeypatch):
    # 路径不存在 -> None（绝对路径，不碰项目内真实向量库）
    assert (
        build_rag_store_if_available(
            {"domain_kb": {"chroma_path": str(tmp_path / "nope")}}
        )
        is None
    )

    # config={} -> 落到默认相对路径 ./domain_kb/chroma；临时工作目录下该路径不存在 -> None
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    monkeypatch.chdir(workdir)
    assert build_rag_store_if_available({}) is None

    # 路径存在且集合非空 -> 返回假实例；集合为空 -> None
    chroma_dir = tmp_path / "chroma_exists"
    chroma_dir.mkdir()  # 真实存在的空目录，通过 exists 检查；假类构造不建 chroma
    config = {"domain_kb": {"chroma_path": str(chroma_dir)}}

    monkeypatch.setattr("kb.rag.store.RAGStore", _FakeRAGStoreWithCount)
    _FakeRAGStoreWithCount.instances.clear()

    _FakeRAGStoreWithCount.count_value = 0
    assert build_rag_store_if_available(config) is None

    _FakeRAGStoreWithCount.count_value = 3
    store = build_rag_store_if_available(config)
    assert isinstance(store, _FakeRAGStoreWithCount)
    assert store.config == config


