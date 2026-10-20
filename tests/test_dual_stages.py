import asyncio
import json
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from orchestrator.branches import inventory
from orchestrator.engine import Manager
from orchestrator.models import NextStageSpec, RunSpec
from orchestrator.store import Store


class Independent:
    def __init__(self, review_tokens=100, expected_source="original"):
        self.calls = []
        self.review_tokens = review_tokens
        self.expected_source = expected_source

    async def stream(self, slot, prompt, workspace, access):
        self.calls.append({"agent": slot.key, "prompt": prompt, "workspace": workspace,
                           "access": access, "native_session": slot.native_session,
                           "local_session": slot.local_session})
        yield {"kind": "session", "session_id": slot.local_session}
        if access == "workspace-write":
            assert Path(workspace, "main.py").read_text() == self.expected_source
            Path(workspace, "main.py").write_text(slot.key)
            Path(workspace, "nested", "part.py").write_text("part-" + slot.key)
            summary = "PRIVATE_IMPLEMENTATION_" + slot.key
        else:
            assert "피드백 전용" in prompt
            assert "상대 코드 자료" in prompt
            summary = "Feedback from " + slot.key
        yield {"kind": "text", "text": json.dumps({"summary": summary, "verdict": "done"})}
        yield {"kind": "meter", "tokens": self.review_tokens if access == "read-only" else 100}
        yield {"kind": "process_exit", "returncode": 0}


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("orchestrator.engine.doctor", lambda: {"providers": {"codex": {"installed": True}}})
    workspace = tmp_path / "source"
    workspace.mkdir()
    (workspace / "main.py").write_text("original")
    (workspace / "nested").mkdir()
    (workspace / "nested" / "part.py").write_text("original-part")
    store = Store(tmp_path / "records")
    yield store, workspace
    store.close()


def spec(workspace, **kw):
    return RunSpec(task="독립 구현 비교", workspace=str(workspace), mode="dual_implementers",
                   agents=[{"provider": "codex"}, {"provider": "codex"}], max_rounds=1, command_backend="trusted_host", **kw)


async def run_pair(store, workspace, adapter=None, **kw):
    adapter = adapter or Independent()
    manager = Manager(store, adapter)
    run = manager.plan(spec(workspace, **kw))["id"]
    await manager.start(run)
    await asyncio.wait_for(manager.tasks[run], 10)
    return manager, manager.describe(run), adapter


def decision(**kw):
    return NextStageSpec(feedback="A의 구조와 B의 오류 처리로 구현", base_candidate="a",
        take_from_a="구조", take_from_b="오류 처리", agents=[{"max_tokens": 9000}, {"max_tokens": 11000}],
        max_seconds=123, **kw)


async def test_independent_builds_frozen_feedback_and_original_untouched(setup):
    store, workspace = setup
    original = inventory(workspace)
    manager, result, adapter = await run_pair(store, workspace)
    assert result["status"] == "awaiting_user_review", result["reason"]
    assert inventory(workspace) == original
    assert [c["agent"] for c in adapter.calls] == ["a", "b", "a", "b"]
    assert len({c["workspace"] for c in adapter.calls}) == 4
    assert "PRIVATE_IMPLEMENTATION_a" not in adapter.calls[1]["prompt"]
    assert "PRIVATE_IMPLEMENTATION_b" in adapter.calls[2]["prompt"]
    assert "PRIVATE_IMPLEMENTATION_a" in adapter.calls[3]["prompt"]
    assert "Feedback from a" not in adapter.calls[3]["prompt"]
    assert all(c["native_session"] is None for c in adapter.calls)
    assert len({c["local_session"] for c in adapter.calls}) == 4
    comparison = result["comparison"]
    assert comparison["feedback_complete"]
    assert comparison["automatic_winner"] is None
    for key in ("a", "b"):
        record = comparison["candidates"][key]
        root = Path(record["preserved"]["path"])
        assert (root / "main.py").read_text() == key
        assert (root / "nested" / "part.py").read_text() == "part-" + key
        assert record["metrics"]["validation_passed"] is None  # Not "100%" without checks.
        assert record["feedback"] == json.dumps({"summary": "Feedback from " + key, "verdict": "done"})
        assert result["usage"][key]["accounted_tokens"] == 200
    assert manager.tasks[result["id"]].done()


