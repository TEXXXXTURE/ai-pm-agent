# 飞书同步自测 - 假 HTTP 传输 + 假 embedder + 真实临时 ChromaDB（零真实网络/API）
"""飞书入口零成本自测：HTTP 全走注入的假 transport，嵌入走假 embedder，Chroma 用 tmp_path。

覆盖：
1. 授权 URL：/authen/v1/authorize、redirect_uri 与 scope 都 urlencode、scope 含四项必需权限；
2. 凭据零密钥：app_id/app_secret 从环境变量文件读，config 里只有变量名；
3. 刷新链路：v3 端点 + client_id/client_secret + refresh_token 一次性轮换回写；
4. 刷新失败（20037 invalid_grant）：registry 不被破坏、抛 FeishuAuthError 且标记需重新授权；
5. access token 过期（99991677）：自动刷新一次并重试一次，成功返回；
6. 空间分页（has_more/page_token）与节点广度遍历（has_child 递归）；
7. 正文拉取：docx raw_content + 标题补 Markdown 一级标题；非 docx 类型抛错（交给调用方跳过）；
8. 入库调用点适配：真实 RAGIngest + 假 embedder → 回读能查到刚入内容，
   metadata 带 domain=increment / doc_type=note / feishu_ref / source=飞书文档 URL；
9. 幂等：同一 source 重跑两次，chunk 数不翻倍；
10. CLI：--from-feishu --list-spaces 出空间清单；缺 --space 退出 2；身份失效退出 3。

运行：
  bash scripts/run-tool.sh -m pytest tests/test_feishu_sync.py -q
"""
from __future__ import annotations

import importlib.util
import json
import sys
import urllib.parse
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
SCRIPTS_DIR = REPO_ROOT / "scripts"
for _candidate in (str(SRC_DIR), str(REPO_ROOT)):
    if _candidate not in sys.path:
        sys.path.insert(0, _candidate)

import pytest  # noqa: E402

from kb import feishu_sync  # noqa: E402
from kb.feishu_sync import (  # noqa: E402
    FeishuAuthError,
    FeishuSync,
    FeishuSyncError,
    build_auth_url,
    ingest_document,
    verify_source,
)
from kb.rag.ingest import RAGIngest  # noqa: E402

APP_ID = "cli_test_app"
APP_SECRET = "test-secret-value"


@pytest.fixture(autouse=True)
def _no_ambient_feishu_env(monkeypatch):
    """清掉进程里可能存在的 FEISHU_* 变量，保证测的是「从 env 文件读」这条路径。"""
    for key in ("FEISHU_APP_ID", "FEISHU_APP_SECRET", "FEISHU_DOMAIN", "FEISHU_TOKEN_REGISTRY"):
        monkeypatch.delenv(key, raising=False)


def load_cli(name: str, path: Path):
    """按文件路径加载 scripts/ 下的 CLI 模块。"""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeTransport:
    """假 HTTP 传输：按 (method, url 片段) 返回预置 (status, json 文本)，记录请求。"""

    def __init__(self, routes=None, default=None):
        self.routes = list(routes or [])
        self.default = default or (200, json.dumps({"code": 0, "data": {}}))
        self.calls: list[dict] = []

    def __call__(self, method, url, headers, body, timeout):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "headers": dict(headers),
                "body": json.loads(body.decode("utf-8")) if body else None,
            }
        )
        for matcher, response in self.routes:
            if matcher in url:
                return response(method, url, headers, body) if callable(response) else response
        return self.default


class FakeEmbedder:
    """确定性假嵌入器（零 API）：与 test_rag_ingest 同形。"""

    def embed(self, texts):
        return [[0.1] * 8 for _ in texts]

    def embed_query(self, text):
        return [0.1] * 8


def _registry(tmp_path: Path, **overrides) -> Path:
    data = {
        "provider": "feishu",
        "auth_type": "oauth",
        "status": "usable",
        "access_token": "old-access",
        "refresh_token": "old-refresh",
        "scopes": ["offline_access", "wiki:wiki"],
    }
    data.update(overrides)
    path = tmp_path / "feishu-oauth.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return path


def _env_file(tmp_path: Path) -> Path:
    path = tmp_path / "hermes.env"
    path.write_text(
        f"FEISHU_APP_ID={APP_ID}\nFEISHU_APP_SECRET={APP_SECRET}\nFEISHU_DOMAIN=feishu\n",
        encoding="utf-8",
    )
    return path


