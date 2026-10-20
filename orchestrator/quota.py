"""Read-only Codex subscription telemetry; independent of local token estimates."""
from __future__ import annotations

import asyncio
import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from .process import spawn
from .environment import child_environment
from .providers import executable


class QuotaLimit(Exception):
    pass


class QuotaUnavailable(Exception):
    pass


def normalize_quota(result: dict) -> dict:
    buckets = result.get("rateLimitsByLimitId")
    if not isinstance(buckets, dict) or not buckets:
        single = result.get("rateLimits")
        buckets = {single.get("limitId") or "codex": single} if isinstance(single, dict) else {}
    windows = []
    for key, bucket in buckets.items():
        if not isinstance(bucket, dict):
            continue
        for position in ("primary", "secondary"):
            window = bucket.get(position)
            if not isinstance(window, dict):
                continue
            used, minutes = window.get("usedPercent"), window.get("windowDurationMins")
            if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(used) or used < 0:
                continue
            if isinstance(minutes, bool) or not isinstance(minutes, (int, float)) or not math.isfinite(minutes) or minutes <= 0:
                continue
            reset = window.get("resetsAt")
            reset = reset if isinstance(reset, (int, float)) and not isinstance(reset, bool) and math.isfinite(reset) else None
            windows.append({"limit_id": str(key), "window_minutes": minutes,
                "window": "five_hour" if minutes == 300 else "weekly" if minutes == 10080 else "other",
                "used_percent": used, "resets_at": reset,
                "position": position, "plan_type": bucket.get("planType"),
                "expired_snapshot": reset is not None and reset <= time.time()})
    return {"status": "available" if windows else "unavailable", "source": "codex app-server account/rateLimits/read",
            "observed_at": datetime.now(timezone.utc).isoformat(), "windows": windows,
            "scope": "account-shared usage, including other Codex sessions; not per-agent tokens"}


def configured(spec) -> bool:
    return any(a.provider == "codex" for a in spec.agents) and (
        spec.codex_five_hour_stop_at_used_percent is not None or spec.codex_weekly_stop_at_used_percent is not None)


def evaluate_quota(snapshot: dict, spec) -> dict:
    missing, reached = [], []
    requirements = [(300, spec.codex_five_hour_stop_at_used_percent), (10080, spec.codex_weekly_stop_at_used_percent)]
    for minutes, threshold in requirements:
        if threshold is None:
            continue
        for limit_id in spec.codex_quota_limit_ids:
            matches = [w for w in snapshot.get("windows", []) if w.get("limit_id") == limit_id
                       and w.get("window_minutes") == minutes and not w.get("expired_snapshot")]
            if not matches:
                missing.append({"limit_id": limit_id, "window_minutes": minutes})
            for window in matches:
                if window["used_percent"] >= threshold:
                    reached.append({**window, "threshold_percent": threshold})
    return {"decision": "stop" if reached else "missing" if missing else "allow", "missing": missing, "reached": reached}


class CodexQuotaClient:
    """One owned app-server connection. Only handshake and rate-limit reads are sent.

    No threads/turns, login, credit resets, or access-token export. Existing CLI
    authentication is reused by the CLI itself.
    """

    def __init__(self):
        self.lock = asyncio.Lock()
        self.process = self.tree = None
        self.readers = []
        self.pending = {}
        self.serial = 0

    async def _send(self, message):
        self.process.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def _request(self, method, params=None):
        self.serial += 1
        key = self.serial
        future = asyncio.get_running_loop().create_future()
        self.pending[key] = future
        try:
            await self._send({"id": key, "method": method, **({"params": params} if params is not None else {})})
            async with asyncio.timeout(15):
                return await future
        finally:
            self.pending.pop(key, None)

    async def _pump(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    value = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if not isinstance(value, dict):
                    continue
                if "method" in value and "id" in value:
                    # An externally-managed auth request cannot be served by this read-only client.
                    await self._send({"id": value["id"], "error": {"code": -32601, "message": "Read-only quota client cannot handle server requests"}})
                    continue
                future = self.pending.get(value.get("id"))
                if future and not future.done():
                    if "error" in value:
                        error = value["error"]
                        future.set_exception(RuntimeError(f"Codex quota RPC error {error.get('code')}: {error.get('message', 'unavailable')}"))
                    else:
                        future.set_result(value.get("result", {}))
        finally:
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(RuntimeError("Codex quota connection closed"))

    async def _drain(self):
        # Do not persist app-server diagnostics, credentials, or account identities.
        while await self.process.stderr.read(16384):
            pass

    async def _start(self):
        prefix = executable("codex")
        if not prefix:
            raise RuntimeError("Codex CLI unavailable")
        self.process, self.tree = await spawn(prefix + ["app-server", "--stdio"], str(Path.home()),
                                              child_environment(agent=True))
        self.readers = [asyncio.create_task(self._pump()), asyncio.create_task(self._drain())]
        await self._request("initialize", {"clientInfo": {"name": "agent_orchestra_quota", "title": "Agent Orchestra quota reader", "version": "0.1.0"}})
        await self._send({"method": "initialized", "params": {}})

    async def read(self) -> dict:
        async with self.lock:
            try:
                if self.process is None or self.process.returncode is not None:
                    await self.close()
                    await self._start()
                result = await self._request("account/rateLimits/read")
                return normalize_quota(result)
            except asyncio.CancelledError:
                await self.close()
                raise
            except Exception as exc:
                await self.close()
                return {"status": "unavailable", "source": "codex app-server account/rateLimits/read", "windows": [],
                        "observed_at": datetime.now(timezone.utc).isoformat(), "error": str(exc)}

    async def close(self):
        if self.tree:
            self.tree.close()
        if self.process and self.process.returncode is None:
            try:
                self.process.kill()
            except ProcessLookupError:
                pass
        for reader in self.readers:
            reader.cancel()
        await asyncio.gather(*self.readers, return_exceptions=True)
        if self.process:
            await self.process.wait()
        self.process = self.tree = None
        self.readers = []
