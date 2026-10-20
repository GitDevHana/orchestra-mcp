import asyncio
import json
from pathlib import Path
from orchestrator.engine import Manager
from orchestrator.models import RunSpec
from orchestrator.store import Store
from orchestrator.visualize import export_run, render_archify


async def main():
    root = Path(__file__).resolve().parent.parent
    workspace = root / "demo-data" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    store = Store(root / "demo-data" / "records")
    manager = Manager(store)
    try:
        spec = RunSpec(task="오프라인 데모: 같은 Claude의 독립 세션과 대화, 승인 보류 기록", workspace=str(workspace),
                       agents=[{"provider": "claude", "name": "구현자"}, {"provider": "claude", "name": "검토자", "role": "독립 검토"}], max_rounds=1)
        run = manager.plan(spec, demo=True)["id"]
        await manager.start(run)
        await manager.tasks[run]
        result = export_run(store, run)
        result["run_id"] = run
        result["archify"] = await render_archify(store, run)
        (root / "demo-result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        compact = {**result, "archify": {k: v for k, v in result["archify"].items() if k != "receipts"}}
        print(json.dumps(compact, ensure_ascii=True))
    finally:
        await manager.shutdown()
        store.close()


asyncio.run(main())
