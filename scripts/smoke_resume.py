"""Fresh MCP stdio + optional real Chrome smoke checks, without model calls."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main(output: Path, browser: bool):
    output = output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "source"
    source.mkdir()
    records = output / "records"
    params = StdioServerParameters(command=sys.executable, args=["-m", "orchestrator",
        "--data-dir", str(records), "--workspace-root", str(source)],
        cwd=str(Path(__file__).resolve().parents[1]))
    with (output / "server-stderr.log").open("w", encoding="utf-8") as log:
        async with stdio_client(params, errlog=log) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                async def call(name, **arguments):
                    result = await client.call_tool(name, arguments)
                    if result.isError:
                        raise RuntimeError(str(result.content))
                    return result.structuredContent or json.loads(result.content[0].text)
                doctor = await call("orchestrator_doctor")
                assert doctor["runtime"]["module"] == str(Path(__file__).resolve().parents[1] / "orchestrator" / "server.py")
                assert doctor["runtime"]["workspace_roots"] == [str(source)]
                assert doctor["runtime"]["default_command_backend"] == "container"
                completed = []
                for mode in ("implement_review", "dual_implementers"):
                    plan = await call("plan_offline_demo", mode=mode)
                    run = plan["id"]
                    await call("approve_and_start", run_id=run)
                    async with asyncio.timeout(30):
                        while True:
                            result = await call("get_task", run_id=run)
                            if result["status"] not in {"running", "awaiting_quota_confirmation"}:
                                break
                            await asyncio.sleep(.1)
                    expected = "unverified" if mode == "implement_review" else "awaiting_user_review"
                    assert result["status"] == expected, result.get("reason")
                    export = await call("export_task", run_id=run)
                    completed.append({"mode": mode, "id": run, "status": result["status"]})
                if browser:
                    relative = Path(export["transcript"]).relative_to(records).as_posix()
                    process = await asyncio.create_subprocess_exec("node",
                        str(Path(__file__).with_name("check_resume_browser.mjs")),
                        str(records / "server.json"), relative, str(output),
                        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 90)
                    if process.returncode:
                        raise RuntimeError(stderr.decode("utf-8", "replace")[-4000:])
                    print(stdout.decode("utf-8", "replace").strip())
                evidence = {"runtime": doctor["runtime"], "offline_runs": completed,
                            "browser": browser, "paid_model_calls": 0}
                (output / "mcp-evidence.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
                print(json.dumps({"mcp": "passed", "runs": completed, "paid_model_calls": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    asyncio.run(main(args.evidence_dir, args.browser))
