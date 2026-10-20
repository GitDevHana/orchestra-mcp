"""Offline regressions for the unfinished October 19 reliability changes."""
import asyncio
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import pytest

from orchestrator import audit
from orchestrator.budget import TokenGuard
from orchestrator.engine import Manager
from orchestrator.environment import child_environment
from orchestrator.execution import check_policy, container_argv
from orchestrator.locking import WorkspaceLease
from orchestrator.models import AgentSpec, NextStageSpec, RunSpec
from orchestrator.providers import Slot, StreamState, normalized
from orchestrator.responses import HISTORY_CHARS, HISTORY_TURNS, remember, response_object
from orchestrator.server import service
from orchestrator.store import Store
from orchestrator.visualize import export_run
from orchestrator.web import Dashboard


class Answer:
    async def stream(self, slot, prompt, workspace, access):
        yield {"kind": "text", "text": '{"summary":"ok","verdict":"done"}'}
        yield {"kind": "process_exit", "returncode": 0}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {
        "providers": {p: {"installed": True} for p in ("codex", "claude", "cursor")}})
    workspace = tmp_path / "source"
    workspace.mkdir()
    store = Store(tmp_path / "records")
    yield store, workspace
    store.close()


def spec(workspace, **kw):
    return RunSpec(task="offline regression", workspace=str(workspace),
                   agents=[{"provider": "claude"}], max_rounds=1, **kw)


async def execute(manager, config):
    run = manager.plan(config)["id"]
    await manager.start(run)
    await asyncio.wait_for(manager.tasks[run], 10)
    return manager.describe(run)


@pytest.mark.parametrize("verdict", [[], {}, None, 1, True, "invalid"])
def test_invalid_result_verdict_is_continue(verdict):
    text = json.dumps({"summary": "bad response", "verdict": verdict})
    assert response_object(text)["verdict"] == "continue"


@pytest.mark.parametrize("text,verdict", [
    ('Example: {"summary":"example","verdict":"done"}\n'
     '{"summary":"real","verdict":"continue"}', "continue"),
    ('{"summary":"ok","verdict":"done"}\nStill working', "continue"),
    ('{"summary":"ok","verdict":"done"}\n{"broken":', "continue"),
    ('{"example":{"summary":"example","verdict":"done"}}', "continue"),
    ('```json\n{"summary":"ok","verdict":"done"}\n```', "done"),
])
def test_result_requires_valid_last_object(text, verdict):
    assert response_object(text)["verdict"] == verdict


def test_history_retains_bounded_summaries_without_prompts():
    history = []
    for i in range(40):
        remember(history, {"summary": f"turn-{i}:" + "x" * 8000, "verdict": "continue",
                           "prompt": json.dumps(history), "deferred": ["private-context"]})
    assert 0 < len(history) <= HISTORY_TURNS
    assert len(json.dumps(history, ensure_ascii=False)) <= HISTORY_CHARS
    assert all(set(item) == {"summary", "verdict"} for item in history)
    assert history[-1]["summary"].startswith("turn-39:")


@pytest.mark.parametrize("denial", [False, True])
@pytest.mark.parametrize("code", [0, 7])
async def test_permission_wording_does_not_hide_exit_failure(context, denial, code):
    store, workspace = context
    class Failed(Answer):
        async def stream(self, *args):
            for event in normalized("codex", {"type": "item.completed", "item": {
                "id": "m", "type": "agent_message", "text": "Permission denied"}}):
                yield event
            if denial:
                yield {"kind": "permission", "request": {"command": "blocked"}}
            yield {"kind": "final_text", "text": '{"summary":"result","verdict":"done"}'}
            yield {"kind": "process_exit", "returncode": code}
    manager = Manager(store, Failed())
    try:
        result = await execute(manager, spec(workspace))
        assert result["status"] == ("failed" if code else "unverified")
        assert len(result["approvals"]) == int(denial)
    finally:
        await manager.shutdown()


