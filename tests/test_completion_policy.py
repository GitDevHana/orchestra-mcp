import asyncio
import json
import sys
from pathlib import Path

import pytest

from orchestrator.completion import assess
from orchestrator.engine import Manager
from orchestrator.models import NextStageSpec, ResumeSpec, RunSpec
from orchestrator.store import Store


def answer(status="pass", blockers=None, deferred=None):
    return {"summary": "checked", "verdict": "done",
            "assessment": {"status": status, "blockers": blockers or [],
                           "evidence": ["Inspected fixture source and regression result"], "criteria": []},
            "deferred": deferred or []}


class Adapter:
    def __init__(self, respond):
        self.respond = respond
        self.calls = []

    async def stream(self, slot, prompt, workspace, access):
        call = {"key": slot.key, "prompt": prompt, "path": Path(workspace), "access": access}
        self.calls.append(call)
        value = self.respond(call, self.calls)
        yield {"kind": "text", "text": json.dumps(value)}
        yield {"kind": "process_exit", "returncode": 0}


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"claude": {"installed": True}}})
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "test_unchanged.py").write_text("# existing tests must reach the reviewer\n")
    store = Store(tmp_path / "data")
    yield store, workspace
    store.close()


async def execute(context, adapter, **options):
    store, workspace = context
    manager = Manager(store, adapter)
    config = dict(task="Fix the fixture", workspace=str(workspace),
                  agents=[{"provider": "claude"}, {"provider": "claude"}],
                  max_rounds=3, max_seconds=30, command_backend="trusted_host",
                  validation_commands=[[sys.executable, "-c", "from pathlib import Path; assert Path('test_unchanged.py').is_file()"]])
    config.update(options)
    run = manager.plan(RunSpec(**config))["id"]
    try:
        await manager.start(run)
        await asyncio.wait_for(manager.tasks[run], 20)
        return manager, manager.describe(run)
    finally:
        await manager.shutdown()


async def test_blocker_overrides_done_and_reaches_implementer(context):
    def respond(call, calls):
        if call["key"] == "a":
            if len(calls) > 1:
                assert "session leak" in call["prompt"]
            return answer()
        return answer(blockers=["session leak"]) if len(calls) == 2 else answer()
    adapter = Adapter(respond)
    _, result = await execute(context, adapter)
    assert result["status"] == "completed"
    assert [c["key"] for c in adapter.calls] == ["a", "b", "a", "b"]
    events = context[0].all_events(result["id"])
    first_review = next(e for e in events if e["kind"] == "answer" and e["agent"] == "b")
    assert first_review["payload"]["verdict"] == "continue"


async def test_role_restriction_is_rework_not_permission(context):
    def respond(call, calls):
        if call["key"] == "b" and len(calls) == 2:
            return answer(deferred=[{"kind": "implementation_work", "description": "Fix stale 401",
                                    "reason": "Reviewer is read-only", "requested_scope": "A fixes the code"}])
        if len(calls) == 3:
            assert "Fix stale 401" in call["prompt"]
        return answer()
    adapter = Adapter(respond)
    _, result = await execute(context, adapter)
    assert result["status"] == "completed"
    assert len(adapter.calls) == 4
    assert result["approvals"] == []
    assert any(e["kind"] == "rework_requested" for e in context[0].all_events(result["id"]))


@pytest.mark.parametrize("deferred,status", [
    ([{"description": "Unclassified request", "reason": "unknown"}], "unverified"),
    ([{"kind": "external_permission", "description": "Publish", "reason": "Outside scope"}], "completed"),
])
async def test_external_permission_is_not_executed_or_confused_with_rework(context, deferred, status):
    adapter = Adapter(lambda *_: answer(deferred=deferred))
    _, result = await execute(context, adapter)
    assert result["status"] == status
    assert len(adapter.calls) == 2
    assert all(a["state"] == "pending" for a in result["approvals"])


