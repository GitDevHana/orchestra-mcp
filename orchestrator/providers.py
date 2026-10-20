from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator
from .models import AgentSpec
from .process import spawn
from .environment import child_environment


def estimate_tokens(text: str) -> int:
    """A visible-text heuristic, never a billing or hidden-reasoning guarantee."""
    return math.ceil(len(text.encode("utf-8")) / 3)


def executable(provider: str) -> list[str] | None:
    names = {"codex": ["codex", "codex.cmd"], "claude": ["claude", "claude.exe", "claude.cmd"],
             "cursor": ["agent", "cursor-agent", "agent.exe"], "gemini": ["gemini", "gemini.cmd"]}[provider]
    npm_entry = {"codex": "@openai/codex/bin/codex.js", "claude": "@anthropic-ai/claude-code/cli.js",
                 "gemini": "@google/gemini-cli/dist/index.js"}
    for name in names:
        found = shutil.which(name)
        if not found:
            continue
        path = Path(found)
        if path.suffix.lower() in {".cmd", ".ps1", ".bat"}:
            entry = path.parent / "node_modules" / npm_entry.get(provider, "__unavailable__")
            node = shutil.which("node")
            if node and entry.is_file():
                return [node, str(entry)]
            continue  # Never pass prompts/model names through cmd.exe string evaluation.
        return [found]
    return None


def doctor() -> dict:
    providers = {}
    for name in ("codex", "cursor", "claude", "gemini"):
        command = executable(name)
        providers[name] = {"installed": command is not None, "command": command,
                           "authentication": "not_probed", "budget": "reported usage when available; otherwise visible-text estimate"}
    return {"python": sys.version.split()[0], "providers": providers,
            "approval_policy": "preapprove workspace scope; deny unattended escalations; collect deferred work",
            "token_limit_guarantee": "best_effort; providers may report usage only after a request completes"}


@dataclass
class Slot:
    key: str
    spec: AgentSpec
    local_session: str
    native_session: str | None = None
    accounted: int = 0
    reported: int = 0
    estimated: int = 0
    guard_count: int = 0
    last_answer: str = ""
    last_verdict: str = "continue"
    assessment: dict = field(default_factory=dict)
    history: list[dict] = field(default_factory=list)


def codex_child_overrides(workspace: str | None = None) -> list[str]:
    """Retain native sandbox configuration, disable nested MCPs without ignoring config.

    Codex 0.163 exec --ignore-user-config resolved to read-only in the live MCP
    trial despite --sandbox workspace-write. Keep user settings and narrow the
    recursion prevention to MCP/plugin activation; never change managed policy.
    """
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    paths = {home / "config.toml"}
    if workspace:
        current = Path(workspace).resolve()
        paths.update(parent / ".codex" / "config.toml" for parent in (current, *current.parents))
    servers = set()
    for path in sorted(paths):
        if not path.is_file():
            continue
        config = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        servers.update(config.get("mcp_servers", {}))
    overrides = ["-c", "features.plugins=false"]
    for key in sorted(servers):
        if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
            raise ValueError("Child Codex MCP isolation needs a simple server name: " + key)
        # Codex -c parses dotted keys itself; quoted segments become literal names.
        overrides += ["-c", f"mcp_servers.{key}.enabled=false"]
    return overrides


