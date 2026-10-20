import asyncio
import json
from pathlib import Path
import pytest
from orchestrator.engine import Manager
from orchestrator.models import RunSpec, ResumeSpec, StageBudget, selected_agent
from orchestrator.providers import Slot, command_for, normalized
from orchestrator.store import Store


class Interrupted:
    def __init__(self, cleanup_delay=0):
        self.calls = []
        self.closed = 0
        self.cleanup_delay = cleanup_delay

    async def stream(self, slot, prompt, workspace, access):
        self.calls.append((access, slot.native_session, slot.spec.model))
        try:
            if access != 'read-only':
                Path(workspace, 'partial.txt').write_text('preserve me')
                yield {'kind': 'session', 'session_id': 'writable-session'}
                yield {'kind': 'text', 'text': 'implementation partial'}
                await asyncio.sleep(30)
            else:
                yield {'kind': 'text', 'text': 'handoff partial'}
                await asyncio.sleep(self.cleanup_delay)
                yield {'kind': 'text', 'text': ' remaining: finish validation'}
        finally:
            self.closed += 1


@pytest.fixture
def context(tmp_path, monkeypatch):
    monkeypatch.setattr('orchestrator.engine.doctor', lambda: {'providers': {p: {'installed': True} for p in ('codex','claude','cursor','gemini')}})
    workspace=tmp_path/'work';workspace.mkdir()
    store=Store(tmp_path/'data')
    yield store,workspace
    store.close()


@pytest.mark.parametrize('dual', [False, True])
async def test_deadline_handoff_resume(context, dual):
    store,workspace=context
    adapter=Interrupted()
    manager=Manager(store,adapter)
    spec=RunSpec(task='finish work',workspace=str(workspace),agents=[{'provider':'claude','model':'sonnet'}]*(2 if dual else 1),
                 max_seconds=1,cleanup_seconds=2,mode='dual_implementers' if dual else 'implement_review')
    run=manager.plan(spec)['id'];await manager.start(run)
    await asyncio.wait_for(manager.tasks[run],8)
    assert store.get(run)['status']=='time_limit'
    assert adapter.calls[1]==('read-only',None,'sonnet')
    assert adapter.closed==2
    handoff=(store.root/'runs'/run/'HANDOFF.md').read_text(encoding='utf-8')
    assert 'remaining: finish validation' in handoff
    assert 'implementation partial' in handoff
    cleanup_prompts = [e['payload']['text'] for e in store.all_events(run)
                       if e['kind'] == 'prompt' and '인수인계만 작성하세요' in e['payload']['text']]
    assert 'partial.txt' in cleanup_prompts[-1]
    result=manager.resume(run,ResumeSpec(max_seconds=30,agents=[{'max_tokens':12000,'model':'opus'}]*(2 if dual else 1),candidate='a' if dual else None))
    assert result['status']=='awaiting_approval'
    assert Path(result['config']['workspace'],'partial.txt').read_text()=='preserve me'
    assert result['config']['agents'][0]['model']=='opus'
    assert result['config']['execution_commands']==[]
    assert result['parent_id']==run
    assert 'implementation partial' in result['config']['task']
    assert 'remaining: finish validation' in result['config']['task']


async def test_cleanup_deadline_retains_partial(context):
    store,workspace=context;adapter=Interrupted(30);manager=Manager(store,adapter)
    run=manager.plan(RunSpec(task='x',workspace=str(workspace),agents=[{'provider':'claude'}],max_seconds=1,cleanup_seconds=.2))['id']
    await manager.start(run);await asyncio.wait_for(manager.tasks[run],5)
    assert store.get(run)['status']=='time_limit'
    assert manager.describe(run)['cleanup']['outcome']=='incomplete'
    assert 'handoff partial' in (store.root/'runs'/run/'HANDOFF.md').read_text(encoding='utf-8')
    assert 'implementation partial' in (store.root/'runs'/run/'HANDOFF.md').read_text(encoding='utf-8')
    assert adapter.closed==2


async def test_user_stop_during_cleanup(context):
    store,workspace=context;adapter=Interrupted(30);manager=Manager(store,adapter)
    run=manager.plan(RunSpec(task='x',workspace=str(workspace),agents=[{'provider':'claude'}],max_seconds=1))['id']
    await manager.start(run)
    async with asyncio.timeout(5):
        while len(adapter.calls)<2:await asyncio.sleep(.02)
    await manager.stop(run)
    assert store.get(run)['status']=='cancelled'
    assert adapter.closed==2
    assert (store.root/'runs'/run/'HANDOFF.md').is_file()


def test_model_selection_and_gemini(monkeypatch):
    monkeypatch.setattr('orchestrator.providers.executable',lambda p:[p])
    monkeypatch.setattr('orchestrator.providers.codex_child_overrides',lambda w:[])
    from orchestrator.models import AgentSpec
    for provider in ('codex','claude','cursor','gemini'):
        slot=Slot('a',AgentSpec(provider=provider,model='custom-1'),'local')
        argv=command_for(slot,'read-only')
        assert argv[argv.index('--model')+1]=='custom-1'
    assert '--approval-mode' in argv and 'plan' in argv and 'none' in argv
    assert normalized('gemini',{'type':'message','role':'assistant','content':'hello'})==[{'kind':'text','text':'hello'}]
    assert normalized('gemini',{'type':'result','stats':{'total_tokens':123}})[0]['tokens']==123
    assert selected_agent({'provider':'codex','model':'old'},StageBudget(provider='claude',max_tokens=1000))['model'] is None


def test_cleanup_cap(context):
    _,workspace=context
    with pytest.raises(ValueError):RunSpec(task='x',workspace=str(workspace),agents=[{'provider':'claude'}],cleanup_seconds=601)


