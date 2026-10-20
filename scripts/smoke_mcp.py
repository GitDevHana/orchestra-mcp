"""Exercise the real stdio protocol, offline collaboration and approval follow-up."""
import asyncio
import json
import sys
import tempfile
import httpx
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    with tempfile.TemporaryDirectory(prefix="orchestra-smoke-") as data:
        params = StdioServerParameters(command=sys.executable, args=["-m", "orchestrator", "--data-dir", data])
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                tools = await client.list_tools()
                assert len(tools.tools) == 15
                async def call(name, **args):
                    result = await client.call_tool(name, args)
                    assert not result.isError, result
                    if result.structuredContent:
                        return result.structuredContent
                    return json.loads(result.content[0].text)
                planned = await call("plan_offline_demo")
                diagnosis = await call("orchestrator_doctor")
                async with httpx.AsyncClient(trust_env=False) as http:
                    url = diagnosis["dashboard"]
                    assert (await http.get(url)).status_code == 200
                    base = url.split("/?")[0]
                    assert (await http.get(base + "/api/runs")).status_code == 403
                    assert (await http.post(base + "/api/demo?" + url.split("?",1)[1], json={}, headers={"Origin":"https://untrusted.example"})).status_code == 403
                    assert (await http.get(base + "/artifact?" + url.split("?",1)[1] + "&path=../outside.html")).status_code == 400
                run = planned["id"]
                assert planned["status"] == "awaiting_approval"
                await call("approve_and_start", run_id=run)
                for _ in range(100):
                    info = await call("get_task", run_id=run)
                    if info["status"] != "running":
                        break
                    await asyncio.sleep(.05)
                assert info["status"] == "unverified", info
                item = info["approvals"][0]
                await call("decide_deferred", run_id=run, item_ids=[item["id"]], approve=True)
                following = await call("run_approved_items", run_id=run)
                assert following["parent_id"] == run
                await call("stop_task", run_id=following["id"])
                exported = await call("export_task", run_id=run)
                assert Path(exported["transcript"]).is_file()
                pair = await call("plan_offline_demo", mode="dual_implementers")
                await call("approve_and_start", run_id=pair["id"])
                for _ in range(100):
                    pair_info = await call("get_task", run_id=pair["id"])
                    if pair_info["status"] != "running":
                        break
                    await asyncio.sleep(.05)
                assert pair_info["status"] == "awaiting_user_review", pair_info
                assert pair_info["comparison"]["feedback_complete"]
                missing = await client.call_tool("plan_next_stage", {"run_id": pair["id"],
                    "decision": {"feedback": "사용자 선택", "base_candidate": "a"}})
                assert missing.isError  # No implicit inheritance of budgets.
                stage = await call("plan_next_stage", run_id=pair["id"], decision={
                    "feedback": "A 기준으로 B의 오류 처리를 참고", "base_candidate": "a",
                    "take_from_a": "구조", "take_from_b": "오류 처리", "max_seconds": 90,
                    "agents": [{"max_tokens": 12000}, {"max_tokens": 15000}]})
                assert stage["status"] == "awaiting_approval"
                assert stage["parent_id"] == pair["id"]
                assert stage["usage"] == {}
                await call("approve_and_start", run_id=stage["id"])
                for _ in range(100):
                    stage_info = await call("get_task", run_id=stage["id"])
                    if stage_info["status"] != "running":
                        break
                    await asyncio.sleep(.05)
                assert stage_info["status"] == "unverified"
                print(json.dumps({"mcp_tools": len(tools.tools), "offline_status": info["status"],
                                  "independent_agents": len(info["config"]["agents"]), "deferred_followup": True,
                                  "dual_mode": pair_info["status"], "stage2": stage_info["status"],
                                  "fresh_budgets_required": True}, ensure_ascii=True))
        # Restart the actual stdio service against the same persisted records.
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                restored = await client.call_tool("get_task", {"run_id": pair["id"]})
                assert not restored.isError
                restored_info = restored.structuredContent or json.loads(restored.content[0].text)
                assert restored_info["status"] == "awaiting_user_review"
                assert restored_info["comparison"] == pair_info["comparison"]
                print(json.dumps({"mcp_restart_preserves_user_review": True}))


asyncio.run(main())