def command_for(slot: Slot, access: str, workspace: str | None = None) -> list[str]:
    provider = slot.spec.provider
    prefix = executable(provider)
    if prefix is None:
        raise RuntimeError(f"{provider} CLI is not installed/on PATH; install and log in first")
    if provider == "codex":
        args = ["-a", "never", *codex_child_overrides(workspace), "exec"]
        if slot.native_session:
            # Resume inherits the session's sandbox; no --last, which can mix slots.
            args += ["resume", slot.native_session]
        else:
            args += ["--sandbox", access]
        args += ["--json", "--skip-git-repo-check"]
        if slot.spec.model:
            args += ["--model", slot.spec.model]
        args += ["-"]
    elif provider == "claude":
        args = ["-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages", "--max-turns", "12",
                "--permission-mode", "dontAsk", "--disallowedTools", "mcp__*", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']
        if access == "workspace-write":
            # Explicitly approved workspace edits; commands requiring more permission are denied.
            args += ["--allowedTools", "Read,Glob,Grep,Edit,Write"]
        else:
            args += ["--allowedTools", "Read,Glob,Grep", "--disallowedTools", "Edit,Write,Bash,mcp__*"]
        args += ["--resume", slot.native_session] if slot.native_session else ["--session-id", slot.local_session]
        if slot.spec.model:
            args += ["--model", slot.spec.model]
    elif provider == "gemini":
        args = ["--output-format", "stream-json", "--approval-mode",
                "plan" if access == "read-only" else "auto_edit", "--extensions", "none",
                "--allowed-mcp-server-names", "__orchestra_no_mcp__"]
        if slot.native_session:
            args += ["--resume", slot.native_session]
        if slot.spec.model:
            args += ["--model", slot.spec.model]
    else:
        args = ["-p", "--output-format", "stream-json", "--stream-partial-output"]
        # No --force: it is an all-command permission bypass, not a workspace allowlist.
        # Cursor's installed permission rules govern tools; blocked operations are deferred.
        if access == "read-only":
            args += ["--mode", "ask"]
        if slot.native_session:
            args += ["--resume", slot.native_session]
        if slot.spec.model:
            args += ["--model", slot.spec.model]
    return prefix + args


@dataclass
class StreamState:
    active_message: str | None = None
    usage: dict[str, dict] = field(default_factory=dict)
    streamed_messages: set[str] = field(default_factory=set)
    item_text: dict[str, str] = field(default_factory=dict)
    cursor_partial: bool = False
    item_tokens: dict[str, int] = field(default_factory=dict)

    def meter(self, message: str, usage: dict) -> dict:
        fields = self.usage.setdefault(message, {})
        for key in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            value = usage.get(key)
            if isinstance(value, int) and value >= 0:
                fields[key] = max(fields.get(key, 0), value)
        return {"kind": "meter", "tokens": sum(sum(values.values()) for values in self.usage.values())}


def normalized(provider: str, raw: dict, state: StreamState | None = None) -> list[dict]:
    """Keep original provider events separately; expose a small stable event vocabulary."""
    out = []
    state = state or StreamState()
    kind = raw.get("type", "")
    if kind == "thread.started" and raw.get("thread_id"):
        out.append({"kind": "session", "session_id": raw["thread_id"]})
    if raw.get("session_id"):
        out.append({"kind": "session", "session_id": raw["session_id"]})
    if provider == "codex":
        item = raw.get("item", {})
        if kind in {"item.started", "item.updated", "item.completed"} and item.get("type") != "agent_message":
            visible = {k: v for k, v in item.items() if k not in {"id", "type", "status", "exit_code", "duration_ms"}}
            if visible:
                item_id = item.get("id")
                count = estimate_tokens(json.dumps(visible, ensure_ascii=False))
                # Without an ID we cannot establish that two events belong to
                # the same tool call. Count each event conservatively instead
                # of merging all anonymous calls of one type.
                previous = state.item_tokens.get(str(item_id), 0) if item_id is not None else 0
                if count > previous:
                    out.append({"kind": "token_output", "estimated_tokens": count - previous,
                                "basis": "visible tool/reasoning item; estimate, not billing"})
                    if item_id is not None:
                        state.item_tokens[str(item_id)] = count
        if kind in {"item.updated", "item.completed"} and item.get("type") == "agent_message":
            current = item.get("text", "")
            item_id = item.get("id", "current")
            previous = state.item_text.get(item_id, "")
            delta = current[len(previous):] if current.startswith(previous) else current
            state.item_text[item_id] = current
            if delta:
                out.append({"kind": "text", "text": delta})
        if kind == "turn.completed" and isinstance(raw.get("usage"), dict):
            u = raw["usage"]
            out.append({"kind": "meter", "tokens": int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))})
        if kind in {"turn.failed", "error"}:
            out.append({"kind": "error", "message": raw.get("error", raw.get("message", raw))})
    elif provider == "gemini":
        if kind == "message" and raw.get("role") == "assistant" and isinstance(raw.get("content"), str):
            out.append({"kind": "text", "text": raw["content"]})
        if kind in {"tool_use", "tool_result"}:
            out.append({"kind": "token_output", "estimated_tokens": estimate_tokens(json.dumps(raw, ensure_ascii=False))})
        if kind == "result":
            stats = raw.get("stats", {})
            count = stats.get("total_tokens")
            if isinstance(count, int) and count >= 0:
                out.append({"kind": "meter", "tokens": count})
            if raw.get("status") == "error" or raw.get("error"):
                out.append({"kind": "error", "message": raw.get("error", "Gemini failed")})
        if kind == "error" and raw.get("severity") != "warning":
            out.append({"kind": "error", "message": raw.get("message", raw)})
    else:
        if provider == "claude" and kind == "stream_event":
            inner = raw.get("event", {})
            inner_kind = inner.get("type")
            if inner_kind == "message_start":
                message = inner.get("message", {})
                state.active_message = str(message.get("id", f"message-{len(state.usage)}"))
                if isinstance(message.get("usage"), dict):
                    out.append(state.meter(state.active_message, message["usage"]))
            elif inner_kind == "message_delta" and isinstance(inner.get("usage"), dict):
                out.append(state.meter(state.active_message or "current", inner["usage"]))
            elif inner_kind == "content_block_delta":
                delta = inner.get("delta", {})
                if delta.get("type") == "text_delta":
                    state.streamed_messages.add(state.active_message or "current")
                    out.append({"kind": "text", "text": delta.get("text", "")})
                else:
                    fragment = delta.get("thinking", delta.get("partial_json", ""))
                    if fragment:
                        out.append({"kind": "token_output", "estimated_tokens": estimate_tokens(fragment)})
        if kind == "assistant":
            message = raw.get("message", {})
            content = message.get("content", [])
            text = "".join(c.get("text", "") for c in content if isinstance(c, dict) and c.get("type") == "text")
            duplicate_cursor_flush = False
            if provider == "cursor" and (state.cursor_partial or "timestamp_ms" in raw or "model_call_id" in raw):
                state.cursor_partial = True
                duplicate_cursor_flush = "timestamp_ms" not in raw or "model_call_id" in raw
            if text and message.get("id") not in state.streamed_messages and not duplicate_cursor_flush:
                out.append({"kind": "text", "text": text})
            if provider == "claude" and isinstance(message.get("usage"), dict):
                out.append(state.meter(str(message.get("id", state.active_message or "current")), message["usage"]))
        if kind == "result":
            if isinstance(raw.get("result"), str):
                out.append({"kind": "final_text", "text": raw["result"]})
            usage = raw.get("usage")
            if isinstance(usage, dict):
                if "total_tokens" in usage:
                    count = int(usage["total_tokens"])
                else:
                    count = sum(int(usage.get(k, 0)) for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
                out.append({"kind": "meter", "tokens": count})
            if raw.get("is_error"):
                out.append({"kind": "error", "message": raw.get("result", raw.get("subtype", "CLI error"))})
    for denial in raw.get("permission_denials", []) or []:
        out.append({"kind": "permission", "request": denial, "source": "provider_reported"})
    if kind in {"permission_request", "approval_request", "permission.request", "approval.requested"}:
        out.append({"kind": "permission", "request": raw, "source": "provider_reported", "interactive": True})
    if kind in {"item.completed", "tool_call", "turn.failed", "error", "result"} and not raw.get("permission_denials"):
        rendered = json.dumps(raw, ensure_ascii=False)
        if re.search(r"requires? (?:user )?(?:approval|confirmation)|permission (?:was )?denied|approval[_ -]required|not allowed by.{0,40}(?:permission|policy)", rendered, re.I):
            out.append({"kind": "permission_hint", "request": raw, "source": "provider_output_inferred"})
    # Usage already present in an event is checked before its text is consumed.
    return sorted(out, key=lambda event: event["kind"] != "meter")


class CLIAdapter:
    async def stream(self, slot: Slot, prompt: str, workspace: str, access: str) -> AsyncIterator[dict]:
        argv = command_for(slot, access, workspace)
        env = child_environment(agent=True)
        process, tree = await spawn(argv, workspace, env)
        readers: list[asyncio.Task] = []
        queue: asyncio.Queue = asyncio.Queue(maxsize=256)
        state = StreamState(cursor_partial=slot.spec.provider == "cursor")

        async def pump(stream, channel):
            try:
                while line := await stream.readline():
                    await queue.put((channel, line.decode("utf-8", errors="replace").rstrip("\r\n")))
            except Exception as exc:
                await queue.put(("reader_error", str(exc)))
            finally:
                await queue.put((channel, None))
        try:
            yield {"kind": "process_started", "pid": process.pid, "argv": argv}
            readers = [asyncio.create_task(pump(process.stdout, "stdout")), asyncio.create_task(pump(process.stderr, "stderr"))]
            async def feed():
                try:
                    process.stdin.write(prompt.encode("utf-8"))
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()
            feeder = asyncio.create_task(feed())
            readers.append(feeder)
            ended = 0
            while ended < 2:
                channel, line = await queue.get()
                if line is None:
                    ended += 1
                    continue
                if channel != "stdout":
                    yield {"kind": "stderr" if channel == "stderr" else "error", "text": line}
                    if re.search(r"\[y/n\]|\(y/n\)|approval required|requires? (?:user )?approval", line, re.I):
                        yield {"kind": "permission", "request": {"text": line}, "source": "provider_output_inferred", "interactive": True}
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    yield {"kind": "stdout", "text": line}
                    if re.search(r"\[y/n\]|\(y/n\)|approval required|requires? (?:user )?approval", line, re.I):
                        yield {"kind": "permission", "request": {"text": line}, "source": "provider_output_inferred", "interactive": True}
                    continue
                if not isinstance(value, dict):
                    yield {"kind": "stdout", "value": value}
                    continue
                yield {"kind": "provider_event", "event": value}
                for event in normalized(slot.spec.provider, value, state):
                    yield event
            yield {"kind": "process_exit", "returncode": await process.wait()}
        finally:
            tree.close()
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
            for task in readers:
                task.cancel()
            await asyncio.gather(*readers, return_exceptions=True)
            await process.wait()


class DemoAdapter:
    """Explicit offline demonstration; never used for a real provider run."""
    async def stream(self, slot: Slot, prompt: str, workspace: str, access: str):
        await asyncio.sleep(0.08)
        yield {"kind": "session", "session_id": slot.local_session}
        yield {"kind": "text", "text": json.dumps({"summary": f"[OFFLINE DEMO] {slot.spec.name or slot.key}: 독립 세션과 전달된 검토 요청 확인.", "verdict": "done", "deferred": [{"description": "외부 배포 실행", "reason": "초기 승인 범위 밖", "requested_scope": "외부 서비스 배포"}] if slot.key == "a" else []}, ensure_ascii=False)}
        yield {"kind": "meter", "tokens": estimate_tokens(prompt) + 100}
        yield {"kind": "process_exit", "returncode": 0}
