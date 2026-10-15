#!/usr/bin/env python3
# 飞书用户授权（用户身份 OAuth）：生成授权链接 + 本机回调换 token
"""feishu_auth.py — 完成飞书用户身份 OAuth 首次授权 / 重新授权。

为什么需要用户身份：`GET /wiki/v2/spaces` 在 tenant_access_token 下返回空列表——
不是应用权限坏了，是这个端点只认用户身份（99991663）。

链路（固定，别改）：
  · 授权 URL：https://open.feishu.cn/open-apis/authen/v1/authorize
    scope 必须含 offline_access wiki:wiki drive:drive docx:document（否则不下发
    可用 refresh_token），redirect_uri 与 scope 都 urlencode；
  · 回调地址：http://127.0.0.1:8765/feishu/oauth/callback（飞书后台登记值与请求值
    必须完全一致）；
  · 换 token：POST https://open.feishu.cn/open-apis/authen/v2/oauth/token，
    字段 client_id / client_secret（不是 app_id / app_secret）。

token 落受控 registry 文件（路径见 config.yaml → kb.feishu.token_registry），
不打印、不进日志。

用法（项目根下）：
    python scripts/feishu_auth.py --url-only   # 只生成并打印授权链接（裸链接单独一行）
    python scripts/feishu_auth.py              # 生成链接 + 起本机回调服务，等用户授权

退出码：0 授权成功；1 运行时错误；2 参数/配置错误；3 等待授权超时。
"""
from __future__ import annotations

import argparse
import http.server
import secrets
import sys
import threading
import time
import urllib.parse
from pathlib import Path

# 让裸命令（python scripts/feishu_auth.py）在仓库根直接可跑：把 src/ 挂进导入路径
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from kb.feishu_sync import DEFAULT_REDIRECT_URI, FeishuSync
from kernel.config import PROJECT_ROOT, load_config

# 起服务时等待用户完成授权的最长秒数（60 分钟：用户回消息节奏不稳，短窗口必然超时）
WAIT_SECONDS = 3600
# 授权链接落盘位置（output/ 被 .gitignore 覆盖）
LINK_FILE = PROJECT_ROOT / "output" / "飞书授权链接.txt"


def build_sync(config: dict, registry: str | None = None) -> FeishuSync:
    """构造 FeishuSync（独立函数便于测试 monkeypatch）。"""
    return FeishuSync(config, token_registry=registry)


def write_link_file(url: str) -> Path:
    """把授权链接写进 txt（路径在报告里给出），返回路径。"""
    LINK_FILE.parent.mkdir(parents=True, exist_ok=True)
    LINK_FILE.write_text(url + "\n", encoding="utf-8")
    return LINK_FILE


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="feishu_auth.py",
        description="飞书用户身份 OAuth 授权（生成链接 + 本机回调换 token）",
    )
    parser.add_argument("--url-only", action="store_true", help="只生成并打印授权链接，不起回调服务")
    parser.add_argument("--registry", default=None, help="token registry 路径；缺省用 config.yaml 的口径")
    parser.add_argument("--timeout", type=int, default=WAIT_SECONDS, help=f"等待授权秒数（默认 {WAIT_SECONDS}）")
    return parser.parse_args(argv)


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """只处理一次 /feishu/oauth/callback：校验 state → 用 code 换 token。"""

    sync: FeishuSync = None  # type: ignore[assignment]
    state = ""
    done = threading.Event()
    ok = False

    def do_GET(self):  # noqa: N802（http.server 接口）
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != urllib.parse.urlparse(DEFAULT_REDIRECT_URI).path:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        if query.get("state", [""])[0] != self.state:
            self.send_error(400, "invalid state")
            return
        if "error" in query:
            self._respond(400, f"授权被拒：{query.get('error', [''])[0]}")
            return
        code = query.get("code", [""])[0]
        try:
            self.sync.exchange_code(code)
            self._respond(200, "飞书授权成功，可以关闭此页面，回到终端继续。")
        except Exception as exc:  # noqa: BLE001（对外只给一句话，细节回终端）
            print(f"[feishu_auth] 换 token 失败：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
            self._respond(500, "飞书授权交换失败，请回到终端查看原因。")

    def _respond(self, status: int, body: str) -> None:
        raw = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except OSError:
            pass
        self.ok = status == 200
        self.done.set()

    def log_message(self, fmt, *args):  # 关掉访问日志
        return


def serve_callback(sync: FeishuSync, url: str, timeout: int) -> int:
    """启本机回调服务等授权；成功返回 0，超时返回 3。"""
    parsed = urllib.parse.urlparse(DEFAULT_REDIRECT_URI)
    host, port = parsed.hostname or "127.0.0.1", parsed.port or 8765
    state = secrets.token_urlsafe(24)
    url = sync.auth_url(state)

    _CallbackHandler.sync = sync
    _CallbackHandler.state = state
    _CallbackHandler.done = threading.Event()
    try:
        server = http.server.HTTPServer((host, port), _CallbackHandler)
    except OSError as exc:
        print(f"[feishu_auth] 回调端口被占用或不监听成功：{host}:{port}（{exc}）", file=sys.stderr)
        return 1

    print("授权链接（浏览器打开，登录后点同意；链接已落盘）：")
    print(url, flush=True)
    print(f"回调地址：{DEFAULT_REDIRECT_URI}", flush=True)
    print(f"链接文件：{write_link_file(url)}", flush=True)

    server.timeout = 1
    deadline = time.time() + timeout
    while not _CallbackHandler.done.is_set() and time.time() < deadline:
        server.handle_request()
    server.server_close()

    if not _CallbackHandler.done.is_set():
        print("[feishu_auth] 等待授权超时；token 未变更。", file=sys.stderr)
        return 3
    if not _CallbackHandler.ok:
        print("[feishu_auth] 授权未完成（见上面的失败原因）。", file=sys.stderr)
        return 1
    print(f"[feishu_auth] 授权完成，token 已写受控 registry：{sync.registry_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        config = load_config()
        sync = build_sync(config, args.registry)
        if args.url_only:
            url = sync.auth_url(secrets.token_urlsafe(24))
            print(url)
            print(f"链接文件：{write_link_file(url)}")
            return 0
        return serve_callback(sync, "", args.timeout)
    except Exception as exc:  # noqa: BLE001
        print(f"[feishu_auth] 失败：{type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
