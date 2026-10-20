import asyncio
import json
import sys
import time

import pytest

from orchestrator.engine import Manager
from orchestrator.models import RunSpec
from orchestrator.quota import CodexQuotaClient, evaluate_quota, normalize_quota
from orchestrator.store import Store


def snapshot(five=20, weekly=10, *, swap=False):
    windows = []
    if five is not None:
        windows.append({"usedPercent": five, "windowDurationMins": 300, "resetsAt": time.time()+10000})
    if weekly is not None:
        windows.append({"usedPercent": weekly, "windowDurationMins": 10080, "resetsAt": time.time()+20000})
    if swap:
        windows.reverse()
    return normalize_quota({"rateLimitsByLimitId": {"codex": {
        "primary": windows[0] if windows else None, "secondary": windows[1] if len(windows)>1 else None}}})


class Agent:
    def __init__(self, delay=0):
        self.delay = delay
        self.calls = 0
        self.closed = False
    async def stream(self, slot, *args):
        self.calls += 1
        try:
            yield {"kind": "session", "session_id": slot.local_session}
            await asyncio.sleep(self.delay)
            yield {"kind": "text", "text": json.dumps({"summary": "finished", "verdict": "done", "deferred": []})}
            yield {"kind": "meter", "tokens": 1000}
            yield {"kind": "process_exit", "returncode": 0}
        finally:
            self.closed = True


@pytest.fixture
def context(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    store = Store(tmp_path / "records")
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"codex": {"installed": True}}})
    yield store, workspace
    store.close()


def spec(workspace, **overrides):
    return RunSpec(task="test", workspace=str(workspace), agents=[{"provider": "codex"}],
                   codex_five_hour_stop_at_used_percent=60, codex_weekly_stop_at_used_percent=70,
                   codex_quota_poll_seconds=1, **overrides)


async def status(store, run, expected):
    async with asyncio.timeout(5):
        while store.get(run)["status"] != expected:
            await asyncio.sleep(.01)


def test_window_duration_not_position_and_no_missing_zero(context):
    _, workspace = context
    config = spec(workspace)
    swapped = snapshot(five=60, weekly=1, swap=True)
    decision = evaluate_quota(swapped, config)
    assert decision["decision"] == "stop"
    assert decision["reached"][0]["window_minutes"] == 300
    missing = evaluate_quota(snapshot(five=None, weekly=1), config)
    assert missing["decision"] == "missing"
    assert missing["missing"] == [{"limit_id": "codex", "window_minutes": 300}]


def test_legacy_snapshot_and_bucket_selection(context):
    _, workspace = context
    result = normalize_quota({"rateLimits": {"primary": {"usedPercent": 71, "windowDurationMins": 10080}}})
    assert evaluate_quota(result, spec(workspace))["reached"][0]["window_minutes"] == 10080
    other = normalize_quota({"rateLimitsByLimitId": {"different-model-bucket": {"primary": {"usedPercent": 10, "windowDurationMins": 300}}}})
    assert evaluate_quota(other, spec(workspace))["decision"] == "missing"


def test_expired_window_and_unavailable_response_stay_unknown(context):
    _, workspace = context
    expired = normalize_quota({"rateLimits": {"primary": {"usedPercent": 1, "windowDurationMins": 300, "resetsAt": 1}}})
    assert evaluate_quota(expired, spec(workspace))["decision"] == "missing"
    assert evaluate_quota({"windows": [], "status": "unavailable"}, spec(workspace))["decision"] == "missing"


def test_example_shape_is_supported_without_fixed_threshold_defaults(context):
    _, workspace = context
    config = RunSpec.model_validate({"task": "test", "workspace": str(workspace), "agents": [
        {"name": "A", "provider": "codex", "max_total_tokens": 80000},
        {"name": "B", "provider": "claude", "max_total_tokens": 80000}],
        "limits": {"codex_five_hour_stop_at_used_percent": 60, "codex_weekly_stop_at_used_percent": 70,
                   "max_runtime_minutes": 120, "max_rounds": 20}, "missing_quota_policy": "pause_for_confirmation"})
    assert config.agents[0].max_tokens == 80000
    assert config.max_seconds == 7200 and config.max_rounds == 20
    assert config.codex_weekly_stop_at_used_percent == 70
    unconfigured = RunSpec(task="test", workspace=str(workspace), agents=[{"provider": "codex"}])
    assert unconfigured.codex_five_hour_stop_at_used_percent is None


