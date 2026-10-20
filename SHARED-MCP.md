# Shared Orchestra MCP

Use `--shared-http` for multiple Codex sessions. One independently running owner
holds SQLite, the manager and dashboard. Each HTTP client has its own MCP session;
closing/reopening a client neither recovers tasks nor shuts down the owner.
Existing stdio and dashboard-only modes remain available for single-owner use.

The HTTP endpoint binds only to 127.0.0.1 and requires a bearer credential. Host
and Origin are checked. Credentials live in the existing private data directory
and Codex configuration; do not publish these files. The workspace allowlist and
default container execution policy still apply. Shared connections have the same
local-user permissions, not separate user identities.

From an ordinary terminal, run the project's Python with:

```powershell
.\.venv\Scripts\python.exe scripts\connect_shared.py --data-dir 'C:\path\to\records' --workspace-root 'C:\path\to\project' --apply
```

Repeat `--workspace-root` for each allowed project. The helper starts a hidden
independent owner, validates readiness, backs up Codex config, and replaces only
`mcp_servers.orchestra` with its authenticated HTTP registration. It reuses an
already healthy owner. An existing stdio/dashboard owner must be stopped first,
after checking for running tasks; the helper never kills it or bypasses its lock.

Use `--allow-any-workspace` on both the helper and owner to accept any existing
source folder selected in a plan. This PC's Start-Orchestra.cmd enables it at the
user's request. The health policy distinguishes this mode from restricted roots;
switching modes requires an owner restart. Plans still require start approval,
and Orchestra's own records folder cannot be selected as source code.

The default MCP port is 8766; `--mcp-port` chooses another port. An existing owner
with different runtime/workspace roots is rejected rather than silently reused.
The bearer credential survives deliberate owner restarts. Registering another
data directory on the same fixed port fails instead of attaching to it.

The owner survives Codex client restarts. This setup does not install an OS service
or login task: after reboot, rerun the same helper before starting Codex. To stop
the owner, verify the current PID in `server.json` and check active tasks first.
Interrupted paid work is recorded on owner restart and is never replayed automatically.

Validation: `tests/test_shared.py` uses two concurrent real HTTP MCP sessions,
checks all 15 tools and shared records, closes clients and reconnects a third,
and verifies missing authentication, foreign Origin and mismatched policy rejection.
No model calls are made.
