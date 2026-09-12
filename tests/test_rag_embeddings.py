# [C 2026-09-12] RAG 基础框架自测 - 嵌入封装（httpx 全假，零真实 API）
"""EmbeddingModel 零成本自测：monkeypatch httpx.post 造假响应，不发起任何真实请求。

覆盖：
1. 单批成功路径（URL / 请求头 / 请求体 / 返回值）；
2. 70 条输入按 batch_size=32 分 3 批（32/32/6）；
3. 响应 data 乱序时按 index 字段还原为输入顺序；
4. 前 2 次返回 500、第 3 次成功（验证重试 2 次 + 指数退避，sleep 被打桩）；
5. 重试耗尽后抛 RuntimeError 且带失败批号；
6. 密钥环境变量缺失/为空抛 RuntimeError；
7. embed_query 单条包装；
8. config.yaml 的 domain_kb.embedding 段字段符合约定。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_rag_embeddings.py -q
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import httpx  # noqa: E402
import pytest  # noqa: E402
import yaml  # noqa: E402

from kb.rag.embeddings import EmbeddingModel  # noqa: E402

TEST_ENV_NAME = "RAG_TEST_EMBED_KEY"


class _FakeResponse:
    """假 httpx 响应：只需要 status_code 与 json()。"""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}

    def json(self) -> dict:
        return self._payload


def _config(**overrides) -> dict:
    cfg = {
        "provider": "siliconflow",
        "model": "Qwen/Qwen3-Embedding-8B",
        "api_key_env": TEST_ENV_NAME,
        "api_base": "https://api.siliconflow.cn/v1",
        "dimensions": 4096,
        "batch_size": 32,
    }
    cfg.update(overrides)
    return cfg


def _vector(text: str) -> list[float]:
    """用文本可辨识的假向量：首字符 ASCII + 文本长度。"""
    return [float(ord(text[0])), float(len(text))]


def _fake_post_factory(calls: list[dict], statuses: list[int] | None = None, reverse: bool = False):
    """造 httpx.post 替身：记录每次调用；statuses 按调用序号取状态码（缺省全 200）。

    reverse=True 时把 data 数组倒序返回，用于验证按 index 还原顺序。
    """
    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        index = len(calls)
        calls.append(
            {"url": url, "json": json, "headers": headers, "timeout": timeout}
        )
        status = statuses[min(index, len(statuses) - 1)] if statuses else 200
        if status != 200:
            return _FakeResponse(status, {})
        inputs = list(json["input"])
        data = [
            {"index": i, "embedding": _vector(text)}
            for i, text in enumerate(inputs)
        ]
        if reverse:
            data.reverse()
        return _FakeResponse(200, {"data": data})

    return fake_post


@pytest.fixture()
def api_key(monkeypatch):
    monkeypatch.setenv(TEST_ENV_NAME, "test-key-123")
    return "test-key-123"


@pytest.fixture()
def no_sleep(monkeypatch):
    """打桩 time.sleep，避免测试真的等待退避时长。"""
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    return []


# ── 1. 单批成功路径 ───────────────────────────────────────────────

def test_single_batch_success(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    texts = ["alpha", "be", "gamma"]
    vectors = EmbeddingModel(_config()).embed(texts)

    assert len(calls) == 1
    assert calls[0]["url"] == "https://api.siliconflow.cn/v1/embeddings"
    assert calls[0]["headers"]["Authorization"] == "Bearer test-key-123"
    assert calls[0]["json"] == {"model": "Qwen/Qwen3-Embedding-8B", "input": texts}
    assert calls[0]["timeout"] == 60.0
    assert vectors == [_vector(t) for t in texts]


def test_api_base_trailing_slash_normalized(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    EmbeddingModel(_config(api_base="https://api.siliconflow.cn/v1/")).embed(["x"])

    assert calls[0]["url"] == "https://api.siliconflow.cn/v1/embeddings"


def test_empty_input_no_request(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    assert EmbeddingModel(_config()).embed([]) == []
    assert calls == []


# ── 2. 分批 ──────────────────────────────────────────────────────

def test_seventy_inputs_split_into_three_batches(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    texts = [f"text-{i}" for i in range(70)]
    vectors = EmbeddingModel(_config()).embed(texts)

    assert len(calls) == 3
    assert [len(c["json"]["input"]) for c in calls] == [32, 32, 6]
    assert len(vectors) == 70
    assert vectors == [_vector(t) for t in texts]


def test_batch_size_defaults_to_32(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    model = EmbeddingModel(_config(batch_size=None))
    assert model.batch_size == 32
    model.embed([f"t{i}" for i in range(33)])
    assert [len(c["json"]["input"]) for c in calls] == [32, 1]


# ── 3. 按 index 还原顺序 ─────────────────────────────────────────

def test_result_order_restored_by_index(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls, reverse=True))

    texts = ["aa", "bbbb", "c"]
    vectors = EmbeddingModel(_config()).embed(texts)

    assert vectors == [_vector(t) for t in texts]


# ── 4. 重试 ─────────────────────────────────────────────────────

def test_retry_twice_then_success(monkeypatch, api_key, no_sleep):
    calls: list[dict] = []
    monkeypatch.setattr(
        httpx, "post", _fake_post_factory(calls, statuses=[500, 500, 200])
    )

    vectors = EmbeddingModel(_config()).embed(["ok"])

    assert len(calls) == 3
    assert vectors == [_vector("ok")]


def test_retry_exhausted_raises_with_batch_no(monkeypatch, api_key, no_sleep):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls, statuses=[500]))

    with pytest.raises(RuntimeError) as err:
        EmbeddingModel(_config()).embed(["ok"])

    assert len(calls) == 3  # 首次 + 2 次重试
    assert "1/1" in str(err.value)


def test_network_exception_retried_then_raises(monkeypatch, api_key, no_sleep):
    calls: list[dict] = []

    def fake_post(url, json=None, headers=None, timeout=None, **kwargs):
        calls.append(url)
        raise httpx.ConnectTimeout("连接超时")

    monkeypatch.setattr(httpx, "post", fake_post)

    with pytest.raises(RuntimeError) as err:
        EmbeddingModel(_config()).embed(["ok"])

    assert len(calls) == 3
    assert "ConnectTimeout" in str(err.value)


def test_second_batch_failure_reports_batch_number(monkeypatch, api_key, no_sleep):
    calls: list[dict] = []
    # 第 1 批（32 次调用内）全 200，第 2 批失败
    statuses = [200] * 1 + [500] * 3
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls, statuses=statuses))

    with pytest.raises(RuntimeError) as err:
        EmbeddingModel(_config()).embed([f"t{i}" for i in range(33)])

    assert len(calls) == 4  # 第 1 批 1 次 + 第 2 批 3 次
    assert "2/2" in str(err.value)


# ── 5. 密钥缺失 ──────────────────────────────────────────────────

def test_missing_api_key_env_raises(monkeypatch):
    monkeypatch.delenv(TEST_ENV_NAME, raising=False)
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    with pytest.raises(RuntimeError) as err:
        EmbeddingModel(_config()).embed(["x"])

    assert TEST_ENV_NAME in str(err.value)
    assert calls == []  # 缺密钥时不应发起请求


def test_empty_api_key_env_raises(monkeypatch):
    monkeypatch.setenv(TEST_ENV_NAME, "   ")

    with pytest.raises(RuntimeError):
        EmbeddingModel(_config()).embed_query("x")


# ── 6. embed_query ───────────────────────────────────────────────

def test_embed_query_returns_single_vector(monkeypatch, api_key):
    calls: list[dict] = []
    monkeypatch.setattr(httpx, "post", _fake_post_factory(calls))

    vector = EmbeddingModel(_config()).embed_query("查询文本")

    assert len(calls) == 1
    assert calls[0]["json"]["input"] == ["查询文本"]
    assert vector == _vector("查询文本")


# ── 7. config.yaml 契约 ──────────────────────────────────────────

def test_config_yaml_domain_kb_embedding():
    config = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    embedding = config["domain_kb"]["embedding"]

    assert embedding["provider"] == "siliconflow"
    assert embedding["model"] == "Qwen/Qwen3-Embedding-8B"
    assert embedding["api_key_env"] == "SILICONFLOW_API_KEY"
    assert embedding["api_base"] == "https://api.siliconflow.cn/v1"
    assert embedding["dimensions"] == 4096
    assert embedding["batch_size"] == 32


# [C 2026-09-12 by pi-deepseek-v4-flash]