async def test_restart_restores_waiting_candidates_feedback_and_no_timer(setup):
    store, workspace = setup
    _, result, _ = await run_pair(store, workspace)
    reopened = Store(store.root)
    try:
        recovered = Manager(reopened)
        recovered.recover()
        restored = recovered.describe(result["id"])
        assert restored["status"] == "awaiting_user_review"
        assert restored["comparison"] == result["comparison"]
        assert recovered.tasks == {}
        assert restored["usage"] == result["usage"]
    finally:
        reopened.close()


async def test_next_stage_new_budgets_explicit_selection_and_no_auto_start(setup):
    store, workspace = setup
    manager, parent, adapter = await run_pair(store, workspace)
    hashes = {key: inventory(Path(c["preserved"]["path"])) for key, c in parent["comparison"]["candidates"].items()}
    child = manager.next_stage(parent["id"], decision(selected_files={"nested/part.py": "b"}))
    assert child["status"] == "awaiting_approval"
    assert child["parent_id"] == parent["id"]
    assert len(adapter.calls) == 4
    assert child["id"] not in manager.tasks
    config = child["config"]
    assert config["mode"] == "implement_review"
    assert [a["max_tokens"] for a in config["agents"]] == [9000, 11000]
    assert config["max_seconds"] == 123
    assert child["usage"] == {}
    new_source = Path(config["workspace"])
    assert (new_source / "main.py").read_text() == "a"
    assert (new_source / "nested" / "part.py").read_text() == "part-b"
    event = next(e for e in store.all_events(child["id"]) if e["kind"] == "stage_decision")
    assert event["payload"]["take_from_a"] == "구조"
    assert event["payload"]["take_from_b"] == "오류 처리"
    references = [e for e in store.all_events(child["id"]) if e["kind"] == "stage_reference"]
    assert {e["payload"]["candidate"] for e in references} == {"a", "b"}
    assert "part-b" in references[1]["payload"]["source_evidence"]
    for key, c in parent["comparison"]["candidates"].items():
        assert inventory(Path(c["preserved"]["path"])) == hashes[key]
    assert (workspace / "main.py").read_text() == "original"


async def test_second_dual_stage_preserves_nested_sources_inside_record_root(setup):
    store, workspace = setup
    manager, parent, _ = await run_pair(store, workspace)
    child = manager.next_stage(parent["id"], decision(mode="dual_implementers", selected_files={"nested/part.py": "b"}))
    manager.adapter = Independent(expected_source="a")
    await manager.start(child["id"])
    await asyncio.wait_for(manager.tasks[child["id"]], 10)
    result = manager.describe(child["id"])
    assert result["status"] == "awaiting_user_review", result["reason"]
    baseline = next(e["payload"] for e in store.all_events(child["id"]) if e["kind"] == "baseline_preserved")
    assert Path(baseline["path"], "nested", "part.py").read_text() == "part-b"
    assert baseline["omitted"] == []
    for key in ("a", "b"):
        preserved = result["comparison"]["candidates"][key]["preserved"]
        assert Path(preserved["path"], "nested", "part.py").is_file()
    assert manager.describe(parent["id"])["status"] == "awaiting_user_review"


@pytest.mark.parametrize("missing", ["agents", "max_seconds"])
def test_next_stage_cannot_inherit_missing_budgets(missing):
    values = decision().model_dump()
    del values[missing]
    with pytest.raises(ValidationError):
        NextStageSpec.model_validate(values)


@pytest.mark.parametrize("name", ["../escape.py", "C:/outside.py", "/outside.py", ".git/config"])
async def test_selected_files_cannot_escape_new_input(setup, name):
    store, workspace = setup
    manager, parent, _ = await run_pair(store, workspace)
    with pytest.raises(ValueError):
        manager.next_stage(parent["id"], decision(selected_files={name: "b"}))


async def test_tampered_preserved_result_is_not_used(setup):
    store, workspace = setup
    manager, parent, _ = await run_pair(store, workspace)
    root = Path(parent["comparison"]["candidates"]["a"]["preserved"]["path"])
    (root / "main.py").write_text("tampered")
    with pytest.raises(ValueError, match="preserved candidate changed"):
        manager.next_stage(parent["id"], decision())


