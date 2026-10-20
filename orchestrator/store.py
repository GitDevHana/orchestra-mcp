from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from .models import RunSpec, TERMINAL
from .redaction import redact


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Store:
    """SQLite is authoritative; JSONL is an export rebuilt from committed events."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.export_lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "runs.sqlite3", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS runs (
              id TEXT PRIMARY KEY, config TEXT NOT NULL, status TEXT NOT NULL,
              created TEXT NOT NULL, updated TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
              parent_id TEXT);
            CREATE TABLE IF NOT EXISTS events (
              seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
              timestamp TEXT NOT NULL, agent TEXT, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_run_seq ON events(run_id,seq);
            CREATE INDEX IF NOT EXISTS events_run_kind_seq ON events(run_id,kind,seq);
            CREATE TABLE IF NOT EXISTS approvals (
              id TEXT PRIMARY KEY, run_id TEXT NOT NULL, agent TEXT, description TEXT NOT NULL,
              reason TEXT NOT NULL, scope TEXT NOT NULL, source TEXT NOT NULL,
              state TEXT NOT NULL, followup_id TEXT);
            CREATE TABLE IF NOT EXISTS artifacts (path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, run_id TEXT NOT NULL);
        """)
        if "argv" not in {r[1] for r in self.db.execute("PRAGMA table_info(approvals)")}:
            self.db.execute("ALTER TABLE approvals ADD COLUMN argv TEXT")
            self.db.commit()

    def create(self, spec: RunSpec, parent: str | None = None) -> str:
        run = uuid.uuid4().hex
        with self.lock, self.db:
            self.db.execute("INSERT INTO runs VALUES (?,?,?,?,?,?,?)",
                            (run, spec.model_dump_json(), "awaiting_approval", now(), now(), "", parent))
        self.event(run, "plan_created", {"spec": spec.model_dump(), "approval_required": True})
        return run

    def state(self, run: str, status: str, reason: str = "") -> None:
        with self.lock, self.db:
            row = self.db.execute("SELECT status FROM runs WHERE id=?", (run,)).fetchone()
            if row is None:
                raise ValueError("Unknown run")
            old = row[0]
            allowed = {"awaiting_approval": {"running", "cancelled"},
                       "running": TERMINAL | {"awaiting_quota_confirmation", "cleaning_up"},
                       "cleaning_up": TERMINAL,
                       "awaiting_quota_confirmation": TERMINAL | {"running", "cleaning_up"}}
            if status != old and status not in allowed.get(old, set()):
                raise ValueError(f"Invalid run state transition: {old} -> {status}")
            self.db.execute("UPDATE runs SET status=?,reason=?,updated=? WHERE id=?", (status, reason, now(), run))
        self.event(run, "state", {"status": status, "reason": reason})

    def event(self, run: str, kind: str, payload: dict, agent: str | None = None) -> int:
        with self.lock, self.db:
            cursor = self.db.execute("INSERT INTO events(run_id,timestamp,agent,kind,payload) VALUES (?,?,?,?,?)",
                                     (run, now(), agent, kind, json.dumps(redact(payload), ensure_ascii=False)))
            return int(cursor.lastrowid)

    def get(self, run: str) -> dict:
        with self.lock:
            row = self.db.execute("SELECT * FROM runs WHERE id=?", (run,)).fetchone()
            if row is None:
                raise ValueError("Unknown run")
            result = dict(row)
            result["config"] = json.loads(result["config"])
            result["approvals"] = [dict(r) for r in self.db.execute("SELECT * FROM approvals WHERE run_id=? ORDER BY rowid", (run,))]
            for approval in result["approvals"]:
                approval["argv"] = json.loads(approval["argv"]) if approval["argv"] else None
            usage = self.db.execute("SELECT agent,payload FROM events WHERE seq IN (SELECT MAX(seq) FROM events WHERE run_id=? AND kind='usage' GROUP BY agent)", (run,)).fetchall()
            result["usage"] = {r["agent"]: json.loads(r["payload"]) for r in usage}
            return result

    def list(self, limit: int = 50) -> list[dict]:
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT id,status,created,updated,reason,parent_id FROM runs ORDER BY created DESC LIMIT ?", (limit,))]

    def events(self, run: str, after: int = 0, limit: int = 500, agent: str | None = None) -> list[dict]:
        with self.lock:
            sql = "SELECT * FROM events WHERE run_id=? AND seq>?"
            params: list = [run, after]
            if agent:
                sql += " AND agent=?"
                params.append(agent)
            rows = self.db.execute(sql + " ORDER BY seq LIMIT ?", (*params, limit)).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in rows]

    def all_events(self, run: str) -> list[dict]:
        result: list[dict] = []
        while page := self.events(run, result[-1]["seq"] if result else 0, 1000):
            result.extend(page)
        return result

    def recent(self, run: str, kinds: tuple[str, ...], limit=100, after=0) -> list[dict]:
        with self.lock:
            placeholders = ",".join("?" for _ in kinds)
            rows = self.db.execute(f"SELECT * FROM events WHERE run_id=? AND kind IN ({placeholders}) AND seq>? ORDER BY seq DESC LIMIT ?",
                                   (run, *kinds, after, limit)).fetchall()
            return [{**dict(r), "payload": json.loads(r["payload"])} for r in reversed(rows)]

    def event_batch(self, run: str, entries: list[tuple[str, dict, str | None]]) -> None:
        with self.lock, self.db:
            self.db.executemany("INSERT INTO events(run_id,timestamp,agent,kind,payload) VALUES (?,?,?,?,?)",
                               [(run, now(), agent, kind, json.dumps(redact(payload), ensure_ascii=False))
                                for kind, payload, agent in entries])

    def register_artifact(self, run: str, path: Path) -> None:
        resolved = path.resolve()
        relative = resolved.relative_to(self.root).as_posix()
        digest = hashlib.sha256(resolved.read_bytes()).hexdigest()
        with self.lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO artifacts VALUES (?,?,?)", (relative, digest, run))

    def artifact_bytes(self, path: Path) -> bytes:
        resolved = path.resolve()
        relative = resolved.relative_to(self.root).as_posix()
        with self.lock:
            row = self.db.execute("SELECT sha256 FROM artifacts WHERE path=?", (relative,)).fetchone()
        if row is None:
            raise ValueError("Not a registered export; export the run again")
        data = resolved.read_bytes()
        if hashlib.sha256(data).hexdigest() != row[0]:
            raise ValueError("Export was modified after generation; refusing to serve it")
        return data

    def defer(self, run: str, agent: str, description: str, reason: str, scope: str, source: str, argv: list[str] | None = None) -> str:
        if argv is not None:
            if not isinstance(argv, list) or not all(isinstance(a, str) for a in argv):
                argv = None
            else:
                try:
                    RunSpec.valid_commands([argv])
                except ValueError:
                    reason += "\n실행 명령을 검토해야 합니다: " + json.dumps(argv, ensure_ascii=False)
                    argv = None
        with self.lock, self.db:
            encoded_argv = json.dumps(argv) if argv else None
            prior = self.db.execute("SELECT id FROM approvals WHERE run_id=? AND agent=? AND description=? AND scope=? AND reason=? AND source=? AND argv IS ? AND state='pending'",
                                    (run, agent, description, scope, reason, source, encoded_argv)).fetchone()
            if prior:
                return prior[0]
            key = uuid.uuid4().hex
            self.db.execute("INSERT INTO approvals(id,run_id,agent,description,reason,scope,source,state,followup_id,argv) VALUES (?,?,?,?,?,?,?,?,?,?)",
                            (key, run, agent, description, reason, scope, source, "pending", None, encoded_argv))
        self.event(run, "approval_deferred", {"id": key, "description": description, "reason": reason, "scope": scope, "source": source, "argv": argv}, agent)
        return key

    def decide(self, run: str, ids: list[str], approve: bool, command_overrides: dict[str, list[str]] | None = None) -> None:
        if not ids:
            raise ValueError("Select at least one item")
        for key, argv in (command_overrides or {}).items():
            if key not in ids or not isinstance(argv, list) or not all(isinstance(arg, str) for arg in argv):
                raise ValueError("Command overrides must belong to selected approval items")
            RunSpec.valid_commands([argv])
        with self.lock, self.db:
            rows = [self.db.execute("SELECT state FROM approvals WHERE id=? AND run_id=?", (key, run)).fetchone() for key in ids]
            if any(row is None or row[0] != "pending" for row in rows):
                raise ValueError("Only pending items in this run may be decided")
            for key in ids:
                if command_overrides and key in command_overrides:
                    self.db.execute("UPDATE approvals SET argv=? WHERE id=?", (json.dumps(command_overrides[key]), key))
                self.db.execute("UPDATE approvals SET state=? WHERE id=?", ("approved" if approve else "rejected", key))
        self.event(run, "approval_decision", {"ids": ids, "approved": approve, "explicit_commands": command_overrides or {}})

    def close(self) -> None:
        self.db.close()
