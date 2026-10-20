import asyncio
from contextlib import AsyncExitStack
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tomllib
from types import SimpleNamespace

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
import pytest

from orchestrator.shared import ready


@pytest.mark.asyncio
async def test_shared_clients_disconnect_and_reconnect(tmp_path):
    records = tmp_path / "records"
    records.mkdir()
    options = SimpleNamespace(data_dir=records, workspace_root=[tmp_path])
    # Independent owner: client process-tree cleanup must not own this server.
    with (records / "owner.log").open("wb") as log:
        process = subprocess.Popen([sys.executable, "-X", "utf8", "-m", "orchestrator",
            "--shared-http", "--data-dir", str(records), "--workspace-root", str(tmp_path)],
            cwd=str(Path(__file__).resolve().parents[1]), stdin=subprocess.DEVNULL, stdout=log, stderr=log,
            **({"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}))

    async def connect(stack, connection):
        http = await stack.enter_async_context(httpx.AsyncClient(trust_env=False,
            headers={"Authorization": "Bearer " + connection["token"]}))
        r, w, _ = await stack.enter_async_context(streamable_http_client(
            f"http://127.0.0.1:{connection['port']}/mcp", http_client=http))
        return await stack.enter_async_context(ClientSession(r, w))

    def data(result):
        assert not result.isError
        return result.structuredContent or json.loads(result.content[0].text)

    try:
        async with asyncio.timeout(35):
            connection = None
            while connection is None:
                assert process.poll() is None, "Owner exited; inspect owner.log"
                connection = await ready(options)
                await asyncio.sleep(.1)
            async with AsyncExitStack() as first, AsyncExitStack() as second:
                a = await connect(first, connection)
                b = await connect(second, connection)
                await asyncio.gather(a.initialize(), b.initialize())
                catalogs = await asyncio.gather(a.list_tools(), b.list_tools())
                assert len(catalogs[0].tools) == len(catalogs[1].tools) == 17
                assert catalogs[0].model_dump() == catalogs[1].model_dump()
                doctors = await asyncio.gather(a.call_tool("orchestrator_doctor", {}),
                                               b.call_tool("orchestrator_doctor", {}))
                assert data(doctors[0])["dashboard"] == data(doctors[1])["dashboard"]
                plan = data(await a.call_tool("plan_offline_demo", {}))
                tasks = data(await b.call_tool("list_tasks", {}))["result"]
                assert any(t["id"] == plan["id"] and t["status"] == "awaiting_approval" for t in tasks)
                pid = json.loads((records / "server.json").read_text(encoding="utf-8"))["pid"]
                async with httpx.AsyncClient(trust_env=False) as client:
                    url = f"http://127.0.0.1:{connection['port']}/health"
                    assert (await client.get(url)).status_code == 403
                    assert (await client.get(url, headers={"Authorization": "Bearer " + connection["token"],
                        "Origin": "https://example.com"})).status_code == 403
                with pytest.raises(RuntimeError, match="different runtime/workspace"):
                    await ready(SimpleNamespace(data_dir=records, workspace_root=[records]))
                await second.aclose()
                assert data(await a.call_tool("get_task", {"run_id": plan["id"]}))["status"] == "awaiting_approval"
            assert await ready(options)
            async with AsyncExitStack() as third:
                c = await connect(third, connection)
                await c.initialize()
                assert len((await c.list_tools()).tools) == 17
                assert data(await c.call_tool("get_task", {"run_id": plan["id"]}))["status"] == "awaiting_approval"
                assert json.loads((records / "server.json").read_text(encoding="utf-8"))["pid"] == pid
            config = tmp_path / "config.toml"
            original = ('model = "test"\n[mcp_servers.rea]\ncommand = "keep-me"\n'
                        '[mcp_servers.orchestra]\ncommand = "old"\nargs = ["-m", "orchestrator"]\n')
            config.write_text(original, encoding="utf-8")
            setup = await asyncio.to_thread(subprocess.run, [sys.executable, "-X", "utf8",
                str(Path(__file__).resolve().parents[1] / "scripts/connect_shared.py"),
                "--data-dir", str(records), "--workspace-root", str(tmp_path),
                "--config", str(config), "--apply"], capture_output=True, timeout=10)
            assert setup.returncode == 0, setup.stderr.decode("utf-8", "replace")
            assert connection["token"].encode() not in setup.stdout
            parsed = tomllib.loads(config.read_text(encoding="utf-8"))
            assert parsed["model"] == "test"
            assert parsed["mcp_servers"]["rea"] == {"command": "keep-me"}
            assert parsed["mcp_servers"]["orchestra"]["url"] == f"http://127.0.0.1:{connection['port']}/mcp"
            assert "command" not in parsed["mcp_servers"]["orchestra"]
            assert len(list(tmp_path.glob("config.toml.shared-backup-*"))) == 1
    finally:
        meta = records / "server.json"
        if meta.exists():
            saved = json.loads(meta.read_text(encoding="utf-8"))
            assert Path(saved["data_dir"]) == records
            try:
                os.kill(saved["pid"], signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.terminate()
            process.wait(timeout=5)
