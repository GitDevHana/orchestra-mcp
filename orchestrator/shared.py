"""Persistent authenticated loopback MCP server, independent of client processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import socket
import sys

import httpx
import uvicorn


def policy(options):
    return {"module": str(Path(__file__).with_name("server.py").resolve()),
            "python": sys.executable,
            "workspace_roots": [str(Path(p).resolve()) for p in (options.workspace_root or [Path.cwd()])],
            **({"allow_any_workspace": True} if getattr(options, "allow_any_workspace", False) else {})}


async def ready(options):
    try:
        meta = json.loads((options.data_dir / "server.json").read_text(encoding="utf-8"))
        connection = meta["shared_mcp"]
        port = connection["port"]
        if type(port) is not int or not 0 < port < 65536:
            return None
        async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
            response = await client.get(f"http://127.0.0.1:{port}/health",
                headers={"Authorization": "Bearer " + connection["token"]})
        if response.status_code != 200:
            return None
        if response.json() != {"pid": meta["pid"], "policy": policy(options)}:
            raise RuntimeError("Shared Orchestra has different runtime/workspace roots")
        return connection
    except (OSError, ValueError, KeyError, httpx.HTTPError):
        return None


async def owner(options):
    from .server import mcp, service

    options.data_dir = options.data_dir.resolve()
    options.data_dir.mkdir(parents=True, exist_ok=True)
    # Stable across intentional restarts; never put the credential in argv/logs.
    token_file = options.data_dir / "shared-mcp-token"
    try:
        with token_file.open("x", encoding="ascii") as handle:
            handle.write(secrets.token_urlsafe(32))
    except FileExistsError:
        pass
    token = token_file.read_text(encoding="ascii").strip()
    if not 32 <= len(token) <= 128 or not token.isascii():
        raise RuntimeError("Invalid shared MCP credential file")
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", options.mcp_port))
        port = sock.getsockname()[1]
        sock.setblocking(False)
        expected_host = f"127.0.0.1:{port}"
        http_app = mcp.streamable_http_app()

        async def authenticated(scope, receive, send):
            if scope["type"] == "http":
                headers = dict(scope["headers"])
                valid = (headers.get(b"host") == expected_host.encode()
                    and headers.get(b"origin") in (None, ("http://" + expected_host).encode())
                    and secrets.compare_digest(headers.get(b"authorization", b""), ("Bearer " + token).encode()))
                if not valid:
                    await send({"type": "http.response.start", "status": 403, "headers": []})
                    await send({"type": "http.response.body", "body": b"Forbidden"})
                    return
                if scope["path"] == "/health":
                    body = json.dumps({"pid": os.getpid(), "policy": policy(options)}).encode()
                    await send({"type": "http.response.start", "status": 200,
                                "headers": [(b"content-type", b"application/json")]})
                    await send({"type": "http.response.body", "body": body})
                    return
            await http_app(scope, receive, send)

        # Manager/recover/shutdown belong to the owner, never individual sessions.
        async with service(options.data_dir, options.port, workspace_roots=options.workspace_root,
                           allow_any_workspace=getattr(options, "allow_any_workspace", False)):
            path = options.data_dir / "server.json"
            meta = json.loads(path.read_text(encoding="utf-8"))
            meta["shared_mcp"] = {"port": port, "token": token}
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
            server = uvicorn.Server(uvicorn.Config(authenticated, log_level="warning", access_log=False))
            await server.serve(sockets=[sock])
    finally:
        sock.close()
