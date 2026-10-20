"""Remaining local checks and preview restart. No AI/model calls or network installs.

Runs pytest, the stdio/HTTP smoke test, Archify's offline-demo renderer and the
headless Chrome UI check. Restarts only this project's inactive preview server.
Saves test receipts and dependency versions. Does not change host permissions.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import urlopen, Request

ROOT = Path(__file__).resolve().parent.parent
ENV = {**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}


def run(argv, quiet=False):
    result = subprocess.run(argv, cwd=ROOT, env=ENV, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if not quiet:
        print(result.stdout, end="", flush=True)
    if result.returncode:
        print(result.stderr, file=sys.stderr, flush=True)
        raise SystemExit(result.returncode)
    return result


def restart_preview():
    metadata = ROOT / "demo-data" / "records" / "server.json"
    if metadata.is_file():
        old = json.loads(metadata.read_text(encoding="utf-8"))
        try:
            url = old["dashboard"]
            parsed = urlsplit(url)
            request = Request(f"{parsed.scheme}://{parsed.netloc}/api/runs?{parsed.query}")
            with urlopen(request, timeout=2) as response:
                statuses = json.load(response)
            if any(r["status"] == "running" for r in statuses):
                raise SystemExit("Preview has an active task; refusing to interrupt it")
            pid = int(old["pid"])
            # Verify the process identity and exact project before stopping it.
            script = (f"$p=Get-CimInstance Win32_Process -Filter 'ProcessId={pid}'; "
                      "if ($p.CommandLine -like '*-m orchestrator --dashboard-only*' "
                      "-and $p.CommandLine -like '*agent-orchestrator*') { Stop-Process -Id $p.ProcessId }")
            run([r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe", "-NoProfile", "-Command", script])
        except (OSError, ValueError):
            pass
    out = (ROOT / "demo-data" / "server-out.log").open("ab")
    err = (ROOT / "demo-data" / "server-error.log").open("ab")
    kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    process = subprocess.Popen([sys.executable, "-m", "orchestrator", "--dashboard-only", "--data-dir", "demo-data/records", "--port", "8765"],
                               cwd=ROOT, env=ENV, stdin=subprocess.DEVNULL, stdout=out, stderr=err, **kwargs)
    out.close()
    err.close()
    for _ in range(100):
        if process.poll() is not None:
            raise SystemExit("Preview startup failed; inspect demo-data/server-error.log")
        try:
            current = json.loads(metadata.read_text(encoding="utf-8"))
            if current["pid"] != old.get("pid") if 'old' in locals() else True:
                with urlopen(current["dashboard"], timeout=1) as response:
                    if response.status == 200:
                        return current
        except (OSError, ValueError):
            pass
        time.sleep(.1)
    raise SystemExit("Preview startup timed out")


def main():
    if "--probe-quota" in sys.argv:
        run([sys.executable, str(ROOT / "scripts" / "probe_codex_quota.py")])
    tests = run([sys.executable, "-m", "pytest", "-q", "--tb=short"])
    (ROOT / "test-result.txt").write_text(tests.stdout, encoding="utf-8")
    run([sys.executable, str(ROOT / "scripts" / "smoke_mcp.py")])
    run([sys.executable, str(ROOT / "scripts" / "make_demo.py")])
    metadata = restart_preview()
    run([r"C:\Program Files\nodejs\node.exe", str(ROOT / "scripts" / "browser_smoke.mjs")])
    frozen = run([sys.executable, "-m", "pip", "freeze"], quiet=True)
    lines = [line for line in frozen.stdout.splitlines() if not line.startswith("-e ") and not line.startswith("# Editable")]
    (ROOT / "requirements-lock.txt").write_text("\n".join(lines)+"\n", encoding="utf-8")
    (ROOT / "preview.html").write_text('<!doctype html><html lang="ko"><meta charset="utf-8"><title>Agent Orchestra</title><meta http-equiv="refresh" content="0;url='+metadata["dashboard"]+'"><a href="'+metadata["dashboard"]+'">실행 화면 열기</a></html>', encoding="utf-8")
    print(json.dumps({"status":"verified", "dashboard":metadata["dashboard"], "project":str(ROOT)}, ensure_ascii=True))


if __name__ == "__main__":
    main()
