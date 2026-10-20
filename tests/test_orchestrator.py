import asyncio
import json
import os
import sys
from pathlib import Path
import pytest
from orchestrator.engine import Manager
from orchestrator.models import RunSpec
from orchestrator.providers import CLIAdapter, Slot, command_for, normalized
from orchestrator.store import Store
from orchestrator.visualize import export_run, candidates


class Scripted:
    def __init__(self, delay=0, answer=None, reported=50):
        self.delay = delay
        self.answer = answer or {"summary": "검증한 결과", "verdict": "done", "deferred": []}
        self.calls = []
        self.closed = False
        self.reported = reported

    async def stream(self, slot, prompt, workspace, access):
        self.calls.append((slot.key, prompt, slot.native_session))
        try:
            await asyncio.sleep(self.delay)
            yield {"kind": "session", "session_id": slot.local_session}
            if access != "read-only":
                Path(workspace, "result.txt").write_text(slot.key, encoding="utf-8")
            yield {"kind": "text", "text": json.dumps(self.answer, ensure_ascii=False)}
            if self.reported is not None:
                yield {"kind": "meter", "tokens": self.reported}
            yield {"kind": "process_exit", "returncode": 0}
        finally:
            self.closed = True


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {p: {"installed": True} for p in ("codex", "claude", "cursor")}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "data")
    yield store, workspace
    store.close()


def spec(workspace, **kw):
    return RunSpec(task="테스트 작업", workspace=str(workspace), agents=[{"provider": "claude"}, {"provider": "claude"}], command_backend="trusted_host", **kw)


async def finish(manager, run):
    await asyncio.wait_for(manager.tasks[run], 10)
    return manager.store.get(run)


async def test_preapproval_is_required_and_same_provider_is_isolated(setup):
    store, workspace = setup
    adapter = Scripted()
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace))["id"]
    assert store.get(run)["status"] == "awaiting_approval"
    assert adapter.calls == []
    await manager.start(run)
    result = await finish(manager, run)
    assert result["status"] == "unverified"  # No registered checks; bare done is insufficient.
    assert len(adapter.calls) == 2
    assert "검증한 결과" in adapter.calls[1][1]  # A's actual answer is sent to B.
    sessions = [e["payload"]["session_id"] for e in store.all_events(run) if e["kind"] == "session"]
    assert len(set(sessions)) == 2
    diffs = [e for e in store.all_events(run) if e["kind"] == "file_changes"]
    assert diffs[0]["payload"]["files"][0]["change"] == "added"
    assert diffs[1]["payload"]["files"] == []  # Reviewer must not edit the implementation.
    with pytest.raises(ValueError):
        await manager.start(run)


async def test_deferred_items_never_run_until_explicit_approval(setup):
    store, workspace = setup
    adapter = Scripted(answer={"summary": "로컬 완료, 배포 보류", "verdict": "done", "deferred": [
        {"description": "배포", "reason": "추가 권한", "requested_scope": "배포 서버"}]})
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace))["id"]
    await manager.start(run)
    info = await finish(manager, run)
    assert len(adapter.calls) == 2
    assert len(info["approvals"]) == 2
    with pytest.raises(ValueError):
        await manager.followup(run)
    store.decide(run, [info["approvals"][0]["id"]], True)
    store.decide(run, [info["approvals"][1]["id"]], False)
    followup = await manager.followup(run)
    await finish(manager, followup["id"])
    assert store.get(followup["id"])["parent_id"] == run
    assert "승인한 보류 작업만" in adapter.calls[2][1]
    assert [a["state"] for a in store.get(run)["approvals"]] == ["dispatched", "rejected"]
    with pytest.raises(ValueError):
        await manager.followup(run)


async def test_time_limit_closes_active_stream(setup):
    store, workspace = setup
    adapter = Scripted(delay=5)
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace, max_seconds=1, cleanup_seconds=0))["id"]
    await manager.start(run)
    assert (await finish(manager, run))["status"] == "time_limit"
    assert adapter.closed


async def test_budget_prevents_start_and_reports_estimate(setup):
    store, workspace = setup
    adapter = Scripted()
    manager = Manager(store, adapter)
    config = spec(workspace).model_dump()
    config["agents"][0]["max_tokens"] = 100
    run = manager.plan(RunSpec.model_validate(config))["id"]
    await manager.start(run)
    assert (await finish(manager, run))["status"] == "token_limit"
    assert not adapter.calls


async def test_midstream_budget_closes_generator(setup):
    store, workspace = setup
    adapter = Scripted(answer={"summary": "x"*40000, "verdict": "done"})
    manager = Manager(store, adapter)
    config = spec(workspace).model_dump()
    config["agents"][0]["max_tokens"] = 5000
    run = manager.plan(RunSpec.model_validate(config))["id"]
    await manager.start(run)
    assert (await finish(manager, run))["status"] == "token_limit"
    assert adapter.closed