def test_distinct_denials_preserve_reason_source_and_command(context):
    store, workspace = context
    run = store.create(spec(workspace))
    def defer(reason="one", argv=None, source="provider"):
        return store.defer(run, "a", "blocked tool", reason, "workspace", source, argv)
    first = defer(argv=["python", "first.py"])
    assert defer(argv=["python", "first.py"]) == first
    assert len({first, defer("two"), defer(source="agent"), defer(argv=["python", "second.py"])}) == 4
    store.decide(run, [first], False)
    assert defer(argv=["python", "first.py"]) != first


@pytest.mark.parametrize("change", ["create", "modify", "delete"])
async def test_read_only_reviewer_violation_fails_run(context, change):
    store, workspace = context
    target = workspace / "tracked.txt"
    if change != "create":
        target.write_text("before")
    class EditingReviewer(Answer):
        async def stream(self, slot, *args):
            if slot.key == "b":
                if change == "delete":
                    target.unlink()
                else:
                    target.write_text("after")
            async for event in super().stream(slot, *args):
                yield event
    config = spec(workspace).model_copy(update={"agents": [AgentSpec(provider="claude")] * 2})
    manager = Manager(store, EditingReviewer())
    try:
        result = await execute(manager, config)
        assert result["status"] == "failed"
        violations = store.recent(result["id"], ("read_only_violation",))
        assert violations[0]["agent"] == "b"
        assert violations[0]["payload"]["paths"] == ["tracked.txt"]
    finally:
        await manager.shutdown()


def test_tool_output_estimate_streams_without_duplicate_charge():
    state = StreamState()
    guard = TokenGuard(Slot("a", AgentSpec(provider="codex"), "local"), "prompt")
    previous = 0
    for kind, size in [("item.updated", 3000), ("item.updated", 6000), ("item.completed", 6000)]:
        events = normalized("codex", {"type": kind, "item": {
            "id": "tool-1", "type": "command_execution", "aggregated_output": "x" * size}}, state)
        for event in events:
            guard.observe(event)
        if kind == "item.updated":
            assert guard.extra_output_tokens > previous
            previous = guard.extra_output_tokens
        else:
            assert guard.extra_output_tokens == previous
    guard.observe({"kind": "meter", "tokens": 4000})
    usage = guard.commit()
    assert usage["accounted_tokens"] == 4000
    assert usage["estimated_tokens"] == 0


def test_anonymous_tool_calls_are_not_merged_by_type():
    state = StreamState()
    event = {"type": "item.completed", "item": {
        "type": "command_execution", "aggregated_output": "x" * 10000}}
    first = normalized("codex", event, state)
    second = normalized("codex", event, state)
    assert sum(e["estimated_tokens"] for e in first if e["kind"] == "token_output") > 0
    assert second == first


def test_audit_covers_binary_large_files_git_venv_and_file_5001(tmp_path):
    (tmp_path / "binary").write_bytes(b"\0before")
    (tmp_path / "large").write_bytes(b"x" * 2_100_000)
    for folder in (".git/hooks", ".venv/lib"):
        path = tmp_path / folder
        path.mkdir(parents=True)
        (path / "tracked").write_text("before")
    many = tmp_path / "many"
    many.mkdir()
    for i in range(5001):
        (many / f"{i:04}.txt").write_bytes(b"a")
    before = audit.strict_snapshot(str(tmp_path))
    changed = ["binary", "large", ".git/hooks/tracked", ".venv/lib/tracked", "many/5000.txt"]
    for name in changed:
        (tmp_path / name).write_bytes(b"\0after")
    after = audit.strict_snapshot(str(tmp_path))
    assert {f["path"] for f in audit.changes(before, after)["files"]} == set(changed)