@pytest.mark.parametrize("legacy,checks", [(True, True), (False, False)])
async def test_missing_assessment_or_commands_never_means_completed(context, legacy, checks):
    adapter = Adapter(lambda *_: {"summary": "done", "verdict": "done"} if legacy else answer())
    options = {} if checks else {"validation_commands": []}
    _, result = await execute(context, adapter, **options)
    assert result["status"] == "unverified"
    assert result["completion"]["status"] == "unverified"
    assert len(adapter.calls) == 2  # Do not spend the hour repeating unavailable checks.


async def test_failed_fixed_command_returns_output_for_repair(context):
    def respond(call, calls):
        if call["key"] == "a":
            if len(calls) > 2:
                assert "NOT_FIXED" in call["prompt"]
            (call["path"] / "state.txt").write_text("fixed" if len(calls) > 2 else "broken")
        return answer()
    command = [sys.executable, "-c",
               "from pathlib import Path; assert Path('state.txt').read_text()=='fixed', 'NOT_FIXED'"]
    adapter = Adapter(respond)
    _, result = await execute(context, adapter, validation_commands=[command])
    assert result["status"] == "completed"
    assert len(adapter.calls) == 4


@pytest.mark.parametrize("report", [
    {},
    {"status": "pass", "evidence": ["looked"], "criteria": []},
    {"status": "pass", "evidence": ["looked"], "criteria": [
        {"criterion": "Keep accounts isolated", "status": "unverifiable", "evidence": "not tested"}]},
])
async def test_completion_criteria_must_have_matching_evidence(context, report):
    def respond(*_):
        return {**answer(), "assessment": report}
    _, result = await execute(context, Adapter(respond), acceptance_criteria=["Keep accounts isolated"])
    assert result["status"] == "unverified"


async def test_satisfied_criteria_with_checks_can_complete(context):
    def respond(*_):
        obj = answer()
        obj["assessment"]["criteria"] = [{"criterion": "Keep accounts isolated", "status": "pass",
                                         "evidence": "Regression test checks delayed response after switching accounts"}]
        return obj
    _, result = await execute(context, Adapter(respond), acceptance_criteria=["Keep accounts isolated"])
    assert result["status"] == "completed"


async def test_dual_rework_targets_only_failed_candidate_with_full_sources(context):
    def respond(call, calls):
        path = call["path"]
        if call["access"] == "workspace-write":
            key = call["key"]
            if (path / "implementation.txt").exists():
                assert key == "a"
                assert "A blocking defect" in call["prompt"]
                (path / "implementation.txt").write_text("a fixed")
            else:
                (path / "implementation.txt").write_text(key + " initial")
            return answer()
        assert (path / "test_unchanged.py").exists()  # Full peer copy, not changed-files packet.
        content = (path / "implementation.txt").read_text()
        assert content.startswith("b" if call["key"] == "a" else "a")
        return answer("needs_fix", ["A blocking defect"]) if content == "a initial" else answer()
    adapter = Adapter(respond)
    manager, result = await execute(context, adapter, mode="dual_implementers")
    assert result["status"] == "awaiting_user_review"
    assert [(c["key"], c["access"]) for c in adapter.calls] == [
        ("a", "workspace-write"), ("b", "workspace-write"),
        ("a", "read-only"), ("b", "read-only"), ("a", "workspace-write"), ("b", "read-only")]
    assert result["comparison"]["round"] == 2
    assert result["comparison"]["rework_candidates"] == []
    for key, candidate in result["comparison"]["candidates"].items():
        assert candidate["completion"]["status"] == "completed"
        assert Path(candidate["preserved"]["path"]).parts[-2:] == ("preserved", key)
    assert not (context[1] / "implementation.txt").exists()
    assert manager.describe(result["id"])["comparison"]["automatic_winner"] is None


@pytest.mark.parametrize("action,rounds", [("wait", 3), ("revise", 1)])
async def test_dual_wait_or_exhausted_rounds_preserve_unresolved_findings(context, action, rounds):
    adapter = Adapter(lambda call, _: answer("needs_fix", ["Not fixed"])
                      if call["access"] == "read-only" else answer())
    _, result = await execute(context, adapter, mode="dual_implementers",
                              dual_feedback_action=action, max_rounds=rounds)
    assert result["status"] == "awaiting_user_review"
    assert result["comparison"]["rework_candidates"] == ["a", "b"]
    assert len(adapter.calls) == 4
    assert all(c["completion"]["status"] == "needs_fix" for c in result["comparison"]["candidates"].values())


