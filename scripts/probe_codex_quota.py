"""Read quota percentages only. Does not start an agent thread or an AI turn."""
import asyncio
import json
from pathlib import Path
from orchestrator.quota import CodexQuotaClient


async def main():
    client = CodexQuotaClient()
    try:
        result = await client.read()
        output = Path(__file__).resolve().parent.parent / "codex-quota-current.json"
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(result, ensure_ascii=True))
    finally:
        await client.close()


asyncio.run(main())