def test_strict_audit_rejects_unreadable_file(tmp_path, monkeypatch):
    target = tmp_path / "unreadable"
    target.write_text("data")
    original = Path.open
    def fail(path, *args, **kwargs):
        if path == target:
            raise PermissionError("injected read failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(RuntimeError, match="Cannot verify"):
        audit.strict_snapshot(str(tmp_path))


def test_filtered_child_environment(monkeypatch):
    monkeypatch.setenv("ORCHESTRA_TEST_SECRET", "must-not-leak")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-leak")
    monkeypatch.setenv("CODEX_HOME", "configured-login-folder")
    monkeypatch.setenv("HTTPS_PROXY", "configured-proxy")
    for agent in (True, False):
        env = child_environment(agent=agent)
        assert "ORCHESTRA_TEST_SECRET" not in env and "OPENAI_API_KEY" not in env
        assert env["ORCH_CHILD"] == "1"
        assert env.get("PATH") == os.environ.get("PATH")
        assert ("CODEX_HOME" in env) is agent
        assert ("HTTPS_PROXY" in env) is agent


async def test_container_policy_never_runs_host_as_fallback(context, monkeypatch):
    store, workspace = context
    command = [sys.executable, "-c", "from pathlib import Path; Path('host-marker').touch()"]
    manager = Manager(store, Answer())
    for image, error in [(None, "이미지"), ("python:3.14-slim", "Docker")]:
        monkeypatch.setattr("orchestrator.execution.shutil.which", lambda _: None)
        run = manager.plan(spec(workspace, validation_commands=[command], command_image=image))["id"]
        with pytest.raises(ValueError, match=error):
            await manager.start(run)
        assert store.get(run)["status"] == "awaiting_approval"
        assert not (workspace / "host-marker").exists()
    await manager.shutdown()


def test_container_mount_and_argument_boundaries(context, monkeypatch):
    _, workspace = context
    monkeypatch.setattr("orchestrator.execution.shutil.which", lambda _: "docker")
    config = spec(workspace, command_image="python:3.14-slim", validation_commands=[["python", "-V"]])
    check_policy(config)
    argv = container_argv(config, ["python", "-c", "print('a; b')"], "test-container")
    assert argv[-4:] == ["python:3.14-slim", "python", "-c", "print('a; b')"]
    assert "--network=none" in argv and "--pull=never" in argv
    assert f"type=bind,src={workspace.resolve()},dst=/workspace" in argv


async def test_explicit_trusted_host_command_executes_and_records_failure(context):
    store, workspace = context
    manager = Manager(store, Answer())
    result = await execute(manager, spec(workspace, command_backend="trusted_host",
        execution_commands=[[sys.executable, "-c", "from pathlib import Path; Path('marker').touch(); raise SystemExit(3)"]]))
    assert result["status"] == "failed" and (workspace / "marker").exists()
    event = store.recent(result["id"], ("approved_execution",))[0]
    assert event["payload"]["returncode"] == 3
    await manager.shutdown()


async def test_different_data_dirs_lock_same_workspace_and_release_after_stop(context, tmp_path):
    store, workspace = context
    other_store = Store(tmp_path / "other-records")
    class Waiting(Answer):
        async def stream(self, *args):
            await asyncio.sleep(30)
            yield
    one = Manager(store, Waiting())
    two = Manager(other_store, Answer())
    first = one.plan(spec(workspace))["id"]
    second = two.plan(spec(workspace))["id"]
    try:
        await one.start(first)
        with pytest.raises(ValueError, match="another manager"):
            await two.start(second)
        await one.stop(first)  # Also covers cancellation before coroutine entry.
        assert store.get(first)["status"] == "cancelled"
        await two.start(second)
        await two.tasks[second]
        assert other_store.get(second)["status"] == "unverified"
    finally:
        await one.shutdown()
        await two.shutdown()
        other_store.close()


async def test_shutdown_before_coroutine_entry_marks_interrupted_and_releases(context):
    store, workspace = context
    manager = Manager(store, Answer())
    run = manager.plan(spec(workspace))["id"]
    await manager.start(run)
    await manager.shutdown()
    assert store.get(run)["status"] == "interrupted"
    with_lease = WorkspaceLease(str(workspace))
    with_lease.close()


async def test_shutdown_during_token_limit_cleanup_preserves_result(context, monkeypatch):
    store, workspace = context
    entered, release = asyncio.Event(), asyncio.Event()
    original = audit.snapshot
    class Limited(Answer):
        async def stream(self, *args):
            yield {"kind": "meter", "tokens": 5000}
    # Hold the audit that follows token-limit publication.
    original_to_thread = asyncio.to_thread
    async def paused(func, *args, **kwargs):
        if func is original and store.list() and store.list()[0]["status"] == "token_limit":
            entered.set()
            await release.wait()
        return await original_to_thread(func, *args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", paused)
    manager = Manager(store, Limited())
    run = manager.plan(
        spec(workspace).model_copy(update={"agents": [AgentSpec(provider="claude", max_tokens=5000)]}))["id"]
    await manager.start(run)
    await asyncio.wait_for(entered.wait(), 5)
    shutting_down = asyncio.create_task(manager.shutdown())
    await asyncio.sleep(0)
    release.set()
    await asyncio.wait_for(shutting_down, 5)
    assert store.get(run)["status"] == "token_limit"
    assert manager.tasks[run].exception() is None


async def test_workspace_scope_rechecked_at_start_and_empty_roots_rejected(context, tmp_path):
    store, workspace = context
    manager = Manager(store, Answer(), workspace_roots=[workspace])
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="outside"):
        manager.plan(spec(outside))
    run = manager.plan(spec(workspace))["id"]
    manager.workspace_roots = (outside,)
    with pytest.raises(ValueError, match="outside"):
        await manager.start(run)
    with pytest.raises(ValueError, match="at least one"):
        Manager(store, workspace_roots=[])
    with pytest.raises(ValueError, match="records directory"):
        Manager(store).plan(spec(store.root))
    await manager.shutdown()


async def test_any_workspace_option_accepts_user_folder_but_excludes_records(context, tmp_path):
    store, workspace = context
    outside = tmp_path / 'other-project'
    outside.mkdir()
    manager = Manager(store, Answer(), workspace_roots=[workspace], allow_any_workspace=True)
    result = await execute(manager, spec(outside))
    assert result['status'] == 'unverified'
    assert result['scope']['allow_any_workspace'] is True
    assert result['config']['workspace'] == str(outside.resolve())
    with pytest.raises(ValueError, match='records directory'):
        manager.plan(spec(store.root))
    with pytest.raises(ValueError, match='existing directory'):
        spec(tmp_path / 'missing-project')
    await manager.shutdown()


async def test_any_workspace_service_exposes_policy(tmp_path):
    async with service(tmp_path / 'records', allow_any_workspace=True) as manager:
        assert manager.allow_any_workspace is True
        meta = json.loads((manager.store.root / 'server.json').read_text(encoding='utf-8'))
        assert meta['allow_any_workspace'] is True
        def get_policy():
            with urlopen(manager.base_url.replace('/?token=', '/api/doctor?token=')) as response:
                return json.load(response)
        policy = await asyncio.to_thread(get_policy)
        assert policy['allow_any_workspace'] is True


async def test_server_root_defaults_to_cwd_and_demo_remains_available(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.chdir(source)
    async with service(tmp_path / "elsewhere" / "records") as manager:
        assert manager.workspace_roots == (source.resolve(),)
        dashboard = Dashboard(manager)
        try:
            demo = await dashboard.action("/api/demo", {})
            await manager.start(demo["id"])
            await manager.tasks[demo["id"]]
            assert manager.describe(demo["id"])["status"] == "unverified"
        finally:
            await dashboard.close()


def test_concurrent_exports_register_complete_files(context):
    store, workspace = context
    run = store.create(spec(workspace))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: export_run(store, run), range(8)))
    for result in results:
        assert store.artifact_bytes(Path(result["transcript"])).startswith(b"<!doctype")
    assert not list((store.root / "runs" / run).glob("*.tmp"))


