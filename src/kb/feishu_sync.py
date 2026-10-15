# 知识库接入 - 飞书知识库拉取 → 增量域入库
"""FeishuSync：把飞书知识库（wiki）里的文档拉进领域知识库的增量域。

为什么必须用用户身份：`GET /wiki/v2/spaces` 在 tenant_access_token 下返回空列表，
不是应用权限坏了，是身份不对（99991663 系端点只认用户身份）。因此本模块用 OAuth
用户 token：token 落在受控 registry 文件（仓库外、被 .gitignore 覆盖），access token
过期（99991677）时用 refresh token 换新——刷新走 v3 端点
（`POST https://accounts.feishu.cn/oauth/v3/token`，字段 client_id/client_secret），
refresh_token 一次性，刷新成功立即轮换回写 registry；刷新失败（20014/20026/20037）
只报一次错、不在同一个 refresh_token 上反复重试。

网络：飞书是国内 API，本模块所有请求显式走空代理（`ProxyHandler({})`），
否则会被系统代理拦成 404。

链路（对应《RAG知识库实施规划》§4.1 / §5.3 / §7 的飞书入口）：

    list_spaces()            列出可见空间（GET /wiki/v2/spaces）
      → iter_space_nodes()   逐层列节点（GET /wiki/v2/spaces/{space_id}/nodes）
      → fetch_document_text() 拉 docx 正文（GET /docx/v1/documents/{id}/raw_content）
      → ingest_document()    入库（RAGIngest 调用点适配，domain=increment）

密钥纪律：app_id / app_secret 只从环境变量读（config.yaml 里只写环境变量名），
本模块不打印任何 token，也不把 token 写进日志。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── 飞书端点（2026-09-04 定稿链路，与 D:\黑洞工作流\...\auth 下实现一致）──
API_BASE = "https://open.feishu.cn/open-apis"
AUTHORIZE_URL = f"{API_BASE}/authen/v1/authorize"
TOKEN_URL = f"{API_BASE}/authen/v2/oauth/token"
REFRESH_URL = "https://accounts.feishu.cn/oauth/v3/token"
DEFAULT_REDIRECT_URI = "http://127.0.0.1:8765/feishu/oauth/callback"
DEFAULT_SCOPE = "offline_access wiki:wiki drive:drive docx:document"

# 增量域固定值（§2.1 双域：increment / stock）
DOMAIN_INCREMENT = "increment"
# 增量域文档类型：飞书文档按 §6 格式契约当作 note
DEFAULT_DOC_TYPE = "note"
# 分页每页条数（飞书上限 50）
PAGE_SIZE = 50
# 单次请求超时（秒）
TIMEOUT = 45
# 飞书文档域名（拼给用户看 / 作为 source 的 URL）
DOC_URL_PREFIX = "https://feishu.cn/wiki/"

# 错误码 → 排查提示（任务书速查表）
CODE_HINTS = {
    99991663: "该端点只认用户身份（tenant token 不行）",
    99991672: "应用未开通该 scope（按飞书返回 msg 里的申请链接去开通）",
    99991679: "用户授权缺 scope（需按 scope 重新授权）",
    99991677: "user token 过期（需刷新，刷新失败则重新授权）",
    20014: "refresh_token 失效（需用户重新授权）",
    20026: "refresh_token 失效（需用户重新授权）",
    20037: "refresh_token 已过期（需用户重新授权）",
}
# 这些错误码表示「当前用户授权不能再用了」，必须由用户重新走 OAuth
REAUTHORIZE_CODES = {99991663, 99991677, 99991679, 20014, 20026, 20037}
# access token 过期的错误码（可先尝试 refresh 再重试一次）
TOKEN_EXPIRED_CODES = {99991677}

# 只支持拉正文的飞书文档类型（其余类型无正文接口或格式不同，跳过并记录原因）
TEXT_OBJ_TYPES = {"docx"}

# 增量文档入库的元数据默认值
DEFAULT_QUALITY = "user_input"
# 增量文档来源分类（写进 metadata.source_category，便于按来源筛）
SOURCE_CATEGORY_FEISHU = "feishu"

# 默认额外环境变量文件：Hermes 自己的 .env（企业自建应用凭据放这儿）
DEFAULT_ENV_FILES = (Path(os.environ.get("LOCALAPPDATA", "")) / "hermes" / ".env",)
# 默认受控 token registry：落在被 .gitignore 覆盖的 domain_kb/ 下
DEFAULT_TOKEN_REGISTRY = Path("./domain_kb/state/feishu_oauth.json")


class FeishuSyncError(RuntimeError):
    """飞书调用失败（网络、协议、业务错误码）。"""

    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message)
        self.code = code
        self.payload = payload or {}


class FeishuAuthError(FeishuSyncError):
    """身份问题：需要刷新或需要用户重新授权。"""

    def __init__(self, message: str, code: int | None = None, payload: dict | None = None):
        super().__init__(message, code=code, payload=payload)
        self.needs_reauthorization = code in REAUTHORIZE_CODES


# ── 小工具 ────────────────────────────────────────────────────────


def read_env_file(path: str | Path) -> dict[str, str]:
    """读 KEY=VALUE 形式的 .env 文件；文件不存在时返回空 dict（不抛异常）。"""
    values: dict[str, str] = {}
    env_path = Path(path)
    try:
        if not env_path.is_file():
            return values
        text = env_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return values
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'\"")
    return values


def _hint(code: int | None) -> str:
    """错误码 → 人话提示（未知码给空串）。"""
    return CODE_HINTS.get(code, "") if code is not None else ""


def _edit_date(node: dict) -> str:
    """节点最后编辑时间（飞书给的是秒级时间戳字符串）→ YYYY-MM-DD；缺则空串。"""
    raw = node.get("obj_edit_time") or node.get("obj_create_time")
    try:
        stamp = int(str(raw))
    except (TypeError, ValueError):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(stamp))


def _default_transport(method: str, url: str, headers: dict, body: bytes | None, timeout: int):
    """默认 HTTP 传输：显式空代理（飞书是国内 API，走系统代理会被拦成 404）。

    Returns:
        (status_code, body_text)
    """
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FeishuSyncError(
            f"飞书请求网络失败：{method} {url}（{type(exc).__name__}: {exc}）"
        ) from exc


def _parse_json(status: int, text: str, url: str) -> dict:
    """解析飞书响应体；非 JSON 时抛错并带回原始片段。"""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise FeishuSyncError(
            f"飞书响应不是 JSON：{url}（HTTP {status}，片段 {text[:200]!r}）"
        ) from exc
    if not isinstance(payload, dict):
        raise FeishuSyncError(f"飞书响应顶层不是对象：{url}（HTTP {status}）")
    return payload


def build_auth_url(
    app_id: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    scope: str = DEFAULT_SCOPE,
    state: str = "",
) -> str:
    """拼用户授权 URL（redirect_uri 与 scope 都经过 urlencode）。

    走用户身份必须这条链路：授权 URL 为 /authen/v1/authorize，
    scope 必须含 offline_access wiki:wiki drive:drive docx:document。
    """
    params = {"app_id": app_id, "redirect_uri": redirect_uri, "scope": scope}
    if state:
        params["state"] = state
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode(params)


def ingest_document(ingester, text: str, doc_type: str, source: str, **meta) -> dict:
    """把单篇文档交给 RAGIngest.ingest_document 入库（调用点适配层）。

    参数形态与规划 §5.3 一致（text / doc_type / source + 元数据关键字）；
    实现细节（切片策略、确定性 chunk ID、先删后插、孤儿清理、hash 跳过、状态表）
    全在 `kb.rag.ingest.RAGIngest.ingest_document` 里，本函数只做调用点适配与
    返回值归一化，不重复实现入库逻辑。

    Args:
        ingester: RAGIngest 实例（或测试注入的同形对象）。
        text: 文档正文（飞书 docx 转出的纯文本 / Markdown）。
        doc_type: 文档类型（增量域契约：note / doc / snippet …）。
        source: 全局唯一来源键（飞书文档 URL），幂等锚点。
        **meta: 其余元数据（title / domain / quality / feishu_ref / node_token …）。

    Returns:
        ingest_document 的统计 dict + "chars"/"skipped" 归一化字段。
    """
    direct = getattr(ingester, "ingest_document", None)
    if not callable(direct):
        raise FeishuSyncError(
            "入库器没有 ingest_document 接口，无法入库飞书文档："
            f"{type(ingester).__name__}（请用 kb.rag.ingest.RAGIngest）"
        )
    meta = {key: value for key, value in meta.items() if value is not None}
    stats = dict(direct(text=str(text), doc_type=str(doc_type), source=str(source), **meta) or {})
    stats["chars"] = len(str(text))
    stats.setdefault("skipped", False)
    stats["mode"] = "ingest_document"
    return stats


def verify_source(ingester, source: str, sample_chars: int = 80) -> dict:
    """按 metadata.source 回读已入库 chunk（真读 Chroma，验证刚入的内容在库里）。"""
    existing = ingester.collection.get(where={"source": str(source)})
    ids = list(existing.get("ids") or [])
    documents = list(existing.get("documents") or [])
    metadatas = list(existing.get("metadatas") or [])
    sample = ""
    for index, document in enumerate(documents):
        meta = metadatas[index] if index < len(metadatas) else {}
        if meta.get("chunk_index") == 0:
            sample = str(document)[:sample_chars]
            break
    if not sample and documents:
        sample = str(documents[0])[:sample_chars]
    return {"source": str(source), "chunks": len(ids), "ids": ids, "sample": sample}


class FeishuSync:
    """飞书 wiki 拉取 + 增量入库客户端。

    Args:
        config: config.yaml 根 dict（取 config["kb"]["feishu"]）；可省略。
        app_id / app_secret: 显式凭据（测试注入用）；给了就不查环境变量。
        domain: 飞书域名标识（feishu / lark），仅用于记录。
        token_registry: 受控 token registry 路径（存 access/refresh token，仓库外）。
        redirect_uri / scope: OAuth 参数（登记值与请求值必须完全一致）。
        env_files: 额外环境变量文件列表（放 app 凭据的 Hermes .env）。
        transport: HTTP 传输函数，签名同 _default_transport（测试注入假实现）。
        timeout: 单次请求超时秒数。
    """

    def __init__(
        self,
        config: dict | None = None,
        app_id: str = "",
        app_secret: str = "",
        domain: str = "",
        token_registry: str | Path | None = None,
        redirect_uri: str | None = None,
        scope: str | None = None,
        env_files=None,
        transport=None,
        timeout: int = TIMEOUT,
    ):
        kb_cfg = dict((config or {}).get("kb") or {})
        fcfg = dict(kb_cfg.get("feishu") or {})
        # 保留整份 config：自建 RAGIngest 时要用 config["domain_kb"]
        self.config = dict(config or {})
        # 兼容旧键（feishu_app_id/feishu_app_secret）：非空才当凭据，空串忽略
        self.cfg = {
            "store_path": kb_cfg.get("store_path"),
            "app_id_env": fcfg.get("app_id_env") or "FEISHU_APP_ID",
            "app_secret_env": fcfg.get("app_secret_env") or "FEISHU_APP_SECRET",
            "domain_env": fcfg.get("domain_env") or "FEISHU_DOMAIN",
            "domain_default": fcfg.get("domain_default") or "feishu",
            "token_registry": fcfg.get("token_registry") or "",
            "redirect_uri": fcfg.get("redirect_uri") or DEFAULT_REDIRECT_URI,
            "scope": fcfg.get("scope") or DEFAULT_SCOPE,
            "env_files": fcfg.get("env_files") or [],
            "app_id": kb_cfg.get("feishu_app_id") or "",
            "app_secret": kb_cfg.get("feishu_app_secret") or "",
        }
        self.app_id = app_id or self.cfg["app_id"]
        self.app_secret = app_secret or self.cfg["app_secret"]
        self.domain = domain
        self.redirect_uri = redirect_uri or self.cfg["redirect_uri"]
        self.scope = scope or self.cfg["scope"]
        env_file_list = env_files if env_files is not None else self.cfg["env_files"]
        self.env_files = [Path(str(p)) for p in (env_file_list or list(DEFAULT_ENV_FILES))]
        registry = token_registry or self.cfg["token_registry"] or os.environ.get(
            "FEISHU_TOKEN_REGISTRY", ""
        )
        self.registry_path = Path(str(registry)) if registry else DEFAULT_TOKEN_REGISTRY
        self.transport = transport or _default_transport
        self.timeout = timeout
        self.log: list[str] = []

    # ── 凭据（零密钥：只从环境变量 / 额外 env 文件读）───────────────

    def env_value(self, key: str) -> str:
        """取值顺序：进程环境变量 → 额外 env 文件（Hermes .env）。"""
        value = os.environ.get(key, "")
        if value:
            return value
        for path in self.env_files:
            value = read_env_file(path).get(key, "")
            if value:
                return value
        return ""

    def credentials(self) -> tuple[str, str]:
        """返回 (app_id, app_secret)；缺任一都抛错（不静默降级成 tenant 身份）。"""
        app_id = self.app_id or self.env_value(self.cfg["app_id_env"])
        app_secret = self.app_secret or self.env_value(self.cfg["app_secret_env"])
        if not app_id or not app_secret:
            raise FeishuSyncError(
                f"飞书应用凭据缺失：请设置环境变量 {self.cfg['app_id_env']} / "
                f"{self.cfg['app_secret_env']}（config.yaml 只写变量名，不写密钥）"
            )
        if not self.domain:
            self.domain = self.env_value(self.cfg["domain_env"]) or self.cfg["domain_default"]
        return app_id, app_secret

    def auth_url(self, state: str = "") -> str:
        """生成用户授权链接（裸链接，可直接贴浏览器）。"""
        app_id, _ = self.credentials()
        return build_auth_url(app_id, self.redirect_uri, self.scope, state)

    # ── token registry（受控文件；不打印、不进日志）─────────────────

    def load_registry(self) -> dict:
        """读 token registry；不存在 / 缺 refresh_token 时按「需要重新授权」报错。"""
        try:
            data = json.loads(self.registry_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise FeishuAuthError(
                f"飞书用户 token 未授权：registry 不存在（{self.registry_path}）。"
                f"请先完成用户 OAuth 授权（scripts/feishu_auth.py --serve 生成链接并回调）",
                code=20014,
            ) from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise FeishuAuthError(
                f"飞书 token registry 读取失败：{self.registry_path}（{exc}）", code=20014
            ) from exc
        if not data.get("access_token") or not data.get("refresh_token"):
            raise FeishuAuthError(
                f"飞书 token registry 不完整（缺 access_token / refresh_token）：{self.registry_path}",
                code=20014,
            )
        return data

    def save_registry(self, data: dict) -> None:
        """写回 registry（受控文件；目录自建；不打印内容）。"""
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)
        self.registry_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    def refresh_user_token(self) -> dict:
        """用 refresh_token 换新（v3 端点，只试一次；成功后立即轮换回写）。

        失败（20014 / 20026 / 20037 …）不覆盖旧 registry、不在同一个 refresh_token
        上反复重试，直接抛 FeishuAuthError（needs_reauthorization=True）。
        """
        app_id, app_secret = self.credentials()
        registry = self.load_registry()
        body = json.dumps(
            {
                "grant_type": "refresh_token",
                "client_id": app_id,
                "client_secret": app_secret,
                "refresh_token": registry["refresh_token"],
            }
        ).encode("utf-8")
        status, text = self.transport(
            "POST",
            REFRESH_URL,
            {"Content-Type": "application/json; charset=utf-8"},
            body,
            self.timeout,
        )
        payload = _parse_json(status, text, REFRESH_URL)
        data = payload.get("data") or payload
        if payload.get("error") or not data.get("access_token"):
            code = payload.get("code")
            raise FeishuAuthError(
                f"飞书 refresh_token 刷新失败：code={code} "
                f"error={payload.get('error')} {_hint(code or 20014)}",
                code=code or 20014,
                payload=payload,
            )
        new_registry = {
            **registry,
            "provider": "feishu",
            "auth_type": "oauth",
            "status": "usable",
            "last_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expires_at": data.get("expires_at"),
            "next_action": None,
            "access_token": data.get("access_token"),
            # 一次性：飞书没下发新 refresh_token 时才沿用旧的
            "refresh_token": data.get("refresh_token") or registry["refresh_token"],
        }
        if data.get("scope"):
            new_registry["scopes"] = str(data["scope"]).split()
        self.save_registry(new_registry)
        self.log.append("飞书用户 token 已刷新并轮换回写 registry（内容未输出）")
        return new_registry

    def exchange_code(self, code: str) -> dict:
        """OAuth 回调拿到 code 后换 token（v2 端点，字段 client_id/client_secret）。"""
        app_id, app_secret = self.credentials()
        body = json.dumps(
            {
                "grant_type": "authorization_code",
                "client_id": app_id,
                "client_secret": app_secret,
                "code": code,
                "redirect_uri": self.redirect_uri,
            }
        ).encode("utf-8")
        status, text = self.transport(
            "POST",
            TOKEN_URL,
            {"Content-Type": "application/json; charset=utf-8"},
            body,
            self.timeout,
        )
        payload = _parse_json(status, text, TOKEN_URL)
        data = payload.get("data") or payload
        if payload.get("error") or not data.get("access_token"):
            code_value = payload.get("code")
            raise FeishuAuthError(
                f"飞书授权码换 token 失败：code={code_value} error={payload.get('error')}",
                code=code_value or 99991679,
                payload=payload,
            )
        registry = {
            "provider": "feishu",
            "auth_type": "oauth",
            "status": "usable",
            "last_verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "expires_at": data.get("expires_at"),
            "scopes": str(data.get("scope") or self.scope).split(),
            "next_action": None,
            "access_token": data.get("access_token"),
            "refresh_token": data.get("refresh_token"),
        }
        self.save_registry(registry)
        self.log.append(f"飞书用户授权完成，token 已落受控 registry：{self.registry_path}")
        return registry

    # ── HTTP ──────────────────────────────────────────────────────

    def _request_with_token(self, method: str, url: str, token: str, body: bytes | None) -> tuple[int, str]:
        headers = {
            "Authorization": f"Bearer {token}",
            "User-Agent": "ai-pm-agent-feishu-sync/1.0",
        }
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return self.transport(method, url, headers, body, self.timeout)

    def api(
        self,
        method: str,
        path: str,
        params: dict | None = None,
        body: dict | None = None,
        retry_on_expired: bool = True,
    ) -> dict:
        """调飞书 OpenAPI（用户身份）；access token 过期先刷新一次再重试一次。

        Returns:
            响应里的 data 段（dict）。
        """
        url = API_BASE + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        payload_body = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        registry = self.load_registry()
        status, text = self._request_with_token(method, url, registry["access_token"], payload_body)
        payload = _parse_json(status, text, url)
        code = payload.get("code")

        if code in TOKEN_EXPIRED_CODES and retry_on_expired:
            self.refresh_user_token()
            registry = self.load_registry()
            status, text = self._request_with_token(method, url, registry["access_token"], payload_body)
            payload = _parse_json(status, text, url)
            code = payload.get("code")

        if code not in (0, None):
            raise self._error_for(code, payload, method, path)
        return payload.get("data") or {}

    @staticmethod
    def _error_for(code, payload: dict, method: str, path: str) -> FeishuSyncError:
        """按飞书错误码给带排查提示的异常（身份类错误码归为 FeishuAuthError）。"""
        msg = str(payload.get("msg") or "")
        hint = _hint(code)
        text = f"飞书 API 失败：{method} {path} code={code} msg={msg}"
        if hint:
            text += f"（{hint}）"
        if code in REAUTHORIZE_CODES:
            return FeishuAuthError(text, code=code, payload=payload)
        return FeishuSyncError(text, code=code, payload=payload)

    # ── wiki / docx 读取 ──────────────────────────────────────────

    def list_spaces(self, page_size: int = PAGE_SIZE) -> list[dict]:
        """列出可见知识空间（分页取全）。用户身份下才会返回实际可见空间。"""
        spaces: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": page_size}
            if page_token:
                params["page_token"] = page_token
            data = self.api("GET", "/wiki/v2/spaces", params=params)
            items = list(data.get("items") or [])
            spaces.extend(items)
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = data["page_token"]
        return spaces

    def get_node(self, node_token: str) -> dict:
        """按 node_token 取节点信息（--node 入口用）。"""
        data = self.api(
            "GET", "/wiki/v2/spaces/get_node", params={"token": node_token, "obj_type": "wiki"}
        )
        node = data.get("node")
        if not isinstance(node, dict):
            raise FeishuSyncError(f"飞书 wiki 节点不存在或不可见：{node_token}")
        return node

    def list_nodes(
        self, space_id: str, parent_node_token: str = "", page_size: int = PAGE_SIZE
    ) -> list[dict]:
        """列出某空间（或某节点）下的直接子节点（分页取全）。"""
        nodes: list[dict] = []
        page_token = ""
        while True:
            params = {"page_size": page_size}
            if parent_node_token:
                params["parent_node_token"] = parent_node_token
            if page_token:
                params["page_token"] = page_token
            data = self.api("GET", f"/wiki/v2/spaces/{space_id}/nodes", params=params)
            items = list(data.get("items") or [])
            nodes.extend(items)
            if not data.get("has_more") or not data.get("page_token"):
                break
            page_token = data["page_token"]
        return nodes

    def iter_space_nodes(self, space_id: str, node_token: str = "", recursive: bool = True):
        """广度遍历节点：给了 node_token 就从该节点（含自身）往下，否则从空间根开始。"""
        queue = [node_token] if node_token else [""]
        if node_token:
            yield self.get_node(node_token)
        seen: set[str] = set()
        while queue:
            parent = queue.pop(0)
            for node in self.list_nodes(space_id, parent):
                token = str(node.get("node_token") or "")
                if token and token in seen:
                    continue
                if token:
                    seen.add(token)
                yield node
                if recursive and node.get("has_child") and token:
                    queue.append(token)

    def document_url(self, node: dict) -> str:
        """节点 → 飞书文档 URL（作为增量域 source，全局唯一来源键）。"""
        token = str(node.get("node_token") or node.get("obj_token") or "")
        return DOC_URL_PREFIX + token

    def fetch_document_text(self, node: dict) -> str:
        """拉文档正文：docx 走 raw_content（纯文本），标题行补成 Markdown 一级标题。

        非 docx（sheet / bitable / mindnote / file / slides …）没有同形正文接口，
        抛 FeishuSyncError，由调用方记为跳过（不猜、不伪造内容）。
        """
        obj_type = str(node.get("obj_type") or "")
        obj_token = str(node.get("obj_token") or "")
        title = str(node.get("title") or "").strip()
        if obj_type not in TEXT_OBJ_TYPES:
            raise FeishuSyncError(
                f"暂不支持拉正文的节点类型 {obj_type or '未知'}（标题：{title or '无'}）"
            )
        if not obj_token:
            raise FeishuSyncError(f"节点缺 obj_token，无法拉正文（标题：{title or '无'}）")
        data = self.api("GET", f"/docx/v1/documents/{obj_token}/raw_content")
        content = str(data.get("content") or "").strip()
        if not content:
            raise FeishuSyncError(f"文档正文为空（标题：{title or '无'}）")
        # 纯文本正文补一级标题，检索时 title/section_title 有锚点
        return f"# {title}\n\n{content}" if title else content

    # ── 同步入口 ──────────────────────────────────────────────────

    def sync_space(
        self,
        space_id: str,
        node: str = "",
        ingest=None,
        limit: int = 0,
        recursive: bool = True,
        dry_run: bool = False,
    ) -> dict:
        """把一个空间（或一个节点子树）拉进增量库。

        Args:
            space_id: 知识空间 id。
            node: 可选起点节点 node_token；给了只处理该节点及其子树。
            ingest: RAGIngest 实例；None 且需要入库时按 config 自建（真实嵌入）。
            limit: 最多入库几篇（0 = 不限，探针用）。
            dry_run: 只拉不用库（验证拉取链路）。

        Returns:
            {"space_id", "node", "scanned", "ingested": [...], "skipped": [...],
             "chunks", "dry_run"}
        """
        summary = {
            "space_id": str(space_id),
            "node": str(node or ""),
            "scanned": 0,
            "ingested": [],
            "skipped": [],
            "chunks": 0,
            "dry_run": bool(dry_run),
        }
        for item in self.iter_space_nodes(space_id, node, recursive=recursive):
            summary["scanned"] += 1
            title = str(item.get("title") or "").strip()
            obj_type = str(item.get("obj_type") or "")
            entry = {
                "title": title,
                "obj_type": obj_type,
                "node_token": str(item.get("node_token") or ""),
                "obj_token": str(item.get("obj_token") or ""),
                "url": self.document_url(item),
            }
            try:
                text = self.fetch_document_text(item)
            except FeishuSyncError as exc:
                summary["skipped"].append({**entry, "reason": str(exc)})
                continue

            entry["chars"] = len(text)
            if dry_run:
                summary["ingested"].append({**entry, "chunks": 0, "sample": text[:120]})
                continue
            if ingest is None:
                from kb.rag.ingest import RAGIngest

                ingest = RAGIngest(self.config)
            result = ingest_document(
                ingest,
                text=text,
                doc_type=DEFAULT_DOC_TYPE,
                source=entry["url"],
                title=title or entry["node_token"],
                domain=DOMAIN_INCREMENT,
                quality=DEFAULT_QUALITY,
                source_category=SOURCE_CATEGORY_FEISHU,
                feishu_ref=entry["node_token"] or entry["obj_token"],
                feishu_space_id=str(space_id),
                feishu_obj_token=entry["obj_token"],
                updated=_edit_date(item),
            )
            entry["chunks"] = result["chunks"]
            entry["skipped"] = bool(result.get("skipped"))
            entry["sample"] = text[:120]
            summary["ingested"].append(entry)
            summary["chunks"] += result["chunks"]
            if limit and len(summary["ingested"]) >= limit:
                break
        return summary

    def fetch_from_feishu(self, space_id: str = "", node: str = "", **kwargs) -> dict:
        """从飞书知识库拉取（保留占位实现的公开方法名，改为真实现）。

        Args:
            space_id: 知识空间 id；不传则只列空间（返回 {"spaces": [...]}）。
            node: 可选起点节点 node_token。
            **kwargs: 透传 sync_space（ingest / limit / recursive / dry_run）。
        """
        if not space_id:
            return {"spaces": self.list_spaces()}
        return self.sync_space(space_id, node=node, **kwargs)

    def push_to_feishu(self, archive: dict):
        """向飞书推送档案（本期不做，保持未实现）。"""
        raise NotImplementedError(
            "飞书推送本期不做：本期只接「飞书 → 增量库」单向拉取"
        )