def _config(tmp_path: Path) -> dict:
    """零密钥 config：只写环境变量名与路径，不出现任何密钥。"""
    return {
        "kb": {
            "feishu": {
                "app_id_env": "FEISHU_APP_ID",
                "app_secret_env": "FEISHU_APP_SECRET",
                "env_files": [str(tmp_path / "hermes.env")],
                "token_registry": str(tmp_path / "feishu-oauth.json"),
            }
        },
        "domain_kb": {
            "chroma_path": str(tmp_path / "chroma"),
            "state_path": str(tmp_path / "state"),
            "strategy_version": "v2",
            "embedding": {
                "provider": "siliconflow",
                "model": "Qwen/Qwen3-Embedding-8B",
                "api_key_env": "FEISHU_TEST_KEY",
                "dimensions": 8,
                "batch_size": 8,
            },
        },
    }


def _space_item(space_id="spc_1", name="我的知识库"):
    return {"space_id": space_id, "name": name, "space_type": "team", "visibility": "private"}


def _node(node_token="nod_1", obj_token="doxcn1", obj_type="docx", title="架构笔记", has_child=False):
    return {
        "node_token": node_token,
        "obj_token": obj_token,
        "obj_type": obj_type,
        "title": title,
        "has_child": has_child,
        "node_type": "origin",
    }


def _data(payload: dict, code: int = 0) -> tuple[int, str]:
    return 200, json.dumps({"code": code, "data": payload}, ensure_ascii=False)


# ── 1-2. 授权 URL 与凭据 ─────────────────────────────────────────


def test_build_auth_url_encodes_redirect_and_scope():
    url = build_auth_url("cli_x", "http://127.0.0.1:8765/feishu/oauth/callback", "offline_access wiki:wiki drive:drive docx:document", "st1")

    assert url.startswith("https://open.feishu.cn/open-apis/authen/v1/authorize?")
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    assert query["app_id"] == ["cli_x"]
    assert query["redirect_uri"] == ["http://127.0.0.1:8765/feishu/oauth/callback"]
    assert query["state"] == ["st1"]
    scope = query["scope"][0]
    for needed in ("offline_access", "wiki:wiki", "drive:drive", "docx:document"):
        assert needed in scope
    # 原始串里必须已 urlencode（未编码的冒号/斜杠会带回原形）
    assert "redirect_uri=http%3A%2F%2F127.0.0.1" in url
    assert "scope=offline_access+wiki%3Awiki" in url


def test_credentials_read_from_env_file_not_config(tmp_path):
    env_path = _env_file(tmp_path)
    cfg = _config(tmp_path)
    sync = FeishuSync(cfg, transport=FakeTransport())

    assert sync.credentials() == (APP_ID, APP_SECRET)
    # config 只声明变量名，任何按键都不含密钥值
    assert APP_SECRET not in json.dumps(cfg, ensure_ascii=False)
    assert sync.env_files == [env_path]


def test_missing_credentials_raises(tmp_path):
    sync = FeishuSync({"kb": {"feishu": {"env_files": [str(tmp_path / "none.env")]}}}, transport=FakeTransport())

    with pytest.raises(FeishuSyncError) as excinfo:
        sync.credentials()
    assert "FEISHU_APP_ID" in str(excinfo.value)


# ── 3-5. token registry / 刷新 / 过期重试 ─────────────────────────


def test_refresh_rotates_refresh_token(tmp_path):
    registry = _registry(tmp_path)
    cfg = _config(tmp_path)
    transport = FakeTransport(
        routes=[
            (
                feishu_sync.REFRESH_URL,
                (200, json.dumps({"access_token": "new-access", "refresh_token": "new-refresh", "expires_in": 7200})),
            )
        ]
    )
    sync = FeishuSync(cfg, transport=transport, env_files=[_env_file(tmp_path)])
    sync.refresh_user_token()

    written = json.loads(registry.read_text(encoding="utf-8"))
    assert written["access_token"] == "new-access"
    assert written["refresh_token"] == "new-refresh"
    assert written["status"] == "usable"

    call = transport.calls[0]
    assert call["url"] == feishu_sync.REFRESH_URL
    assert call["body"]["grant_type"] == "refresh_token"
    assert call["body"]["client_id"] == APP_ID
    assert call["body"]["client_secret"] == APP_SECRET
    assert call["body"]["refresh_token"] == "old-refresh"