async def test_http_artifact_registration_tamper_csp_and_non_ascii_auth(context):
    store, workspace = context
    manager = Manager(store)
    dashboard = Dashboard(manager)
    run = store.create(spec(workspace))
    path = store.root / "old-export.html"
    path.write_text("<!doctype html>old")
    base = dashboard.url.split("/?")[0]
    def request(route, token=None):
        address = base + route + ("&" if "?" in route else "?") + urlencode({"token": dashboard.token if token is None else token})
        try:
            with urlopen(Request(address), timeout=5) as response:
                return response.status, response.headers, response.read()
        except HTTPError as exc:
            return exc.code, exc.headers, exc.read()
    try:
        status, _, _ = await asyncio.to_thread(request, "/artifact?path=old-export.html")
        assert status == 400
        result = await asyncio.to_thread(export_run, store, run)
        relative = Path(result["transcript"]).relative_to(store.root).as_posix()
        status, headers, body = await asyncio.to_thread(request, "/artifact?path=" + relative)
        assert status == 200 and b'Agent A' in body
        assert "sandbox allow-scripts;" in headers["Content-Security-Policy"]
        assert "allow-same-origin" not in headers["Content-Security-Policy"]
        Path(result["transcript"]).write_text("modified")
        assert (await asyncio.to_thread(request, "/artifact?path=" + relative))[0] == 400
        assert (await asyncio.to_thread(request, "/api/runs", "한글"))[0] == 403
        # Explicit re-export repairs old/unregistered export access.
        await asyncio.to_thread(export_run, store, run)
        assert (await asyncio.to_thread(request, "/artifact?path=" + relative))[0] == 200
    finally:
        await dashboard.close()
        await manager.shutdown()