async def test_estimates_and_provider_usage_not_double_counted(setup):
    store, workspace = setup
    manager = Manager(store, Scripted(reported=2000))
    run = manager.plan(spec(workspace))["id"]
    await manager.start(run)
    info = await finish(manager, run)
    assert info["usage"]["a"]["accounted_tokens"] == 2000
    assert info["usage"]["a"]["estimated_tokens"] == 0


async def test_validation_failure_does_not_claim_completion(setup):
    store, workspace = setup
    manager = Manager(store, Scripted())
    run = manager.plan(spec(workspace, max_rounds=1, validation_commands=[[sys.executable, "-c", "raise SystemExit(3)"]]))["id"]
    await manager.start(run)
    assert (await finish(manager, run))["status"] == "round_limit"
    assert any(e["kind"] == "validation" and e["payload"]["returncode"] == 3 for e in store.all_events(run))


async def test_stop_and_workspace_edit_collision(setup):
    store, workspace = setup
    manager = Manager(store, Scripted(delay=5))
    first = manager.plan(spec(workspace))["id"]
    second = manager.plan(spec(workspace))["id"]
    await manager.start(first)
    await asyncio.sleep(.02)
    with pytest.raises(ValueError, match="active run"):
        await manager.start(second)
    assert (await manager.stop(first))["status"] == "cancelled"
    assert (await manager.stop(second))["status"] == "cancelled"


def test_restart_marks_interrupted_and_does_not_replay(setup):
    store, workspace = setup
    manager = Manager(store, Scripted())
    run = manager.plan(spec(workspace))["id"]
    store.state(run, "running")
    manager.recover()
    assert store.get(run)["status"] == "interrupted"
    assert not manager.tasks


async def test_interactive_approval_is_deferred_without_waiting(setup):
    store, workspace = setup
    class Interactive(Scripted):
        async def stream(self, slot, prompt, workspace, access):
            self.calls.append(slot.key)
            try:
                yield {"kind": "permission", "request": {"command": "deploy", "prompt": "[y/N]"}, "source": "provider_reported", "interactive": True}
                await asyncio.sleep(120)
            finally:
                self.closed = True
    adapter = Interactive()
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace, max_rounds=1))["id"]
    await manager.start(run)
    info = await finish(manager, run)
    assert adapter.calls == ["a", "b"]
    assert adapter.closed
    assert info["status"] == "round_limit"
    assert len(info["approvals"]) == 2


def test_demo_identity_survives_restart(setup):
    store, workspace = setup
    first = Manager(store)
    run = first.plan(spec(workspace), demo=True)["id"]
    assert run in Manager(store).demo_runs


async def test_immediate_stop_before_coroutine_launch(setup):
    store, workspace = setup
    adapter = Scripted()
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace))["id"]
    await manager.start(run)
    assert (await manager.stop(run))["status"] == "cancelled"
    assert not adapter.calls


def test_provider_event_usage_and_permissions():
    event = {"type": "result", "result": "finished", "usage": {"input_tokens": 10, "output_tokens": 20,
             "cache_read_input_tokens": 30, "cache_creation_input_tokens": 40},
             "permission_denials": [{"tool_name": "Bash", "tool_input": {"command": "deploy"}}]}
    parsed = normalized("claude", event)
    assert next(e for e in parsed if e["kind"] == "meter")["tokens"] == 100
    assert any(e["kind"] == "permission" for e in parsed)
    assert not any(e["kind"] == "meter" for e in normalized("cursor", {"type": "result", "result": "ok"}))


async def test_only_explicitly_approved_command_executes_once(setup):
    store, workspace = setup
    manager = Manager(store, Scripted())
    run = manager.plan(spec(workspace))["id"]
    await manager.start(run)
    await finish(manager, run)
    approved = store.defer(run, "a", "승인 파일 작성", "추가 작업", "workspace 내 marker 파일", "agent_reported")
    denied = store.defer(run, "a", "미승인 파일 작성", "추가 작업", "workspace 내 forbidden 파일", "agent_reported",
                       [sys.executable, "-c", "from pathlib import Path; Path('forbidden').write_text('no')"])
    command = [sys.executable, "-c", "from pathlib import Path; p=Path('marker'); p.write_text(p.read_text()+'x' if p.exists() else 'x')"]
    assert not (workspace / "marker").exists()
    store.decide(run, [approved], True, {approved: command})
    store.decide(run, [denied], False)
    following = await manager.followup(run)
    await finish(manager, following["id"])
    assert (workspace / "marker").read_text() == "x"
    assert not (workspace / "forbidden").exists()
    assert any(e["kind"] == "approved_execution" for e in store.all_events(following["id"]))
    with pytest.raises(ValueError):
        await manager.followup(run)


