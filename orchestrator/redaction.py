"""Best-effort log redaction; never used to alter instructions or executed argv."""
from __future__ import annotations

import re

SECRET_KEYS = {"authorization", "api_key", "apikey", "access_token", "refresh_token", "password", "client_secret"}
PATTERNS = [
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]{8,}"), "Bearer [REDACTED]"),
    (re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,})\b"), "[REDACTED]"),
    (re.compile(r"(?i)\b(api[_-]?key|access_token|refresh_token|password|client_secret)(\s*[=:]\s*)[^\s,;}]+"), r"\1\2[REDACTED]"),
]


def redact(value):
    if isinstance(value, dict):
        return {key: "[REDACTED]" if key.lower() in SECRET_KEYS else redact(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [redact(item) for item in value]
    if isinstance(value, str):
        for pattern, replacement in PATTERNS:
            value = pattern.sub(replacement, value)
        if len(value) > 200000:
            value = value[:200000] + "\n[log field truncated at 200000 characters]"
    return value
