"""Cross-process locks independent of each MCP server's data directory."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path


class WorkspaceLease:
    def __init__(self, workspace: str):
        canonical = os.path.normcase(str(Path(workspace).resolve()))
        identity = str(getattr(os, "getuid", lambda: "user")())
        folder = Path(tempfile.gettempdir()) / ("orchestra-workspace-locks-" + identity)
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / (hashlib.sha256(canonical.encode("utf-8")).hexdigest() + ".lock")
        self.file = self.path.open("a+b")
        try:
            if self.file.seek(0, 2) == 0:
                self.file.write(b"0")
                self.file.flush()
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            raise ValueError("This workspace already has an active run in another manager or process") from exc

    def close(self):
        self.file.close()  # Do not unlink: replacing the inode would break mutual exclusion.
