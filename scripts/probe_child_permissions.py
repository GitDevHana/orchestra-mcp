"""No model calls. Compare child Codex permission context through MCP-style envs."""
import json
import os
import subprocess
import sys
from pathlib import Path
from mcp.client.stdio import get_default_environment
from orchestrator.providers import executable, codex_child_overrides

root = Path(__file__).resolve().parent.parent
workspace = root / "real-runs" / "permissions-probe"
workspace.mkdir(parents=True, exist_ok=True)
prefix = executable("codex")
for name, env, extra in [
    ("inherited", dict(os.environ), []),
    ("mcp_defaults", get_default_environment(), []),
    ("mcp_windows_config", get_default_environment(), ["-c", 'windows.sandbox="elevated"']),
    ("child_overrides", get_default_environment(), codex_child_overrides(str(workspace))),
]:
    result = subprocess.run(prefix + ["-a", "never", "--sandbox", "workspace-write"] + extra
        + ["debug", "prompt-input", "Permission context inspection only; do not execute tasks."],
        cwd=workspace, env=env, capture_output=True, timeout=30)
    text = result.stdout.decode("utf-8", "replace")
    try:
        context = json.loads(text)
        permissions = []
        def collect(value):
            if isinstance(value, dict):
                for key, part in value.items():
                    if key == "text" and isinstance(part, str) and ("<permissions instructions>" in part or "<filesystem>" in part):
                        permissions.append(part)
                    else:
                        collect(part)
            elif isinstance(value, list):
                for part in value:
                    collect(part)
        collect(context)
        print(json.dumps({"case": name, "returncode": result.returncode, "permissions": permissions}, ensure_ascii=True), flush=True)
    except ValueError:
        print(json.dumps({"case": name, "returncode": result.returncode,
            "error": result.stderr.decode("utf-8", "replace")[-2000:]}, ensure_ascii=True), flush=True)
