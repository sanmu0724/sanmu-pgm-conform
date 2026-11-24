"""Token-authenticated JSON RPC for the local PGM production catalogue.

Run one service on the library host and connect other edit-room computers by URL.
SQLite remains on that host. The shared NAS contains media/export files only.
"""
from __future__ import annotations

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request

from library_store import LibraryError, Store

MAX_REQUEST_BYTES = 16 * 1024 * 1024
MAX_RESPONSE_BYTES = 64 * 1024 * 1024
API_REVISION = 7


def _encode(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class LibraryServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, store, token):
        if not isinstance(token, str) or len(token) < 24:
            raise LibraryError("invalid", "服务令牌必须至少包含 24 个字符")
        self.store = store
        self.token = token
        super().__init__(address, Handler)


class Handler(BaseHTTPRequestHandler):
    server_version = "PGMLibrary/1"

    def log_message(self, *_):
        # Do not log bearer tokens, media paths, dialogue, or large RPC bodies.
        pass

    def _send(self, status, value):
        payload = _encode(value)
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        if self.path == "/health":
            self._send(200, {"status": "ok", "version": 1, "api_revision": API_REVISION})
        else:
            self._send(404, {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}})

    def do_POST(self):
        if self.path != "/rpc":
            self._send(404, {"ok": False, "error": {"code": "not_found", "message": "接口不存在"}})
            return
        authorization = self.headers.get("Authorization", "")
        expected = "Bearer " + self.server.token
        if not hmac.compare_digest(authorization.encode("utf-8"), expected.encode("utf-8")):
            self.close_connection = True
            self._send(401, {"ok": False, "error": {"code": "unauthorized", "message": "连接令牌无效"}})
            return
        if self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            self._send(400, {"ok": False, "error": {"code": "invalid", "message": "不支持分块请求"}})
            return
        try:
            length = int(self.headers.get("Content-Length", "-1"))
            if length < 1 or length > MAX_REQUEST_BYTES:
                raise LibraryError("invalid", "请求大小无效或超过 16 MB，请分批导入")
            self.connection.settimeout(30)
            payload = self.rfile.read(length)
            if len(payload) != length:
                raise LibraryError("invalid", "请求内容不完整")
            try:
                request = json.loads(payload)
            except (UnicodeError, json.JSONDecodeError) as exc:
                raise LibraryError("invalid", "请求不是有效 JSON") from exc
            if not isinstance(request, dict):
                raise LibraryError("invalid", "请求必须是对象")
            result = self.server.store.call(request.get("method"), request.get("params", {}), request.get("actor", ""))
            self._send(200, {"ok": True, "result": result})
        except LibraryError as exc:
            status = {"conflict": 409, "not_found": 404, "unauthorized": 401, "storage": 503}.get(exc.code, 400)
            self._send(status, {"ok": False, "error": {"code": exc.code, "message": str(exc)}})
        except (ValueError, OverflowError):
            self._send(400, {"ok": False, "error": {"code": "invalid", "message": "请求参数无效"}})
        except Exception:
            # A client must not receive a host traceback, local paths, or SQL text.
            self._send(500, {"ok": False, "error": {"code": "internal", "message": "服务处理失败，请查看本机服务状态"}})


class Client:
    def __init__(self, url, token, actor="local", timeout=20):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
            raise LibraryError("invalid", "服务地址必须是 http:// 或 https:// 地址")
        if parsed.query or parsed.fragment or parsed.path not in ("", "/"):
            raise LibraryError("invalid", "服务地址不应包含路径、参数或锚点")
        if not isinstance(token, str) or not token:
            raise LibraryError("invalid", "请输入连接令牌")
        if not isinstance(actor, str) or not actor.strip():
            raise LibraryError("invalid", "请输入操作者名称")
        if not isinstance(timeout, (int, float)) or timeout <= 0 or timeout > 120:
            raise LibraryError("invalid", "连接超时必须在 0 到 120 秒之间")
        self.url = url.rstrip("/")
        if any(ord(char) < 33 or ord(char) > 126 for char in token):
            raise LibraryError("invalid", "连接令牌不能包含空白或控制字符")
        self.token = token
        self.actor = actor.strip()
        self.timeout = timeout
        # A LAN catalogue must not be silently routed through a system HTTP proxy.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method, **params):
        if not isinstance(method, str) or method not in Store.METHODS:
            raise LibraryError("invalid", "不支持的操作")
        try:
            body = _encode({"method": method, "params": params, "actor": self.actor})
        except (TypeError, ValueError) as exc:
            raise LibraryError("invalid", "参数必须是有效 JSON") from exc
        if len(body) > MAX_REQUEST_BYTES:
            raise LibraryError("invalid", "请求超过 16 MB，请分批导入")
        request = urllib.request.Request(self.url + "/rpc", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.token})
        try:
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as exc:
                response = exc
            with response:
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise LibraryError("network", "服务响应过大，请分批读取")
            value = json.loads(payload)
            if not isinstance(value, dict) or "ok" not in value:
                raise LibraryError("network", "服务返回了无效响应")
            if not value["ok"]:
                error = value.get("error", {})
                raise LibraryError(str(error.get("code", "network")), str(error.get("message", "服务请求失败")))
            return value.get("result")
        except LibraryError:
            raise
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise LibraryError("network", "无法连接素材库服务，请检查服务、地址和局域网") from exc
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise LibraryError("network", "素材库服务返回内容无法解析") from exc


def read_or_create_token(path):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise LibraryError("invalid", "令牌文件不能是符号链接")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 24:
            raise LibraryError("invalid", "现有令牌过短，请重新创建令牌文件")
        path.chmod(0o600)
        return token
    token = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="utf-8") as out:
        out.write(token + "\n")
    return token


def create_server(db, host="127.0.0.1", port=0, token=None):
    store = Store(db)
    try:
        return LibraryServer((host, port), store, token or secrets.token_urlsafe(32))
    except Exception:
        store.close()
        raise


def _ready(path, value):
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + "." + str(os.getpid()) + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="PGM 共享素材库服务（数据库保存在本机）")
    parser.add_argument("--db", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--ready-file")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    server = None
    try:
        token = read_or_create_token(args.token_file)
        server = create_server(args.db, args.host, args.port, token)
        host, port = server.server_address[:2]
        accessible_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
        ready = {"url": f"http://{accessible_host}:{port}", "host": host, "port": port,
                 "pid": os.getpid(), "version": 1, "api_revision": API_REVISION}
        if args.ready_file:
            _ready(args.ready_file, ready)
        print(json.dumps(ready, ensure_ascii=False), flush=True)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    except (LibraryError, OSError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    finally:
        if server:
            server.server_close()
            server.store.close()
        if args.ready_file:
            Path(args.ready_file).expanduser().unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
