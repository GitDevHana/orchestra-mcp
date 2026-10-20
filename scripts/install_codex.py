"""Add only this MCP server, preserving and backing up existing Codex config."""
import json
import os
import shutil
import sys
import tomllib
from datetime import datetime
from pathlib import Path

directory = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
path = directory / "config.toml"
text = path.read_text(encoding="utf-8") if path.is_file() else ""
config = tomllib.loads(text)
if "orchestra" in config.get("mcp_servers", {}):
    print(json.dumps({"status": "already_registered", "path": str(path)}))
else:
    addition = "\n[mcp_servers.orchestra]\ncommand = " + json.dumps(sys.executable, ensure_ascii=False) + "\nargs = ['-m', 'orchestrator']\nstartup_timeout_sec = 30\ntool_timeout_sec = 600\n"
    tomllib.loads(text + addition)
    backup = path.with_name(path.name + ".before-orchestra-" + datetime.now().strftime("%Y%m%d-%H%M%S") + ".bak")
    if path.is_file():
        shutil.copy2(path, backup)
    directory.mkdir(parents=True, exist_ok=True)
    path.write_text(text + addition, encoding="utf-8")
    print(json.dumps({"status": "registered", "path": str(path), "backup": str(backup)}, ensure_ascii=True))
