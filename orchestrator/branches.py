"""Separate source trees and sealed, hash-checked handoffs. No merge into source."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path, PurePosixPath

EXCLUDED = {".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache", ".archify"}


def linked(path: Path) -> bool:
    # Python 3.11 has no Path.is_junction; Windows exposes the reparse flag.
    import stat
    return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0)
                                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def runtime_directory(path: Path) -> bool:
    """Orchestra records are runtime state, never candidate source code."""
    return (path / "runs.sqlite3").is_file() and (path / "server.lock").is_file()


def inventory(root: Path) -> dict[str, str]:
    files = {}
    for folder, dirs, names in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not linked(Path(folder, d))
                         and not runtime_directory(Path(folder, d)))
        for name in sorted(names):
            path = Path(folder, name)
            if linked(path):
                raise ValueError(f"Linked file not supported: {path}")
            with path.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            files[path.relative_to(root).as_posix()] = digest
    return files


def copy_tree(source: Path, destination: Path, exclude: Path | None = None) -> dict:
    """Never follows junctions/symlinks. Explicitly report omitted dependencies."""
    source, destination = source.resolve(), destination.resolve()
    if runtime_directory(source):
        raise ValueError(f"Choose a source workspace, not an Orchestra records directory: {source}")
    if exclude and source.is_relative_to(exclude.resolve()):
        exclude = None  # A later stage legitimately starts inside the data directory.
    if destination.exists():
        raise ValueError("Refusing to overwrite an existing candidate")
    destination.mkdir(parents=True)
    omitted = []
    count = total = 0
    for folder, dirs, names in os.walk(source, followlinks=False):
        folder = Path(folder)
        kept = []
        for name in sorted(dirs):
            path = folder / name
            if name in EXCLUDED or linked(path) or runtime_directory(path) or (exclude and path.resolve().is_relative_to(exclude)) or destination.is_relative_to(path.resolve()):
                omitted.append(path.relative_to(source).as_posix() + "/")
            else:
                kept.append(name)
        dirs[:] = kept
        target = destination / folder.relative_to(source)
        target.mkdir(parents=True, exist_ok=True)
        for name in sorted(names):
            path = folder / name
            if linked(path):
                omitted.append(path.relative_to(source).as_posix())
                continue
            total += path.stat().st_size
            count += 1
            if count > 50000 or total > 1_000_000_000:
                raise ValueError("Source copy exceeds 50,000 files / 1 GB; choose a smaller workspace")
            try:
                shutil.copy2(path, target / name)
            except OSError as exc:
                raise OSError(f"Cannot copy source file {path}: {exc}") from exc
    return {"files": count, "bytes": total, "omitted": omitted, "path": str(destination)}


def seal(source: Path, destination: Path) -> dict:
    result = copy_tree(source, destination)
    result["hashes"] = inventory(destination)
    return result


def verify(record: dict) -> Path:
    root = Path(record["path"])
    if inventory(root) != record["hashes"]:
        raise ValueError("A preserved candidate changed; refusing to use it as a stage input")
    return root


def selected_path(root: Path, name: str) -> Path:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ":" in normalized or any(p in {"..", ".git"} for p in path.parts) or normalized in {".", ""}:
        raise ValueError("Selected files must be relative paths inside a candidate")
    resolved = (root / normalized).resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise ValueError("Selected path escapes candidate")
    return resolved


def packet(record: dict, baseline: dict, limit=60000) -> str:
    """Bounded code evidence, with explicit omissions; no writable peer path."""
    root = verify(record)
    changes = []
    remaining = limit
    omitted = []
    for name in sorted(baseline.keys() | record["hashes"].keys()):
        if baseline.get(name) == record["hashes"].get(name):
            continue
        path = root / name
        item = {"path": name, "sha256": record["hashes"].get(name), "deleted": not path.exists()}
        if path.exists():
            if path.stat().st_size > remaining:
                omitted.append(name)
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeError:
                omitted.append(name)
                continue
            if len(content) > remaining:
                omitted.append(name)
                continue
            item["content"] = content
            remaining -= len(content)
        changes.append(item)
    return json.dumps({"changed_files": changes, "omitted_files": omitted,
                       "coverage": "Changed UTF-8 sources only, bounded size. No omitted content was reviewed."}, ensure_ascii=False)