async def test_at_threshold_blocks_before_any_agent(context):
    store, workspace = context
    async def read():
        return snapshot(five=60)
    agent = Agent()
    manager = Manager(store, agent, read)
    run = manager.plan(spec(workspace))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 3)
        assert store.get(run)["status"] == "quota_limit"
        assert agent.calls == 0
    finally:
        await manager.shutdown()


async def test_missing_window_pauses_until_explicit_confirmation(context):
    store, workspace = context
    async def read():
        return snapshot(five=None)
    agent = Agent()
    manager = Manager(store, agent, read)
    run = manager.plan(spec(workspace))["id"]
    try:
        await manager.start(run)
        await status(store, run, "awaiting_quota_confirmation")
        assert not agent.calls
        manager.confirm_missing_quota(run, True)
        await asyncio.wait_for(manager.tasks[run], 3)
        assert store.get(run)["status"] == "unverified"
        assert agent.calls == 1
        assert any(e["kind"] == "quota_missing_decision" for e in store.all_events(run))
    finally:
        await manager.shutdown()


async def test_recheck_after_confirmation_never_bypasses_known_limit(context):
    store, workspace = context
    current = [snapshot(five=None, weekly=1)]
    async def read():
        return current[0]
    agent = Agent()
    manager = Manager(store, agent, read)
    run = manager.plan(spec(workspace))["id"]
    try:
        await manager.start(run)
        await status(store, run, "awaiting_quota_confirmation")
        current[0] = snapshot(five=None, weekly=70)
        manager.confirm_missing_quota(run, True)
        await asyncio.wait_for(manager.tasks[run], 3)
        assert store.get(run)["status"] == "quota_limit"
        assert agent.calls == 0
    finally:
        await manager.shutdown()


@pytest.mark.parametrize("policy,expected", [("stop", "quota_unavailable"), ("continue_with_warning", "unverified")])
async def test_missing_policies(context, policy, expected):
    store, workspace = context
    async def read():
        return snapshot(five=None)
    agent = Agent()
    manager = Manager(store, agent, read)
    run = manager.plan(spec(workspace, missing_quota_policy=policy))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 3)
        assert store.get(run)["status"] == expected
        assert agent.calls == (1 if expected == "unverified" else 0)
    finally:
        await manager.shutdown()


async def test_running_poll_stops_active_stream(context):
    store, workspace = context
    reads = 0
    async def read():
        nonlocal reads
        reads += 1
        return snapshot(five=61 if reads >= 3 else 20)
    agent = Agent(delay=120)
    manager = Manager(store, agent, read)
    run = manager.plan(spec(workspace))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 5)
        assert store.get(run)["status"] == "quota_limit"
        assert agent.closed
        assert any(e["kind"] == "quota_turn_interrupted" for e in store.all_events(run))
    finally:
        await manager.shutdown()


async def test_appserver_protocol_only_reads_limits_without_ai_turn(tmp_path, monkeypatch):
    log = tmp_path / "methods.jsonl"
    fake = tmp_path / "app_server.py"
    fake.write_text('''import sys,json
log=sys.argv[1]
for line in sys.stdin:
 m=json.loads(line)
 with open(log,"a") as f: f.write(json.dumps(m)+"\\n")
 if "id" not in m: continue
 result={"userAgent":"fixture"} if m["method"]=="initialize" else {"rateLimits":{"primary":{"usedPercent":38,"windowDurationMins":300}}}
 print(json.dumps({"id":m["id"],"result":result}),flush=True)
''', encoding="utf-8")
    monkeypatch.setattr("orchestrator.quota.executable", lambda p: [sys.executable, str(fake), str(log)])
    monkeypatch.setenv("ORCHESTRA_UNRELATED_SECRET", "test-value")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "login-config"))
    from orchestrator import quota as quota_module
    original_spawn = quota_module.spawn
    environments = []
    async def filtered_spawn(argv, workspace, env):
        environments.append(env.copy())
        return await original_spawn(argv, workspace, env)
    monkeypatch.setattr(quota_module, "spawn", filtered_spawn)
    client = CodexQuotaClient()
    try:
        result = await client.read()
        assert result["windows"][0]["used_percent"] == 38
        result = await client.read()
        assert result["status"] == "available"
    finally:
        await client.close()
    methods = [json.loads(line)["method"] for line in log.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read", "account/rateLimits/read"]
    assert len(environments) == 1
    assert "ORCHESTRA_UNRELATED_SECRET" not in environments[0]
    assert environments[0]["CODEX_HOME"] == str(tmp_path / "login-config")
