"""Real Codex-only example through the MCP stdio protocol (not an offline demo).

The user authorized a small isolated example. Token/runtime limits are explicit
arguments, recorded in the approved plan; defaults retain the original trial.
--keep-open retains the MCP connection/dashboard for inspection after the run.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(keep_open: bool, max_tokens: int, max_seconds: int):
    root = Path(__file__).resolve().parent.parent
    folder = root / "real-runs" / ("codex-" + datetime.now().strftime("%Y%m%d-%H%M%S"))
    workspace = folder / "workspace"
    workspace.mkdir(parents=True)
    records = folder / "records"
    report_path = root / "real-codex-example.json"
    params = StdioServerParameters(command=sys.executable, args=["-m", "orchestrator", "--data-dir", str(records), "--workspace-root", str(workspace)])
    with (folder / "mcp-stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                catalog = await client.list_tools()
                calls = []
                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    calls.append(name)
                    if result.isError:
                        raise RuntimeError(str(result.content))
                    return result.structuredContent or json.loads(result.content[0].text)

                diagnosis = await call("orchestrator_doctor")
                quota = await call("get_codex_quota")
                print(json.dumps({"stage": "mcp_connected", "tool_count": len(catalog.tools), "dashboard": diagnosis["dashboard"],
                                  "quota": quota, "workspace": str(workspace)}, ensure_ascii=True), flush=True)
                spec = {
                    "task": """실제 MCP 연동 예제로 아주 작은 Python 모듈을 구현하세요.
이 작업 폴더 안에 다음 두 파일만 새로 작성하세요.
1. clamp.py: clamp(value, minimum, maximum) 함수를 구현합니다. 값이 범위 밖이면 가장 가까운 경계를 반환하고 범위 안이면 그대로 반환합니다. minimum > maximum이면 ValueError를 발생시킵니다.
2. test_clamp.py: 표준 unittest로 범위 안, 하한 미만, 상한 초과, 잘못된 경계의 네 가지 테스트를 작성합니다.
외부 패키지 설치, 네트워크 작업, 다른 폴더 변경은 하지 마세요.
오케스트레이터가 별도 Python 명령으로 테스트하므로 테스트 실행을 위해 권한을 기다리지 마세요.
실제 파일 작성이 끝나면 summary, verdict=done, deferred=[]의 JSON으로 결과를 보고하세요.
파일 작성이 막히면 원인을 구체적으로 기록하고 보류하세요. 작성하지 않았는데 작성했다고 보고하지 마세요.""",
                    "workspace": str(workspace),
                    "agents": [{"provider": "codex", "name": "Codex 실제 실행", "role": "작은 Python 함수를 구현하고 검토한다", "max_tokens": max_tokens}],
                    "max_seconds": max_seconds, "max_rounds": 1, "access": "workspace-write",
                    "validation_commands": [[sys.executable, "-m", "unittest", "discover", "-v"]],
                    "validation_timeout": 30,
                    "command_backend": "trusted_host",
                }
                plan = await call("plan_task", spec=spec)
                run = plan["id"]
                report = {"run_id": run, "workspace": str(workspace), "records": str(records), "dashboard": plan["dashboard"],
                          "spec": spec, "status": plan["status"], "real_provider": True, "quota_before": quota}
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                (folder / "approved-plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8")
                # Authorization is the user's explicit request to try Codex through MCP.
                await call("approve_and_start", run_id=run)
                print(json.dumps({"stage": "real_codex_started", "run_id": run, "dashboard": plan["dashboard"]}), flush=True)
                after = 0
                async with asyncio.timeout(max_seconds + 60):
                    while True:
                        info = await call("get_task", run_id=run)
                        page = await call("get_events", run_id=run, after_seq=after, limit=100)
                        for event in page["events"]:
                            after = event["seq"]
                            if event["kind"] in {"state", "process_started", "answer", "validation", "error", "token_limit_reached"}:
                                print(json.dumps({"stage": event["kind"], "payload": event["payload"]}, ensure_ascii=True), flush=True)
                        if info["status"] not in {"running", "awaiting_quota_confirmation"}:
                            break
                        await asyncio.sleep(2)
                exported = await call("export_task", run_id=run)
                events = []
                cursor = 0
                while True:
                    page = await call("get_events", run_id=run, after_seq=cursor, limit=500)
                    events.extend(page["events"])
                    if not page["has_more"]:
                        break
                    cursor = page["next_after_seq"]
                report.update({"status": info["status"], "reason": info["reason"], "usage": info["usage"], "approvals": info["approvals"],
                               "export": exported, "files": [p.name for p in workspace.iterdir() if p.is_file()],
                               "validation": [e["payload"] for e in events if e["kind"] == "validation"],
                               "provider_events": sum(e["kind"] == "provider_event" for e in events),
                               "demo_events": sum(e["kind"] == "demo" for e in events), "mcp_calls": sorted(set(calls))})
                if info["status"] == "completed":
                    diagrams = await call("export_task", run_id=run, archify=True)
                    report["archify"] = diagrams.get("archify")
                report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"stage": "result", **{k: report[k] for k in ("run_id", "status", "reason", "dashboard", "workspace", "usage", "files", "validation", "provider_events", "demo_events")},
                                  "report": str(report_path)}, ensure_ascii=True), flush=True)
                if keep_open:
                    print("MCP_CONNECTION_KEPT_OPEN", flush=True)
                    await asyncio.Event().wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep-open", action="store_true")
    parser.add_argument("--max-tokens", type=int, default=8000)
    parser.add_argument("--max-seconds", type=int, default=120)
    args = parser.parse_args()
    try:
        asyncio.run(main(args.keep_open, args.max_tokens, args.max_seconds))
    except KeyboardInterrupt:
        pass