def test_conflicting_dual_role_is_rejected_before_any_run(context):
    store, workspace = context
    manager = Manager(store, Adapter(lambda *_: answer()))
    with pytest.raises(ValueError, match="옵션 1"):
        manager.plan(RunSpec(task="Fix code\n- B: 읽기 전용 검토 담당",
            workspace=str(workspace), mode="dual_implementers", agents=[{"provider": "claude"}] * 2))
    assert store.list() == []


async def test_resume_uses_sealed_candidate_and_rejects_drift(context):
    def respond(call, _):
        if call["access"] == "workspace-write":
            (call["path"] / "version.txt").write_text("selected " + call["key"])
        return answer()
    manager, parent = await execute(context, Adapter(respond), mode="dual_implementers")
    run = parent["id"]
    mutable = context[0].root / "runs" / run / "candidates" / "a" / "workspace" / "version.txt"
    mutable.write_text("wrong mutable version")
    child = manager.resume(run, ResumeSpec(candidate="a", max_seconds=30,
        agents=[{"max_tokens": 10000}] * 2, acceptance_criteria=["Preserve version"],
        validation_commands=[[sys.executable, "-V"]]))
    assert Path(child["config"]["workspace"], "version.txt").read_text() == "selected a"
    assert child["source"]["candidate"] == "a"
    assert child["config"]["acceptance_criteria"] == ["Preserve version"]
    assert child["config"]["validation_commands"] == [[sys.executable, "-V"]]
    Path(child["config"]["workspace"], "version.txt").write_text("modified since plan")
    with pytest.raises(ValueError, match="preserved candidate changed"):
        await manager.start(child["id"])
    assert context[0].get(child["id"])["status"] == "awaiting_approval"


async def test_next_stage_accepts_new_checks_and_preserves_input(context):
    manager, parent = await execute(context, Adapter(lambda *_: answer()), mode="dual_implementers")
    child = manager.next_stage(parent["id"], NextStageSpec(
        feedback="Continue from chosen A", base_candidate="a",
        agents=[{"max_tokens": 10000}] * 2, max_seconds=30,
        acceptance_criteria=["Check chosen source"], validation_commands=[[sys.executable, "-V"]],
        command_backend="trusted_host", command_image=None, dual_feedback_action="wait"))
    assert child["source"]["parent_id"] == parent["id"]
    assert child["config"]["dual_feedback_action"] == "wait"
    assert child["config"]["validation_commands"] == [[sys.executable, "-V"]]
    assert child["status"] == "awaiting_approval"


async def test_shared_resume_uses_completed_snapshot_not_later_workspace_changes(context):
    def respond(call, _):
        if call["access"] == "workspace-write":
            (call["path"] / "version.txt").write_text("finished version")
        return answer()
    manager, parent = await execute(context, Adapter(respond))
    assert parent["status"] == "completed"
    (context[1] / "version.txt").write_text("later unrelated version")
    child = manager.resume(parent["id"], ResumeSpec(
        max_seconds=30, agents=[{"max_tokens": 10000}] * 2))
    assert Path(child["config"]["workspace"], "version.txt").read_text() == "finished version"


async def test_unresolved_blockers_stop_at_round_limit_without_success(context):
    adapter = Adapter(lambda call, _: answer(blockers=["still broken"])
                      if call["key"] == "b" else answer())
    _, result = await execute(context, adapter, max_rounds=2)
    assert result["status"] == "round_limit"
    assert len(adapter.calls) == 4


def test_invalid_assessment_cannot_claim_pass():
    for value in [None, [], {"status": "pass", "evidence": [""]}, {"status": "pass", "evidence": []}]:
        assert assess({"assessment": value}, [])["status"] == "unverifiable"