def test_export_is_complete_and_html_escapes_target_content(setup):
    store, workspace = setup
    run = store.create(spec(workspace))
    for _ in range(1100):
        store.event(run, "stdout", {"text": "</script><img src=x onerror=alert(1)>"})
    output = export_run(store, run)
    assert len(Path(output["events"]).read_text(encoding="utf-8").splitlines()) == 1101
    text = Path(output["transcript"]).read_text(encoding="utf-8")
    assert "</script><img" not in text
    assert "\\u003c/script>" in text
    assert candidates(store, run)[0].is_file()


def test_commands_keep_native_sessions_separate_without_bypasses(monkeypatch):
    monkeypatch.setattr("orchestrator.providers.executable", lambda p: [p])
    a = Slot("a", spec_for_provider("codex"), "local-a")
    first = command_for(a, "workspace-write")
    assert "never" in first and "--sandbox" in first
    a.native_session = "native-a"
    resumed = command_for(a, "workspace-write")
    assert "resume" in resumed and "native-a" in resumed
    assert "--last" not in resumed and not any("bypass" in arg for arg in resumed)
    assert "--force" not in command_for(Slot("b", spec_for_provider("cursor"), "local-b"), "workspace-write")


def spec_for_provider(provider):
    from orchestrator.models import AgentSpec
    return AgentSpec(provider=provider)


def test_codex_keeps_platform_config_but_disables_nested_mcps(tmp_path, monkeypatch):
    from orchestrator.providers import codex_child_overrides
    config = tmp_path / "config.toml"
    config.write_text('[windows]\nsandbox="elevated"\n[mcp_servers.orchestra]\ncommand="python"\n[mcp_servers.rea]\ncommand="node"\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    monkeypatch.setattr("orchestrator.providers.executable", lambda _: ["codex"])
    argv = command_for(Slot("a", spec_for_provider("codex"), "local"), "workspace-write")
    assert "--ignore-user-config" not in argv
    assert "mcp_servers.orchestra.enabled=false" in argv
    assert "mcp_servers.rea.enabled=false" in argv
    assert not any('mcp_servers."' in arg for arg in argv)
    assert "--sandbox" in argv and "workspace-write" in argv and "never" in argv
    config.unlink()
    assert codex_child_overrides() == ["-c", "features.plugins=false"]  # Do not invent invalid transports.


async def test_real_process_stream_uses_stdin_without_shell(setup, monkeypatch):
    store, workspace = setup
    fake = workspace / "fake_cli.py"
    fake.write_text('''import sys,json
text=sys.stdin.read()
print(json.dumps({"type":"thread.started","thread_id":"fake-native"}),flush=True)
print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":text}}),flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":2}}),flush=True)
''', encoding="utf-8")
    monkeypatch.setattr("orchestrator.providers.executable", lambda p: [sys.executable, str(fake)])
    slot = Slot("a", spec_for_provider("codex"), "one")
    literal = '$(touch not-executed) & echo "hello" `whoami`\n한글'
    result = [e async for e in CLIAdapter().stream(slot, literal, str(workspace), "read-only")]
    assert next(e for e in result if e["kind"] == "text")["text"] == literal
    assert result[-1] == {"kind": "process_exit", "returncode": 0}
    assert not (workspace / "not-executed").exists()


async def test_real_process_cancel_kills_descendant(setup, monkeypatch):
    _, workspace = setup
    fake = workspace / "sleep_cli.py"
    fake.write_text('''import subprocess,sys,time,json
child=subprocess.Popen([sys.executable,"-c","import time; time.sleep(120)"])
print(json.dumps({"type":"child","pid":child.pid}),flush=True)
time.sleep(120)
''', encoding="utf-8")
    monkeypatch.setattr("orchestrator.providers.executable", lambda p: [sys.executable, str(fake)])
    stream = CLIAdapter().stream(Slot("a", spec_for_provider("codex"), "one"), "", str(workspace), "read-only")
    child_pid = None
    async with asyncio.timeout(5):
        async for event in stream:
            if event.get("kind") == "provider_event" and event["event"].get("type") == "child":
                child_pid = event["event"]["pid"]
                break
    await stream.aclose()
    assert child_pid
    await asyncio.sleep(.1)
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD,w.BOOL,w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.GetExitCodeProcess.argtypes = [w.HANDLE,ctypes.POINTER(w.DWORD)]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, child_pid)
        if handle:
            code = w.DWORD()
            kernel.GetExitCodeProcess(handle, ctypes.byref(code))
            kernel.CloseHandle(handle)
            assert code.value != 259
    else:
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