def test_refresh_failure_keeps_registry_and_flags_reauth(tmp_path):
    registry = _registry(tmp_path)
    before = registry.read_text(encoding="utf-8")
    transport = FakeTransport(
        routes=[
            (
                feishu_sync.REFRESH_URL,
                (400, json.dumps({"error": "invalid_grant", "error_description": "The refresh token has expired.", "code": 20037})),
            )
        ]
    )
    sync = FeishuSync(_config(tmp_path), transport=transport, env_files=[_env_file(tmp_path)])

    with pytest.raises(FeishuAuthError) as excinfo:
        sync.refresh_user_token()

    assert excinfo.value.needs_reauthorization is True
    assert excinfo.value.code == 20037
    # 只在同一个 refresh_token 上试一次，且不覆盖旧 registry
    assert len(transport.calls) == 1
    assert registry.read_text(encoding="utf-8") == before


def test_expired_access_token_refreshes_then_retries_once(tmp_path):
    registry = _registry(tmp_path)
    attempts = {"spaces": 0}

    def spaces(matcher, method=None, url=None, headers=None, body=None):
        return None

    routes = []

    def spaces_response(method, url, headers, body):
        attempts["spaces"] += 1
        if attempts["spaces"] == 1:
            assert headers["Authorization"] == "Bearer old-access"
            return 401, json.dumps({"code": 99991677, "msg": "Authentication token expired."})
        assert headers["Authorization"] == "Bearer new-access"
        return _data({"items": [_space_item()], "has_more": False})

    routes.append(("/wiki/v2/spaces", spaces_response))
    routes.append(
        (
            feishu_sync.REFRESH_URL,
            (200, json.dumps({"access_token": "new-access", "refresh_token": "new-refresh"})),
        )
    )
    sync = FeishuSync(_config(tmp_path), transport=FakeTransport(routes=routes), env_files=[_env_file(tmp_path)])

    spaces_list = sync.list_spaces()

    assert [s["space_id"] for s in spaces_list] == ["spc_1"]
    assert attempts["spaces"] == 2
    assert json.loads(registry.read_text(encoding="utf-8"))["refresh_token"] == "new-refresh"


# ── 6-7. 空间 / 节点 / 正文 ──────────────────────────────────────


def test_list_spaces_paginates(tmp_path):
    _registry(tmp_path)
    calls = {"n": 0}

    def spaces_response(method, url, headers, body):
        calls["n"] += 1
        if calls["n"] == 1:
            return _data({"items": [_space_item("spc_1", "A")], "has_more": True, "page_token": "p2"})
        assert "page_token=p2" in url
        return _data({"items": [_space_item("spc_2", "B")], "has_more": False})

    sync = FeishuSync(
        _config(tmp_path), transport=FakeTransport(routes=[("/wiki/v2/spaces", spaces_response)])
    )
    assert [s["space_id"] for s in sync.list_spaces()] == ["spc_1", "spc_2"]


def test_iter_space_nodes_walks_children(tmp_path):
    _registry(tmp_path)

    def nodes_response(method, url, headers, body):
        if "parent_node_token=nod_1" in url:
            return _data({"items": [_node("nod_2", "doxcn2", "docx", "子文档")], "has_more": False})
        return _data(
            {
                "items": [
                    _node("nod_1", "doxcn1", "docx", "父文档", has_child=True),
                    _node("nod_x", "shtcn1", "sheet", "表格"),
                ],
                "has_more": False,
            }
        )

    sync = FeishuSync(
        _config(tmp_path), transport=FakeTransport(routes=[("/wiki/v2/spaces/spc_1/nodes", nodes_response)])
    )
    tokens = [node["node_token"] for node in sync.iter_space_nodes("spc_1")]
    assert tokens == ["nod_1", "nod_x", "nod_2"]


def test_fetch_document_text_docx_and_unsupported_type(tmp_path):
    _registry(tmp_path)

    def raw_content(method, url, headers, body):
        assert url.endswith("/docx/v1/documents/doxcn1/raw_content")
        return _data({"content": "第一段\n第二段"})

    sync = FeishuSync(_config(tmp_path), transport=FakeTransport(routes=[("/docx/v1/documents/", raw_content)]))

    text = sync.fetch_document_text(_node())
    assert text.startswith("# 架构笔记")
    assert "第一段" in text
    assert sync.document_url(_node()) == "https://feishu.cn/wiki/nod_1"

    with pytest.raises(FeishuSyncError):
        sync.fetch_document_text(_node(obj_type="sheet"))


