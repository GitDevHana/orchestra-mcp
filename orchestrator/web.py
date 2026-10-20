from __future__ import annotations

import asyncio
import concurrent.futures
import json
import mimetypes
import secrets
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from .models import RunSpec, NextStageSpec
from .providers import doctor
from .visualize import export_run, render_archify


class Dashboard:
    def __init__(self, manager, port=0, archify_cli=None):
        self.manager = manager
        self.loop = asyncio.get_running_loop()
        self.token = secrets.token_urlsafe(32)
        self.archify_cli = archify_cli
        self.renders: set[asyncio.Task] = set()
        dashboard = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def respond(self, data, code=200, content_type="application/json; charset=utf-8", *, artifact=False):
                body = json.dumps(data, ensure_ascii=False).encode("utf-8") if isinstance(data, (dict, list)) else data
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Frame-Options", "DENY")
                if artifact and content_type.startswith("text/html"):
                    self.send_header("Content-Security-Policy", "sandbox allow-scripts; default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; img-src data:; connect-src 'none'; form-action 'none'; base-uri 'none'; frame-ancestors 'none'")
                self.end_headers()
                self.wfile.write(body)

            def authorized(self):
                expected_host = f"127.0.0.1:{dashboard.http.server_port}"
                origin = self.headers.get("Origin")
                query = parse_qs(urlsplit(self.path).query)
                token = self.headers.get("Authorization", "").removeprefix("Bearer ") or query.get("token", [""])[0]
                valid = self.headers.get("Host") == expected_host and (not origin or origin == "http://" + expected_host)
                if not valid or not token.isascii() or len(token) > 256 or not secrets.compare_digest(token, dashboard.token):
                    self.respond({"error": "Local dashboard authorization required"}, 403)
                    return False
                return True

            def do_GET(self):
                if not self.authorized():
                    return
                parsed = urlsplit(self.path)
                query = parse_qs(parsed.query)
                try:
                    if parsed.path == "/":
                        page = (Path(__file__).parent / "static" / "dashboard.html").read_bytes()
                        lifecycle = (Path(__file__).parent / "static" / "lifecycle.js").read_bytes()
                        page = page.replace(b"</script></body>", b"\n" + lifecycle + b"\n</script></body>")
                        return self.respond(page, content_type="text/html; charset=utf-8")
                    if parsed.path == "/api/doctor":
                        return self.respond({**doctor(), "allow_any_workspace": manager.allow_any_workspace,
                                             "workspace_roots": [str(p) for p in manager.workspace_roots]})
                    if parsed.path == "/api/models":
                        from .catalog import model_catalog
                        return self.respond(model_catalog())
                    if parsed.path == "/api/runs":
                        return self.respond(manager.store.list())
                    run = query.get("run", [""])[0]
                    if parsed.path == "/api/run":
                        return self.respond(manager.describe(run))
                    if parsed.path == "/api/events":
                        return self.respond(manager.store.events(run, max(0, int(query.get("after", [0])[0])), 500))
                    if parsed.path == "/artifact":
                        path = (manager.store.root / query.get("path", [""])[0]).resolve()
                        path.relative_to(manager.store.root)
                        if path.suffix not in {".html", ".json", ".jsonl", ".md"} or not path.is_file() or path.name == "server.json":
                            raise ValueError("Not an exported artifact")
                        # HTML index links are intended for offline use; direct file links are returned by API.
                        data = manager.store.artifact_bytes(path)
                        return self.respond(data, content_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream", artifact=True)
                    self.respond({"error": "Not found"}, 404)
                except (ValueError, OSError) as exc:
                    self.respond({"error": str(exc)}, 400)

            def do_POST(self):
                if not self.authorized():
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 1 or length > 200000 or "application/json" not in self.headers.get("Content-Type", ""):
                        raise ValueError("Expected a bounded JSON body")
                    body = json.loads(self.rfile.read(length))
                    path = urlsplit(self.path).path
                    future = asyncio.run_coroutine_threadsafe(dashboard.action(path, body), dashboard.loop)
                    self.respond(future.result(timeout=30))
                except concurrent.futures.TimeoutError:
                    self.respond({"error": "Operation still running; inspect run status"}, 202)
                except Exception as exc:
                    self.respond({"error": str(exc)}, 400)

        self.http = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self.http.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.http.server_port}/?token={self.token}"
        manager.base_url = self.url
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    async def action(self, path, body):
        run = body.get("run")
        if path == "/api/open-candidate":
            candidate = body.get("candidate")
            if candidate not in {"a", "b"}:
                raise ValueError("Choose candidate A or B")
            info = self.manager.describe(run)
            if not info.get("comparison"):
                raise ValueError("Preserved candidates are not ready")
            root = self.manager.store.root.resolve()
            preserved = (root / "runs" / run / "preserved" / candidate).resolve()
            preserved.relative_to(root)
            entry = (preserved / "index.html").resolve()
            entry.relative_to(preserved)
            if not entry.is_file():
                raise ValueError("이 구현에는 index.html이 없습니다. 보존 위치에서 실행 방법을 확인하세요.")
            opened = await asyncio.to_thread(webbrowser.open, entry.as_uri(), new=2)
            if not opened:
                raise ValueError("브라우저를 열지 못했습니다. 보존 위치에서 index.html을 직접 열어주세요.")
            return {"status": "opened", "candidate": candidate}
        if path == "/api/quota":
            return await self.manager.get_codex_quota()
        if path == "/api/quota-confirm":
            return self.manager.confirm_missing_quota(run, body["approve"])
        if path == "/api/plan":
            return await asyncio.to_thread(self.manager.plan, RunSpec.model_validate(body["spec"]))
        if path == "/api/next-stage":
            return await asyncio.to_thread(self.manager.next_stage, run, NextStageSpec.model_validate(body["decision"]))
        if path == "/api/resume":
            from .models import ResumeSpec
            return await asyncio.to_thread(self.manager.resume, run, ResumeSpec.model_validate(body["decision"]))
        if path == "/api/demo":
            workspace = self.manager.store.root / "demo-workspace"
            workspace.mkdir(exist_ok=True)
            spec = RunSpec(task="오프라인 데모: 두 독립 세션의 대화와 추가 승인 보류를 시연", workspace=str(workspace), mode=body.get("mode", "implement_review"),
                           agents=[{"provider": "claude", "name": "구현자"}, {"provider": "claude", "name": "검토자", "role": "독립 검토"}], max_rounds=1)
            return self.manager.plan(spec, demo=True)
        if path == "/api/start":
            return await self.manager.start(run)
        if path == "/api/stop":
            return await self.manager.stop(run)
        if path == "/api/message":
            return self.manager.message(run, body["text"], body.get("agent"))
        if path == "/api/decide":
            self.manager.store.decide(run, body["ids"], body["approve"], body.get("command_overrides"))
            return self.manager.describe(run)
        if path == "/api/followup":
            return await self.manager.followup(run, body.get("max_seconds", 300))
        if path == "/api/export":
            return await asyncio.to_thread(export_run, self.manager.store, run)
        if path == "/api/archify":
            async def generate():
                try:
                    result = await render_archify(self.manager.store, run, self.archify_cli)
                    self.manager.store.event(run, "archify_export", result)
                except Exception as exc:
                    self.manager.store.event(run, "archify_export", {"status": "failed", "message": str(exc)})
            task = asyncio.create_task(generate())
            self.renders.add(task)
            task.add_done_callback(self.renders.discard)
            return {"status": "generating", "message": "완료 결과는 실행 기록의 archify_export 이벤트에 표시됩니다."}
        raise ValueError("Unknown action")

    async def close(self):
        for task in list(self.renders):
            task.cancel()
        await asyncio.gather(*self.renders, return_exceptions=True)
        await asyncio.to_thread(self.http.shutdown)
        self.http.server_close()
