"""Start the independent shared owner and register its HTTP endpoint in Codex.

Run from a normal terminal, independently of an MCP stdio client. Re-run after
a Windows restart; it reuses an already healthy owner and preserves all tasks.
"""
import argparse
import asyncio
from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import tomllib

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))
from orchestrator.shared import ready
from orchestrator.server import InstanceLock


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--workspace-root", type=Path, action="append", required=True)
    parser.add_argument("--mcp-port", type=int, default=8766)
    parser.add_argument("--config", type=Path, default=Path.home() / ".codex/config.toml")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--allow-any-workspace", action="store_true")
    options = parser.parse_args()
    options.data_dir = options.data_dir.resolve()
    options.workspace_root = [p.resolve() for p in options.workspace_root]
    if not all(p.is_dir() for p in options.workspace_root):
        raise ValueError("Workspace roots must exist")
    if not options.apply:
        print("Validated paths. Use --apply to start the server and update only the orchestra registration.")
        return
    options.data_dir.mkdir(parents=True, exist_ok=True)
    # Serializes launch/config writes by this helper; never removes the DB lock.
    with_lock = InstanceLock(options.data_dir / ".shared-startup")
    try:
        connection = await ready(options)
        if connection is None:
            probe = InstanceLock(options.data_dir)
            probe.close()
            args = [sys.executable, "-X", "utf8", "-m", "orchestrator", "--shared-http",
                    "--data-dir", str(options.data_dir), "--mcp-port", str(options.mcp_port)]
            for root in options.workspace_root:
                args.extend(["--workspace-root", str(root)])
            if options.allow_any_workspace:
                args.append("--allow-any-workspace")
            with (options.data_dir / "shared-owner.log").open("ab") as log:
                child = subprocess.Popen(args, cwd=str(PROJECT), stdin=subprocess.DEVNULL,
                    stdout=log, stderr=log,
                    **({"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
                       if os.name == "nt" else {"start_new_session": True}))
            deadline = time.monotonic() + 20
            while connection is None and time.monotonic() < deadline:
                connection = await ready(options)
                if child.poll() is not None:
                    raise RuntimeError("Shared owner exited; inspect shared-owner.log")
                await asyncio.sleep(.1)
            if connection is None:
                raise RuntimeError("Shared owner readiness timed out; registration unchanged")
        config = options.config
        original = config.read_bytes()
        text = original.decode("utf-8-sig")
        before = tomllib.loads(text)
        url = f"http://127.0.0.1:{connection['port']}/mcp"
        entry = ('[mcp_servers.orchestra]\nurl = ' + json.dumps(url) + '\n'
                 'http_headers = { Authorization = ' + json.dumps('Bearer ' + connection['token']) + ' }\n'
                 'startup_timeout_sec = 30\ntool_timeout_sec = 600\n\n')
        updated, count = re.subn(r'(?ms)^\[mcp_servers\.orchestra\].*?(?=^\[(?!mcp_servers\.orchestra\.)|\Z)',
                                lambda _: entry, text)
        if count != 1:
            raise RuntimeError("Expected exactly one existing orchestra registration")
        after = tomllib.loads(updated)
        expected = {**before, "mcp_servers": {**before['mcp_servers'], 'orchestra': after['mcp_servers']['orchestra']}}
        assert after == expected, "Unrelated configuration changed"
        assert config.read_bytes() == original, "Config changed during setup"
        if updated != text:
            backup = config.with_name(config.name + '.shared-backup-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f'))
            backup.write_bytes(original)
            temporary = config.with_name(config.name + '.shared.tmp')
            temporary.write_text(updated, encoding='utf-8', newline='\n')
            temporary.replace(config)
        print(json.dumps({'status': 'ready', 'url': url, 'config': str(config),
                          'other_mcp_servers': 'unchanged', 'paid_model_calls': 0}, ensure_ascii=True))
    finally:
        with_lock.close()


if __name__ == '__main__':
    asyncio.run(main())
