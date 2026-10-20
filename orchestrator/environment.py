"""Child environment allowlists; unrelated credentials are never copied wholesale."""
from __future__ import annotations

import os

BASE = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR",
        "HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
        "PROGRAMDATA", "LANG", "LC_ALL", "USER", "USERNAME", "LOGNAME"}
AGENT = {"CODEX_HOME", "CLAUDE_CONFIG_DIR", "CURSOR_CONFIG_DIR", "XDG_CONFIG_HOME",
         "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY", "SSL_CERT_FILE", "SSL_CERT_DIR",
         "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE"}


def child_environment(*, agent=False) -> dict[str, str]:
    allowed = BASE | (AGENT if agent else set())
    result = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    result.update(ORCH_CHILD="1", NO_COLOR="1", PYTHONUTF8="1", PYTHONIOENCODING="utf-8")
    return result