async def test_comparison_keeps_failed_candidate_for_user_without_choosing_winner(setup):
    store, workspace = setup
    check = [sys.executable, "-c", "from pathlib import Path; raise SystemExit(0 if Path('main.py').read_text()=='a' else 2)"]
    _, result, _ = await run_pair(store, workspace, validation_commands=[check])
    assert result["status"] == "awaiting_user_review"
    c = result["comparison"]
    assert c["baseline_metrics"]["validation_passed"] is False
    assert c["candidates"]["a"]["metrics"]["validation_passed"] is True
    assert c["candidates"]["b"]["metrics"]["validation_passed"] is False
    assert c["automatic_winner"] is None
    assert all(c["candidates"][k]["metrics"]["validation_seconds"] >= 0 for k in ("a", "b"))


async def test_feedback_token_limit_stops_pair_and_keeps_both_candidates(setup):
    store, workspace = setup
    _, result, adapter = await run_pair(store, workspace, Independent(review_tokens=50000))
    assert result["status"] == "token_limit"
    assert len(adapter.calls) == 3  # B feedback never launches.
    assert result["comparison"]["feedback_complete"] is False
    assert result["usage"]["a"]["accounted_tokens"] == 50100
    for key in ("a", "b"):
        assert Path(result["comparison"]["candidates"][key]["preserved"]["path"], "main.py").read_text() == key


async def test_cross_candidate_write_detected_and_not_adopted(setup):
    store, workspace = setup
    class Bad(Independent):
        async def stream(self, slot, prompt, workspace, access):
            if slot.key == "b":
                (Path(workspace).parents[1] / "a" / "workspace" / "main.py").write_text("bad")
            async for event in super().stream(slot, prompt, workspace, access):
                yield event
    _, result, _ = await run_pair(store, workspace, Bad())
    assert result["status"] == "failed"
    assert "another candidate" in result["reason"]
    assert result["comparison"] is None
    checkpoint = next(e for e in store.all_events(result["id"]) if e["kind"] == "candidate_checkpoint")
    assert Path(checkpoint["payload"]["path"], "main.py").read_text() == "a"
    assert (workspace / "main.py").read_text() == "original"


@pytest.mark.parametrize('mode', ['implement_review', 'dual_implementers'])
async def test_stage_shares_full_sources_without_strengths_and_assigns_roles(setup, mode):
    store, workspace = setup
    (workspace / 'unchanged.txt').write_text('shared unchanged source')
    manager, parent, _ = await run_pair(store, workspace)
    choice = decision(mode=mode).model_copy(update={'take_from_a': '', 'take_from_b': ''})
    child = manager.next_stage(parent['id'], choice)
    refs = [e['payload'] for e in store.all_events(child['id']) if e['kind'] == 'stage_reference']
    assert {r['candidate'] for r in refs} == {'a', 'b'}
    for reference in refs:
        root = Path(reference['full_source']['path'])
        assert (root / 'main.py').read_text() == reference['candidate']
        assert (root / 'unchanged.txt').read_text() == 'shared unchanged source'
        assert root != Path(parent['comparison']['candidates'][reference['candidate']]['preserved']['path'])
        assert reference['feedback']
    if mode == 'implement_review':
        assert '검토자' in child['config']['agents'][1]['role']
    else:
        assert all('구현자' in agent['role'] for agent in child['config']['agents'])

    class StageAdapter:
        def __init__(self):
            self.calls = []

        async def stream(self, slot, prompt, workspace, access):
            self.calls.append((slot.key, access, prompt))
            for reference in refs:
                assert json.dumps(reference['full_source']['path'], ensure_ascii=False) in prompt
                assert reference['feedback'] in prompt or 'Feedback from ' in prompt
            if mode == 'implement_review' and slot.key == 'b':
                assert access == 'read-only'
                assert '옵션 1의 검토자' in prompt
            yield {'kind': 'text', 'text': json.dumps({'summary': 'stage done', 'verdict': 'done'})}
            yield {'kind': 'meter', 'tokens': 100}
            yield {'kind': 'process_exit', 'returncode': 0}

    adapter = StageAdapter()
    manager.adapter = adapter
    await manager.start(child['id'])
    await asyncio.wait_for(manager.tasks[child['id']], 10)
    result = manager.describe(child['id'])
    assert result['status'] == ('unverified' if mode == 'implement_review' else 'awaiting_user_review'), result['reason']
    assert [c[:2] for c in adapter.calls[:2]] == [('a', 'workspace-write'), ('b', 'read-only' if mode == 'implement_review' else 'workspace-write')]
    for reference in refs:
        assert inventory(Path(reference['full_source']['path'])) == reference['full_source']['hashes']
