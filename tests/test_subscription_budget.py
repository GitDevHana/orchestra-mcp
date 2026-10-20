import pytest
from pydantic import ValidationError
from orchestrator.models import RunSpec, AgentSpec
from orchestrator.budget import TokenGuard
from orchestrator.providers import Slot
import asyncio
from orchestrator.engine import Manager
from orchestrator.store import Store
from test_quota import Agent, snapshot

def test_subscription_budget_counts_tokens_without_token_stop(tmp_path):
    spec = RunSpec(task='pong', workspace=str(tmp_path), agents=[{'provider':'codex','max_tokens':None}], codex_five_hour_stop_at_used_percent=40)
    guard = TokenGuard(Slot('a', spec.agents[0], 'local'), 'prompt')
    guard.observe({'kind':'meter', 'tokens':10000001})
    assert not guard.reached
    assert guard.commit()['reported_tokens'] == 10000001

@pytest.mark.parametrize('provider,threshold,policy', [('codex',None,'stop'),('claude',40,'stop'),('codex',40,'continue_with_warning')])
def test_subscription_budget_rejects_unprotected_configuration(tmp_path,provider,threshold,policy):
    with pytest.raises(ValidationError):
        RunSpec(task='pong',workspace=str(tmp_path),agents=[{'provider':provider,'max_tokens':None}],codex_five_hour_stop_at_used_percent=threshold,missing_quota_policy=policy)

def test_ten_percent_reserve_caps_both_windows(tmp_path):
    spec=RunSpec(task='pong',workspace=str(tmp_path),agents=[{'provider':'codex','max_tokens':None}],codex_reserve_percent=10,codex_five_hour_stop_at_used_percent=98)
    assert spec.codex_five_hour_stop_at_used_percent == 90
    assert spec.codex_weekly_stop_at_used_percent == 90

@pytest.mark.parametrize('used,expected,calls', [(20,'unverified',1),(90,'quota_limit',0)])
async def test_early_completion_or_reserve_stop_writes_handoff(tmp_path,monkeypatch,used,expected,calls):
    monkeypatch.setattr('orchestrator.engine.doctor',lambda:{'providers':{'codex':{'installed':True}}})
    store=Store(tmp_path/'records')
    adapter=Agent()
    async def quota():return snapshot(five=used,weekly=10)
    manager=Manager(store,adapter=adapter,quota_reader=quota)
    spec=RunSpec(task='pong',workspace=str(tmp_path),agents=[{'provider':'codex','max_tokens':None}],codex_reserve_percent=10,missing_quota_policy='stop')
    run=manager.plan(spec)['id']
    try:
        await manager.start(run)
        await manager.tasks[run]
        assert store.get(run)['status']==expected
        assert adapter.calls==calls
        text=(store.root/'runs'/run/'HANDOFF.md').read_text(encoding='utf-8')
        assert expected in text and 'pong' in text
    finally:
        await manager.shutdown()
        store.close()
