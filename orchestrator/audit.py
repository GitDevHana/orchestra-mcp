from __future__ import annotations
import difflib
import hashlib
import os
from pathlib import Path
from .branches import runtime_directory, linked

EXCLUDED = {"__pycache__", ".pytest_cache", ".archify"}


def snapshot(workspace: str, exclude_root: Path | None = None, *, strict=False) -> dict:
    if exclude_root and Path(workspace).resolve().is_relative_to(exclude_root.resolve()):
        exclude_root = None  # Candidate workspaces legitimately live below the record root.
    files = {}
    skipped = 0
    excluded = set() if strict else EXCLUDED
    errors = []
    def walk_error(exc):
        errors.append(str(exc))
    for root, dirs, names in os.walk(workspace, followlinks=False, onerror=walk_error):
        kept = []
        links = []
        for name in sorted(dirs):
            path = Path(root, name)
            try:
                if name in excluded or runtime_directory(path) or (exclude_root is not None and path.resolve().is_relative_to(exclude_root)):
                    continue
                if linked(path):
                    links.append(name)
                else:
                    kept.append(name)
            except OSError as exc:
                errors.append(str(exc))
        dirs[:] = kept
        for name in sorted(names + links):
            path = Path(root, name)
            try:
                if linked(path):
                    target = os.readlink(path)
                    files[path.relative_to(workspace).as_posix()] = ("link:" + target, None)
                    continue
                with path.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                    stream.seek(0)
                    data = stream.read(128001)
                try:
                    text = data.decode("utf-8") if len(data) <= 128000 and b"\0" not in data else None
                except UnicodeError:
                    text = None  # Binary/non-UTF8 content still receives a hash.
                files[path.relative_to(workspace).as_posix()] = (digest, text)
            except OSError as exc:
                skipped += 1
                errors.append(str(exc))
    if strict and errors:
        raise RuntimeError("Cannot verify a read-only workspace: " + "; ".join(errors[:3]))
    return {"files": files, "skipped": skipped, "errors": errors,
            "excluded_directories": sorted(excluded)}


def strict_snapshot(workspace: str, exclude_root: Path | None = None) -> dict:
    return snapshot(workspace, exclude_root, strict=True)


def changes(before: dict, after: dict) -> dict:
    result = []
    for path in sorted(before["files"].keys() | after["files"].keys()):
        a, b = before["files"].get(path), after["files"].get(path)
        if a and b and a[0] == b[0]:
            continue
        diff = None
        if (a is None or a[1] is not None) and (b is None or b[1] is not None):
            diff = "".join(difflib.unified_diff((a[1] if a else "").splitlines(True), (b[1] if b else "").splitlines(True), fromfile=path, tofile=path))
        result.append({"path": path, "change": "added" if a is None else "deleted" if b is None else "modified",
                       "before_sha256": a[0] if a else None, "after_sha256": b[0] if b else None,
                       "diff": diff})
    return {"files": result, "coverage": {"before_skipped": before["skipped"], "after_skipped": after["skipped"],
            "errors": before.get("errors", []) + after.get("errors", []),
            "excluded_directories": before["excluded_directories"], "limits": "All file sizes/counts hashed; 128KB/text diff; workspace only; links recorded, not followed"}}