# ── 8-9. 入库适配 + 幂等 + 回读 ──────────────────────────────────


def _ingester(tmp_path: Path) -> RAGIngest:
    return RAGIngest(_config(tmp_path), embedder=FakeEmbedder())


def test_ingest_document_writes_increment_metadata(tmp_path):
    ingester = _ingester(tmp_path)
    source = "https://feishu.cn/wiki/nod_1"

    result = ingest_document(
        ingester,
        text="# 架构笔记\n\n## 一、目标\n\n把飞书文档拉进增量库。\n\n## 二、口径\n\nsource 用飞书文档 URL。",
        doc_type="note",
        source=source,
        title="架构笔记",
        domain="increment",
        feishu_ref="nod_1",
    )

    assert result["chunks"] >= 2
    check = verify_source(ingester, source)
    assert check["chunks"] == result["chunks"]
    assert "把飞书文档拉进增量库" in check["sample"] or "增量库" in check["sample"]

    stored = ingester.collection.get(where={"source": source})
    meta = stored["metadatas"][0]
    assert meta["domain"] == "increment"
    # layer 由 ingest.py v2 决定取 concept（增量身份靠 domain + doc_type 表达，
    # layer 仍走 concept 以便现有 RAGStore 分层加权命中）
    assert meta["layer"] == "concept"
    assert meta["doc_type"] == "note"
    assert meta["source"] == source
    assert meta["feishu_ref"] == "nod_1"
    assert meta["chunking_version"]


def test_ingest_document_is_idempotent_per_source(tmp_path):
    ingester = _ingester(tmp_path)
    source = "https://feishu.cn/wiki/nod_9"
    text = "# T\n\n## A\n\n内容甲。" * 3

    first = ingest_document(ingester, text=text, doc_type="note", source=source, title="T")
    second = ingest_document(ingester, text=text, doc_type="note", source=source, title="T")

    # 第二次 content_hash 与 strategy_version 均未变 → 整条流水线跳过、库不翻倍
    assert first["chunks"] >= 1
    assert second["chunks"] == 0
    assert second["skipped"] is True
    assert verify_source(ingester, source)["chunks"] == first["chunks"]


def test_sync_space_pulls_and_ingests_with_readback(tmp_path):
    _registry(tmp_path)

    def nodes_response(method, url, headers, body):
        return _data({"items": [_node("nod_1", "doxcn1", "docx", "架构笔记"), _node("nod_x", "shtcn1", "sheet", "表格")], "has_more": False})

    def raw_content(method, url, headers, body):
        return _data({"content": "## 一、目标\n\n把飞书文档拉进增量库。"})

    sync = FeishuSync(
        _config(tmp_path),
        transport=FakeTransport(routes=[("/wiki/v2/spaces/spc_1/nodes", nodes_response), ("/docx/v1/documents/", raw_content)]),
        env_files=[_env_file(tmp_path)],
    )
    ingester = _ingester(tmp_path)

    summary = sync.sync_space("spc_1", ingest=ingester)

    assert summary["scanned"] == 2
    assert [entry["title"] for entry in summary["ingested"]] == ["架构笔记"]
    assert summary["chunks"] >= 1
    assert summary["skipped"][0]["obj_type"] == "sheet"
    assert "暂不支持" in summary["skipped"][0]["reason"]

    check = verify_source(ingester, "https://feishu.cn/wiki/nod_1")
    assert check["chunks"] == summary["chunks"]
    assert "增量库" in check["sample"]


def test_sync_space_dry_run_does_not_touch_chroma(tmp_path):
    _registry(tmp_path)

    def nodes_response(method, url, headers, body):
        return _data({"items": [_node()], "has_more": False})

    def raw_content(method, url, headers, body):
        return _data({"content": "正文"})

    sync = FeishuSync(
        _config(tmp_path),
        transport=FakeTransport(routes=[("/wiki/v2/spaces/spc_1/nodes", nodes_response), ("/docx/v1/documents/", raw_content)]),
    )
    summary = sync.sync_space("spc_1", dry_run=True)

    assert summary["dry_run"] is True
    assert summary["ingested"][0]["chunks"] == 0
    assert not (tmp_path / "chroma").exists()


# ── 10. CLI ──────────────────────────────────────────────────────


