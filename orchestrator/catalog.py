"""Model suggestions, never an assertion of account entitlement."""
import json
import os
from pathlib import Path


def model_catalog():
    models = {"codex": [], "claude": ["sonnet", "opus", "fable"], "cursor": [],
              "gemini": ["gemini-4-argon", "gemini-3.1-pro-preview"]}
    source = "unavailable"
    try:
        cache = json.loads((Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))) / "models_cache.json").read_text(encoding="utf-8"))
        models["codex"] = [m["slug"] for m in cache.get("models", [])
                           if m.get("visibility") == "list" and isinstance(m.get("slug"), str)]
        source = "local_cache"
    except (OSError, ValueError, TypeError):
        pass
    return {"models": models, "codex_source": source, "account_access": "not_verified",
            "custom_ids": True, "note": "후보는 계정 이용 권한을 보장하지 않습니다. 빈칸은 CLI 기본 모델입니다."}
