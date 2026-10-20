import asyncio
import ctypes
import json
import os
import sys
import time
from pathlib import Path

import pytest

from orchestrator.budget import TokenGuard
from orchestrator.engine import Manager
from orchestrator.models import AgentSpec, RunSpec
from orchestrator.providers import CLIAdapter, Slot, StreamState, normalized
from orchestrator.store import Store


@pytest.mark.parametrize("reported,reached", [(79, False), (80, True), (81, True)])
def test_guard_stops_at_equality_or_excess(reported, reached):
    slot = Slot("a", AgentSpec(provider="codex", max_tokens=100), "local", accounted=20)
    guard = TokenGuard(slot, "p")
    guard.observe({"kind": "meter", "tokens": reported})
    assert guard.reached is reached
    assert guard.usage()["guard_tokens"] == 20 + reported


def test_final_result_cannot_reset_peak_or_double_charge_streamed_text():
    slot = Slot("a", AgentSpec(provider="claude", max_tokens=100), "local")
    guard = TokenGuard(slot, "p")
    guard.observe({"kind": "text", "text": "a" * 300})
    peak = guard.peak
    guard.observe({"kind": "final_text", "text": "short"})
    guard.observe({"kind": "meter", "tokens": 2})
    assert guard.reached and guard.peak == peak
    guard.commit()
    assert slot.guard_count == peak
    assert TokenGuard(slot, "next").reached
    second = TokenGuard(Slot("b", AgentSpec(provider="claude"), "other"), "p")
    second.observe({"kind": "text", "text": "a" * 30})
    first = second.estimated
    second.observe({"kind": "final_text", "text": "a" * 30})
    assert second.estimated == first


def test_partial_claude_usage_is_cumulative_per_message_not_added_repeatedly():
    state = StreamState()
    def stream(event):
        return normalized("claude", {"type": "stream_event", "event": event}, state)
    assert stream({"type": "message_start", "message": {"id": "one", "usage": {"input_tokens": 100, "cache_read_input_tokens": 50}}})[0]["tokens"] == 150
    assert stream({"type": "message_delta", "usage": {"output_tokens": 10}})[0]["tokens"] == 160
    assert stream({"type": "message_delta", "usage": {"output_tokens": 12}})[0]["tokens"] == 162
    assert stream({"type": "message_delta", "usage": {"output_tokens": 12}})[0]["tokens"] == 162
    stream({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "answer"}})
    final = normalized("claude", {"type": "assistant", "message": {"id": "one", "content": [{"type": "text", "text": "answer"}], "usage": {"input_tokens": 100, "cache_read_input_tokens": 50, "output_tokens": 12}}}, state)
    assert final == [{"kind": "meter", "tokens": 162}]
    assert stream({"type": "message_start", "message": {"id": "two", "usage": {"input_tokens": 20}}})[0]["tokens"] == 182
    assert stream({"type": "message_delta", "usage": {"output_tokens": 5}})[0]["tokens"] == 187


def test_codex_partial_snapshots_do_not_repeat_completed_text():
    state = StreamState()
    parsed = []
    for kind, text in [("item.updated", "hello"), ("item.updated", "hello world"), ("item.completed", "hello world")]:
        parsed += normalized("codex", {"type": kind, "item": {"id": "m1", "type": "agent_message", "text": text}}, state)
    assert "".join(e["text"] for e in parsed) == "hello world"


def test_cursor_partial_output_excludes_buffered_and_final_duplicates():
    state = StreamState(cursor_partial=True)
    parsed = []
    for metadata, text in [({"timestamp_ms": 1}, "hello "), ({"timestamp_ms": 2}, "world"),
                           ({"timestamp_ms": 3, "model_call_id": "call-one"}, "hello world"), ({}, "hello world")]:
        parsed += normalized("cursor", {"type": "assistant", "message": {"content": [{"type": "text", "text": text}]}, **metadata}, state)
    assert "".join(e["text"] for e in parsed if e["kind"] == "text") == "hello world"


