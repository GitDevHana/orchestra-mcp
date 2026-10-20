from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from contextlib import AsyncExitStack, ExitStack, asynccontextmanager
from pathlib import Path
from typing import Literal
from mcp.server.fastmcp import FastMCP
from .engine import Manager
from .models import RunSpec, NextStageSpec, ResumeSpec
from .providers import doctor
from .store import Store
from .visualize import export_run, render_archify
from .web import Dashboard

APP: Manager | None = None
WEB: Dashboard | None = None
OPTIONS = None


class InstanceLock:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.file = (root / "server.lock").open("a+b")
        self.file.seek(0)
        self.file.write(b"0")
        self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise RuntimeError("Another orchestrator owns this data directory; use its dashboard or another --data-dir")

    def close(self):
        self.file.close()


@asynccontextmanager
async def service(root: Path, port=0, *, workspace_roots=None, allow_any_workspace=False):
    global APP, WEB
    with ExitStack() as resources:
        lock = InstanceLock(root)
        resources.callback(lock.close)
        store = Store(root)
        resources.callback(store.close)
        async with AsyncExitStack() as services:
            manager = Manager(store, workspace_roots=[Path.cwd()] if workspace_roots is None else workspace_roots,
                              allow_any_workspace=allow_any_workspace)
            services.push_async_callback(manager.shutdown)
            manager.recover()
            cli = Path(os.environ["ORCH_ARCHIFY_CLI"]) if os.environ.get("ORCH_ARCHIFY_CLI") else None
            dashboard = Dashboard(manager, port, cli)
            services.push_async_callback(dashboard.close)
            APP, WEB = manager, dashboard
            try:
                metadata = {"dashboard": dashboard.url, "pid": os.getpid(), "data_dir": str(root.resolve()),
                            "workspace_roots": [str(p) for p in manager.workspace_roots],
                            "allow_any_workspace": manager.allow_any_workspace}
                (root / "server.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
                print("Dashboard: " + dashboard.url, file=sys.stderr, flush=True)
                yield manager
            finally:
                APP = WEB = None


@asynccontextmanager
async def lifespan(_):
    if OPTIONS.shared_owner:
        yield {"manager": app()}
        return
    async with service(OPTIONS.data_dir, OPTIONS.port, workspace_roots=OPTIONS.workspace_root,
                       allow_any_workspace=OPTIONS.allow_any_workspace) as manager:
        yield {"manager": manager}


mcp = FastMCP("Agent Orchestra", instructions="Plan work first, show the initial scope to the user, then approve_and_start only with user authorization. Additional approvals are collected and executed only after an explicit user decision. Child CLI token budgets are best-effort, not exact billing caps.", lifespan=lifespan)


def app() -> Manager:
    if APP is None:
        raise RuntimeError("Service not started")
    return APP


@mcp.tool()
async def orchestrator_doctor() -> dict:
    """Discover installed CLIs, approval policy, budget limitations and dashboard URL. No model calls."""
    return {**doctor(), "dashboard": app().base_url,
            "runtime": {"module": str(Path(__file__).resolve()), "python": sys.executable,
                        "workspace_roots": [str(p) for p in app().workspace_roots],
                        "allow_any_workspace": app().allow_any_workspace,
                        "default_command_backend": RunSpec.model_fields["command_backend"].default}}


@mcp.tool()
async def get_codex_quota() -> dict:
    """Read Codex account subscription percentages/reset windows via app-server. No model/thread or credential export. Missing windows remain unknown."""
    return await app().get_codex_quota()


@mcp.tool()
async def confirm_quota_unknown(run_id: str, approve: bool) -> dict:
    """Record the user's explicit decision to proceed with missing quota telemetry for this run. Known percentage thresholds remain enforced. Never infer consent."""
    return app().confirm_missing_quota(run_id, approve)


@mcp.tool()
async def plan_task(spec: RunSpec) -> dict:
    """Prepare a task with one/two independent agents (same provider allowed). Does NOT execute. Present the scope for initial user approval."""
    return app().plan(spec)


@mcp.tool()
async def plan_next_stage(run_id: str, decision: NextStageSpec) -> dict:
    """After user review, prepare a linked stage from preserved A/B results. Requires explicit base selection, feedback and NEW per-agent token/time budgets. Selected files are copied only into a new input folder. Does NOT start or merge into the original source."""
    return app().next_stage(run_id, decision)


@mcp.tool()
async def plan_resume(run_id: str, decision: ResumeSpec) -> dict:
    """Plan continuation from saved files and handoff with fresh budgets. Does not start execution."""
    return await asyncio.to_thread(app().resume, run_id, decision)


@mcp.tool()
async def list_agent_models() -> dict:
    """Local model suggestions and custom IDs. Does not verify account entitlement or call a model."""
    from .catalog import model_catalog
    return model_catalog()


@mcp.tool()
async def approve_and_start(run_id: str) -> dict:
    """Approve the exact displayed plan and run it. Requires user authorization of its workspace, agents, commands and budgets."""
    return await app().start(run_id)


@mcp.tool()
async def list_tasks() -> list[dict]:
    """List durable task status and parent/follow-up links."""
    return app().store.list()


@mcp.tool()
async def get_task(run_id: str) -> dict:
    """Get task state, token accounting and all deferred approval items."""
    return app().describe(run_id)


@mcp.tool()
async def get_events(run_id: str, after_seq: int = 0, limit: int = 100, agent: str | None = None) -> dict:
    """Page dialogue, provider/tool events, errors, file diffs, validation and state. Full original provider events are retained."""
    app().store.get(run_id)
    events = app().store.events(run_id, max(0, after_seq), max(1, min(limit, 500)), agent)
    return {"events": events, "next_after_seq": events[-1]["seq"] if events else after_seq, "has_more": len(events) == max(1,min(limit,500))}


@mcp.tool()
async def send_message(run_id: str, text: str, agent: str | None = None) -> dict:
    """Queue a user instruction for an agent's next turn. Does not inject keystrokes into a running CLI."""
    return app().message(run_id, text, agent)


@mcp.tool()
async def stop_task(run_id: str) -> dict:
    """Cancel a plan or stop the active run and its owned child process tree."""
    return await app().stop(run_id)


@mcp.tool()
async def decide_deferred(run_id: str, item_ids: list[str], approve: bool, command_overrides: dict[str, list[str]] | None = None) -> dict:
    """Record the user's decision on specific deferred items. Never infer approval. Does not execute them."""
    app().store.decide(run_id, item_ids, approve, command_overrides)
    return app().describe(run_id)


@mcp.tool()
async def run_approved_items(run_id: str, max_seconds: float = 300) -> dict:
    """Execute only approved, unexecuted items as a linked follow-up. Reuses original per-agent token limits (new budgets), one round, and permissions. It does not grant OS/CLI permission bypasses."""
    return await app().followup(run_id, max_seconds)


@mcp.tool()
async def export_task(run_id: str, archify: bool = False) -> dict:
    """Export full JSONL, standalone conversation HTML, approval report, and optional validated Archify sequence pages."""
    result = await asyncio.to_thread(export_run, app().store, run_id)
    if archify:
        result["archify"] = await render_archify(app().store, run_id, WEB.archify_cli)
    return result


@mcp.tool()
async def plan_offline_demo(mode: Literal["implement_review", "dual_implementers"] = "implement_review") -> dict:
    """Prepare an explicitly offline demonstration with Claude A and Claude B. No AI CLI or API is called."""
    return await WEB.action("/api/demo", {"mode": mode})


def main():
    global OPTIONS
    if os.environ.get("ORCH_CHILD") == "1":
        raise SystemExit("Recursive orchestrator execution from a child CLI is disabled")
    parser = argparse.ArgumentParser(description="Python MCP orchestrator and local dashboard")
    parser.add_argument("--data-dir", type=Path, default=Path(os.environ.get("ORCH_DATA_DIR", str(Path.home() / ".agent-orchestrator"))))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--workspace-root", type=Path, action="append",
                        help="Allowed source directory (repeatable); defaults to the server working directory")
    parser.add_argument("--dashboard-only", action="store_true")
    parser.add_argument("--allow-any-workspace", action="store_true",
                        help="Allow any existing source folder selected in a task plan")
    parser.add_argument("--shared-http", dest="shared_owner", action="store_true",
                        help="Run one persistent loopback MCP server for multiple clients")
    parser.add_argument("--mcp-port", type=int, default=0, help="Shared HTTP MCP port (0 chooses a free port)")
    OPTIONS = parser.parse_args()
    if OPTIONS.shared_owner:
        from .shared import owner
        asyncio.run(owner(OPTIONS))
    elif OPTIONS.dashboard_only:
        async def serve():
            async with service(OPTIONS.data_dir, OPTIONS.port, workspace_roots=OPTIONS.workspace_root,
                               allow_any_workspace=OPTIONS.allow_any_workspace):
                await asyncio.Event().wait()
        try:
            asyncio.run(serve())
        except KeyboardInterrupt:
            pass
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