def test_cli_from_feishu_list_spaces(tmp_path, monkeypatch, capsys):
    cli = load_cli("rag_ingest_feishu_test", SCRIPTS_DIR / "rag_ingest.py")

    class FakeSync:
        def list_spaces(self):
            return [_space_item("spc_1", "AI 知识库")]

    monkeypatch.setattr(cli, "build_sync", lambda config, registry=None: FakeSync())

    code = cli.main(["--from-feishu", "--list-spaces"])
    out = capsys.readouterr().out

    assert code == 0
    assert "可见知识空间数: 1" in out
    assert "space_id=spc_1" in out
    assert "AI 知识库" in out


def test_cli_from_feishu_ingest_prints_readback(tmp_path, monkeypatch, capsys):
    cli = load_cli("rag_ingest_feishu_ingest_test", SCRIPTS_DIR / "rag_ingest.py")

    class FakeSync:
        def sync_space(self, space_id, node="", ingest=None, limit=0, recursive=True, dry_run=False):
            assert space_id == "spc_1"
            assert recursive is True
            ingest_document(
                ingest,
                text="# 架构笔记\n\n## 一、目标\n\n把飞书文档拉进增量库。",
                doc_type="note",
                source="https://feishu.cn/wiki/nod_1",
                title="架构笔记",
                domain="increment",
            )
            return {
                "space_id": space_id,
                "node": node,
                "scanned": 1,
                "ingested": [
                    {
                        "title": "架构笔记",
                        "obj_type": "docx",
                        "chars": 30,
                        "chunks": 1,
                        "url": "https://feishu.cn/wiki/nod_1",
                        "sample": "# 架构笔记",
                    }
                ],
                "skipped": [],
                "chunks": 1,
                "dry_run": dry_run,
            }

    monkeypatch.setattr(cli, "build_sync", lambda config, registry=None: FakeSync())
    monkeypatch.setattr(cli, "build_ingester", lambda config: _ingester(tmp_path))

    code = cli.main(["--from-feishu", "--space", "spc_1"])
    out = capsys.readouterr().out

    assert code == 0
    assert "飞书空间: spc_1" in out
    assert "入库文档: 1 篇，写入 chunk 总数: 1" in out
    assert "回读校验: 架构笔记 → 命中 chunk 1 条" in out


def test_cli_feishu_requires_space():
    cli = load_cli("rag_ingest_feishu_missing_space", SCRIPTS_DIR / "rag_ingest.py")
    with pytest.raises(SystemExit) as excinfo:
        cli.main(["--from-feishu"])
    assert excinfo.value.code == 2


def test_cli_requires_layer_or_feishu():
    cli = load_cli("rag_ingest_no_target", SCRIPTS_DIR / "rag_ingest.py")
    with pytest.raises(SystemExit) as excinfo:
        cli.main([])
    assert excinfo.value.code == 2


def test_cli_feishu_auth_failure_returns_3(monkeypatch, capsys):
    cli = load_cli("rag_ingest_feishu_auth_fail", SCRIPTS_DIR / "rag_ingest.py")

    class DeadSync:
        def list_spaces(self):
            raise FeishuAuthError("飞书 API 失败：code=99991677（user token 过期）", code=99991677)

    monkeypatch.setattr(cli, "build_sync", lambda config, registry=None: DeadSync())

    code = cli.main(["--from-feishu", "--list-spaces"])
    err = capsys.readouterr().err

    assert code == 3
    assert "需要用户重新授权" in err
    assert "feishu_auth.py" in err


def test_auth_helper_url_only(tmp_path, monkeypatch, capsys):
    auth = load_cli("feishu_auth_cli_test", SCRIPTS_DIR / "feishu_auth.py")
    env_path = _env_file(tmp_path)
    cfg = _config(tmp_path)
    sync = FeishuSync(cfg, token_registry=str(tmp_path / "feishu-oauth.json"), env_files=[env_path])
    monkeypatch.setattr(auth, "load_config", lambda *a, **k: cfg)
    monkeypatch.setattr(auth, "build_sync", lambda config, registry=None: sync)
    monkeypatch.setattr(auth, "LINK_FILE", tmp_path / "飞书授权链接.txt")

    code = auth.main(["--url-only"])
    out = capsys.readouterr().out

    assert code == 0
    url = out.strip().splitlines()[0]
    assert url.startswith("https://open.feishu.cn/open-apis/authen/v1/authorize?")
    assert (tmp_path / "飞书授权链接.txt").read_text(encoding="utf-8").strip() == url