@pytest.mark.parametrize("five,weekly,reserve,expected", [
    (20, 30, 0, (20, 30)), (None, None, 0, (None, None)),
    (95, None, 10, (90, 90)),
])
async def test_next_stage_applies_new_quota_budget_without_changing_parent(context, five, weekly, reserve, expected):
    store, workspace = context
    async def quota():
        return {"status": "available", "windows": [
            {"limit_id": "codex", "window_minutes": 300, "used_percent": 1},
            {"limit_id": "codex", "window_minutes": 10080, "used_percent": 1}]}
    manager = Manager(store, Answer(), quota_reader=quota)
    parent_spec = RunSpec(task="offline dual", workspace=str(workspace), mode="dual_implementers",
        agents=[{"provider": "codex"}, {"provider": "codex"}], max_rounds=1,
        codex_five_hour_stop_at_used_percent=60, codex_weekly_stop_at_used_percent=70)
    try:
        parent = await execute(manager, parent_spec)
        original = store.get(parent["id"])
        child = manager.next_stage(parent["id"], NextStageSpec(
            feedback="keep existing files", base_candidate="a", max_seconds=30,
            agents=[{"max_tokens": 9000}, {"max_tokens": 10000}],
            codex_five_hour_stop_at_used_percent=five, codex_weekly_stop_at_used_percent=weekly,
            codex_reserve_percent=reserve))
        assert (child["config"]["codex_five_hour_stop_at_used_percent"],
                child["config"]["codex_weekly_stop_at_used_percent"]) == expected
        assert store.get(parent["id"])["config"] == original["config"]
        assert store.get(parent["id"])["usage"] == original["usage"]
        assert child["usage"] == {}
        # Internal stage input remains permitted with a narrow external root.
        manager.workspace_roots = (workspace,)
        await manager.start(child["id"])
        await manager.tasks[child["id"]]
        assert manager.describe(child["id"])["status"] == "unverified"
    finally:
        await manager.shutdown()


async def test_workspace_lease_is_cross_process_and_released_on_process_death(context):
    _, workspace = context
    code = ("import sys; from orchestrator.locking import WorkspaceLease; "
            "lease=WorkspaceLease(sys.argv[1]); print('locked',flush=True); sys.stdin.read()")
    process = await asyncio.create_subprocess_exec(sys.executable, "-c", code, str(workspace),
        cwd=str(Path(__file__).resolve().parents[1]), stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=child_environment())
    try:
        assert (await asyncio.wait_for(process.stdout.readline(), 5)).strip() == b"locked"
        with pytest.raises(ValueError, match="another manager"):
            WorkspaceLease(str(workspace))
    finally:
        if process.returncode is None:
            process.kill()
        await process.wait()
    lease = WorkspaceLease(str(workspace))
    lease.close()


async def test_failed_server_setup_releases_data_lock(context, tmp_path):
    _, workspace = context
    records = tmp_path / "failed-start-records"
    with pytest.raises(ValueError, match="at least one"):
        async with service(records, workspace_roots=[]):
            pass
    async with service(records, workspace_roots=[workspace]) as manager:
        assert manager.workspace_roots == (workspace,)