async def test_projected_prompt_rejection_does_not_charge_unspent_tokens(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "records")
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"codex": {"installed": True}}})
    class MustNotStart:
        async def stream(self, *args):
            raise AssertionError("CLI must not launch")
            yield
    manager = Manager(store, MustNotStart())
    run = manager.plan(RunSpec(task="test", workspace=str(workspace), agents=[{"provider": "codex", "max_tokens": 100}]))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 3)
        info = store.get(run)
        assert info["status"] == "token_limit"
        assert info["usage"]["a"]["accounted_tokens"] == 0
        assert info["usage"]["a"]["last_turn_basis"] == "not_started"
        assert info["usage"]["a"]["projected_prompt_tokens"] >= 100
    finally:
        await manager.shutdown()
        store.close()


@pytest.mark.parametrize("tokens", [5000, 5001])
async def test_limit_closes_stream_before_audit_and_skips_next_agent_and_validation(tmp_path, monkeypatch, tokens):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "records")
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"codex": {"installed": True}}})
    class MeterThenHang:
        closed = False
        called_agents = []
        continued_after_limit = False
        async def stream(self, slot, *args):
            self.called_agents.append(slot.key)
            try:
                yield {"kind": "meter", "tokens": tokens}
                self.continued_after_limit = True
                await asyncio.sleep(120)
            finally:
                self.closed = True
    adapter = MeterThenHang()
    from orchestrator import audit
    original = audit.snapshot
    def slow_final_audit(*args):
        if adapter.closed:
            time.sleep(.2)
        return original(*args)
    monkeypatch.setattr(audit, "snapshot", slow_final_audit)
    manager = Manager(store, adapter)
    run = manager.plan(RunSpec(task="test", workspace=str(workspace), agents=[{"provider": "codex", "max_tokens": 5000}, {"provider": "codex"}],
        command_backend="trusted_host", validation_commands=[[sys.executable, "-c", "from pathlib import Path; Path('must-not-run').touch()"]]))["id"]
    try:
        await manager.start(run)
        async with asyncio.timeout(3):
            while store.get(run)["status"] == "running":
                await asyncio.sleep(.005)
        assert adapter.closed
        assert not manager.tasks[run].done()  # State is published before the slow audit.
        await asyncio.wait_for(manager.tasks[run], 3)
        assert store.get(run)["status"] == "token_limit"
        assert adapter.called_agents == ["a"]
        assert not adapter.continued_after_limit
        assert not (workspace / "must-not-run").exists()
        stopped = [e for e in store.all_events(run) if e["kind"] == "token_limit_reached"]
        assert stopped[0]["payload"]["guard_tokens"] == tokens
        assert stopped[0]["payload"]["stream_closed"]
    finally:
        await manager.shutdown()
        store.close()


async def test_reported_limit_kills_real_cli_and_child_process(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    fake = workspace / "fake_meter_cli.py"
    fake.write_text('''import json,subprocess,sys,time
sys.stdin.read()
child=subprocess.Popen([sys.executable,"-c","import time;time.sleep(120)"])
print(json.dumps({"type":"child","pid":child.pid}),flush=True)
print(json.dumps({"type":"turn.completed","usage":{"input_tokens":5000,"output_tokens":0}}),flush=True)
time.sleep(120)
''', encoding="utf-8")
    monkeypatch.setattr("orchestrator.providers.executable", lambda p: [sys.executable, str(fake)])
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"codex": {"installed": True}}})
    store = Store(tmp_path / "records")
    manager = Manager(store, CLIAdapter())
    run = manager.plan(RunSpec(task="test", workspace=str(workspace), agents=[{"provider": "codex", "max_tokens": 5000}]))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 5)
        assert store.get(run)["status"] == "token_limit"
        pid = next(e["payload"]["event"]["pid"] for e in store.all_events(run) if e["kind"] == "provider_event" and e["payload"]["event"].get("type") == "child")
        if os.name == "nt":
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            kernel.OpenProcess.restype = w.HANDLE
            kernel.GetExitCodeProcess.argtypes = [w.HANDLE, ctypes.POINTER(w.DWORD)]
            kernel.CloseHandle.argtypes = [w.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                code = w.DWORD()
                kernel.GetExitCodeProcess(handle, ctypes.byref(code))
                kernel.CloseHandle(handle)
                assert code.value != 259
        else:
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
    finally:
        await manager.shutdown()
        store.close()
