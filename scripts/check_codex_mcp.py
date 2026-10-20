"""Verify registered tools through a fresh Codex app-server, without model calls."""
import argparse
import asyncio
import json
from pathlib import Path
import sys


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence", type=Path)
    options = parser.parse_args()
    cli = Path.home() / "AppData/Roaming/npm/node_modules/@openai/codex/bin/codex.js"
    node = Path("C:/Program Files/nodejs/node.exe")
    proc = await asyncio.create_subprocess_exec(str(node), str(cli), "app-server", "--stdio",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    counter = 0

    async def request(method, params):
        nonlocal counter
        counter += 1
        number = counter
        proc.stdin.write((json.dumps({"id": number, "method": method, "params": params}) + "\n").encode())
        await proc.stdin.drain()
        while True:
            line = await proc.stdout.readline()
            if not line:
                raise RuntimeError("Codex daemon control connection closed")
            message = json.loads(line)
            if message.get("id") == number:
                if "error" in message:
                    raise RuntimeError("Codex control request failed: " + method)
                return message.get("result")
            if "id" in message and "method" in message:
                raise RuntimeError("Unexpected Codex server request; no approvals inferred")

    try:
        async with asyncio.timeout(40):
            await request("initialize", {"clientInfo": {"name": "orchestra-connection-check", "version": "1"},
                                         "capabilities": {"experimentalApi": True}})
            proc.stdin.write(b'{"method":"initialized"}\n')
            await proc.stdin.drain()
            params = {"serverName": "orchestra", "detail": "toolsAndAuthOnly"}
            result = await request("mcpServerStatus/list", params)
            evidence = {"servers": [{"name": s["name"], "tools": len(s["tools"])} for s in result["data"]],
                        "scope": "fresh_codex_app_server", "paid_model_calls": 0}
            print(json.dumps(evidence))
            if options.evidence:
                options.evidence.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
            assert any(s["name"] == "orchestra" and s["tools"] == 17 for s in evidence["servers"])
    finally:
        proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except asyncio.TimeoutError:
            proc.terminate()
            await proc.wait()


if __name__ == "__main__":
    asyncio.run(main())