async def test_cleanup_token_cap(context):
    store,workspace=context
    class Expensive(Interrupted):
        async def stream(self,slot,prompt,workspace,access):
            async for event in super().stream(slot,prompt,workspace,access):
                yield event
                if access=='read-only':
                    yield {'kind':'meter','tokens':999999}
    adapter=Expensive();manager=Manager(store,adapter)
    run=manager.plan(RunSpec(task='x',workspace=str(workspace),agents=[{'provider':'claude'}],max_seconds=1,cleanup_seconds=2))['id']
    await manager.start(run);await asyncio.wait_for(manager.tasks[run],5)
    assert store.get(run)['status']=='token_limit'
    assert manager.describe(run)['cleanup']['outcome']=='budget_exhausted'
    assert len(adapter.calls)==2


async def test_cleanup_quota_cap(context,monkeypatch):
    from orchestrator.quota import QuotaLimit
    store,workspace=context;adapter=Interrupted();manager=Manager(store,adapter)
    original=manager._quota_controlled_turn
    async def controlled(*args,**kw):
        if kw.get('phase')=='handoff':raise QuotaLimit('account limit')
        return await original(*args,**kw)
    monkeypatch.setattr(manager,'_quota_controlled_turn',controlled)
    run=manager.plan(RunSpec(task='x',workspace=str(workspace),agents=[{'provider':'claude'}],max_seconds=1,cleanup_seconds=2))['id']
    await manager.start(run);await asyncio.wait_for(manager.tasks[run],5)
    assert manager.describe(run)['cleanup']['outcome']=='budget_exhausted'
    assert len(adapter.calls)==1


@pytest.mark.parametrize('partial', [False, True])
def test_legacy_handoff_retains_work_and_executed_commands(context, partial):
    from orchestrator.visualize import export_run
    store, workspace = context
    manager = Manager(store)
    run = manager.plan(RunSpec(task='continue safely', workspace=str(workspace),
                              agents=[{'provider': 'claude'}]))['id']
    store.state(run, 'running')
    store.event(run, 'answer', {'text': 'implementation: migration already applied'}, 'a')
    store.event(run, 'approved_execution', {'argv': ['python', 'migrate.py'], 'returncode': 0})
    store.state(run, 'cleaning_up')
    store.event(run, 'cleanup_started', {'max_seconds': 600})
    # Backward compatibility: old completed answer events did not contain phase.
    store.event(run, 'partial_answer' if partial else 'answer',
                {'text': 'cleanup: validation still pending', **({'phase': 'handoff'} if partial else {})}, 'a')
    store.state(run, 'time_limit')
    handoff = Path(export_run(store, run)['handoff']).read_text(encoding='utf-8')
    assert 'implementation: migration already applied' in handoff
    assert 'cleanup: validation still pending' in handoff
    assert 'migrate.py' in handoff
    resumed = manager.resume(run, ResumeSpec(max_seconds=30, agents=[{'max_tokens': 12000}]))
    assert 'migrate.py' in resumed['config']['task']
    assert resumed['config']['execution_commands'] == []


@pytest.mark.parametrize('agent_count', [1, 2])
async def test_resume_option_one_to_two_and_back(context, agent_count):
    store, workspace = context
    (workspace / 'existing.txt').write_text('finished option one work')
    manager = Manager(store)
    parent = manager.plan(RunSpec(task='extend saved work', workspace=str(workspace),
                                 agents=[{'provider':'claude','role':'original role'}]*agent_count), demo=True)['id']
    store.state(parent, 'running')
    store.event(parent, 'answer', {'text':'saved implementation and review'}, 'a')
    store.state(parent, 'completed')
    child = manager.resume(parent, ResumeSpec(mode='dual_implementers', max_rounds=2,
        max_seconds=30, agents=[{'max_tokens':12000,'model':'opus'}, {'max_tokens':14000,'model':'sonnet'}]))
    assert child['config']['mode'] == 'dual_implementers'
    assert child['config']['max_rounds'] == 2
    assert child['status'] == 'awaiting_approval'
    assert child['id'] not in manager.tasks
    assert all('구현자' in a['role'] and '검토자' not in a['role'] for a in child['config']['agents'])
    assert 'saved implementation and review' in child['config']['task']
    assert Path(child['config']['workspace'], 'existing.txt').read_text() == 'finished option one work'
    await manager.start(child['id'])
    await asyncio.wait_for(manager.tasks[child['id']], 10)
    assert manager.describe(child['id'])['status'] == 'awaiting_user_review'
    for key in ('a','b'):
        folder = manager.describe(child['id'])['comparison']['candidates'][key]['preserved']['path']
        assert Path(folder, 'existing.txt').read_text() == 'finished option one work'
    back = manager.resume(child['id'], ResumeSpec(mode='implement_review', candidate='b',
        max_seconds=30, agents=[{'max_tokens':12000}, {'max_tokens':12000}]))
    assert back['config']['mode'] == 'implement_review'
    assert '검토자' in back['config']['agents'][1]['role']
    assert back['status'] == 'awaiting_approval'
    assert (workspace / 'existing.txt').read_text() == 'finished option one work'
    await manager.shutdown()


def test_resume_option_two_requires_two_agents(context):
    store, workspace = context
    manager = Manager(store)
    run = manager.plan(RunSpec(task='x', workspace=str(workspace), agents=[{'provider':'claude'}]))['id']
    store.state(run, 'cancelled')
    with pytest.raises(ValueError, match='two agents'):
        manager.resume(run, ResumeSpec(mode='dual_implementers', max_seconds=30, agents=[{'max_tokens':12000}]))
