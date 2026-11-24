"""Foreground launcher for the edit-room shared catalogue.

No GUI or media processing dependencies are needed. The database is fixed to the
service computer's local application-data directory; NAS paths belong to media.
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys

from library_bridge import data_dir
from library_service import create_server, read_or_create_token
from library_store import LibraryError, _local_db_path


def server_directory():
    folder = data_dir() / "server"
    # Validate the actual filesystem before creating the token or SQLite files.
    _local_db_path(folder / "library.sqlite3")
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    return folder


def lan_addresses():
    """Read local interface addresses without contacting an Internet service."""
    addresses = set()
    if sys.platform == "darwin":
        for interface in ("en0", "en1", "en2", "en3", "en4", "bridge0"):
            try:
                result = subprocess.run(["/usr/sbin/ipconfig", "getifaddr", interface],
                                        capture_output=True, text=True, timeout=2, check=False)
                addresses.add(result.stdout.strip())
            except (OSError, subprocess.SubprocessError):
                continue
    else:
        try:
            addresses.update(socket.gethostbyname_ex(socket.gethostname())[2])
        except OSError:
            pass
    result = []
    for value in sorted(addresses):
        try:
            address = ipaddress.ip_address(value)
        except ValueError:
            continue
        if address.version == 4 and address.is_private and not address.is_loopback and not address.is_link_local:
            result.append(value)
    return result


def connection_urls(host, port):
    if host == "0.0.0.0":
        local = f"http://127.0.0.1:{port}"
        others = [f"http://{ip}:{port}" for ip in lan_addresses()]
        return local, others
    if host in ("127.0.0.1", "localhost"):
        return f"http://127.0.0.1:{port}", []
    return f"http://{host}:{port}", [f"http://{host}:{port}"]


def write_ready(path, value):
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="启动机房共享素材库，媒体留在 NAS，数据库保存在服务电脑本机")
    parser.add_argument("--host", default="0.0.0.0", help="监听地址，默认允许局域网客户端连接")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765；测试可用 0 自动分配")
    parser.add_argument("--ready-file", help="仅供服务状态检查；不包含连接令牌")
    args = parser.parse_args(argv)
    if not 0 <= args.port <= 65535:
        parser.error("端口必须在 0 到 65535 之间")
    if args.host not in ("127.0.0.1", "localhost"):
        print("即将启动机房局域网共享素材库。持连接令牌的人可以读写库内资料。", flush=True)
        print("此服务没有 TLS 加密，仅用于可信的机房局域网；不要映射到公网。", flush=True)
    else:
        print("即将启动本机测试素材库，仅接受本机连接。", flush=True)
    print("NAS 只存媒体和导出文件，运行中的数据库固定保存在这台电脑的本地应用数据目录。", flush=True)
    server, ready_path, old_term = None, None, None
    try:
        folder = server_directory()
        database = folder / "library.sqlite3"
        token_file = folder / "service.token"
        ready_path = Path(args.ready_file).expanduser() if args.ready_file else folder / "shared-ready.json"
        token = read_or_create_token(token_file)
        server = create_server(database, args.host, args.port, token)
        host, port = server.server_address[:2]
        local_url, shared_urls = connection_urls(host, port)
        state = {"host": host, "port": port, "url": local_url, "shared_urls": shared_urls,
                 "pid": os.getpid(), "version": 1, "database": str(database)}
        write_ready(ready_path, state)
        print("\n素材库服务已启动。", flush=True)
        print("本机连接地址：" + local_url, flush=True)
        for url in shared_urls:
            print("机房电脑连接地址：" + url, flush=True)
        if host == "0.0.0.0" and not shared_urls:
            print(f"机房电脑连接地址：http://<这台电脑的局域网 IP>:{port}", flush=True)
            print("可在系统设置的网络详情中查看局域网 IP。", flush=True)
        print("连接令牌：" + token, flush=True)
        print("数据库：" + str(database), flush=True)
        print("各客户端填写同一服务地址和令牌，并填写各自姓名用于操作记录。", flush=True)
        print("保持此窗口和服务电脑开启；按 Control+C 可停止服务。\n", flush=True)

        def stop(_signal, _frame):
            raise KeyboardInterrupt
        old_term = signal.signal(signal.SIGTERM, stop)
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\n共享素材库服务已停止，已提交的资料仍保留在本机数据库。", flush=True)
    except (LibraryError, OSError, ValueError) as exc:
        print("共享素材库未能启动：" + str(exc), file=sys.stderr, flush=True)
        if isinstance(exc, OSError) and exc.errno in (48, 98, 10048):
            print("端口可能已被占用，请先确认服务是否已经启动，或使用 --port 指定另一个端口。", file=sys.stderr, flush=True)
        return 1
    finally:
        if old_term is not None:
            signal.signal(signal.SIGTERM, old_term)
        if server is not None:
            server.server_close()
            server.store.close()
        if ready_path is not None:
            try:
                # A failed second instance must not remove the first service's state.
                state = json.loads(ready_path.read_text(encoding="utf-8"))
                if state.get("pid") == os.getpid():
                    ready_path.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
