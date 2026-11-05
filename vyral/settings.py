"""App-wide settings, kept outside project folders so keys never travel with a project."""
from __future__ import annotations

import json
import threading
from pathlib import Path

from . import store

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULTS = {
    "script_provider": "claude",
    "groq_key": "",
    "groq_model": DEFAULT_GROQ_MODEL,
    "groq_fallback": True,
}
_lock = threading.RLock()


def _path() -> Path:
    return store.PROJECTS.parent / "settings.json"


def load() -> dict:
    with _lock:
        path = _path()
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **saved}


def update(**changes) -> dict:
    with _lock:
        merged = {**load(), **changes}
        path = _path()
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        temp.replace(path)
        return merged


def public(values: dict | None = None) -> dict:
    """Settings that are safe to send to the browser. The key itself never is."""
    values = values or load()
    key = values["groq_key"]
    return {
        "script_provider": values["script_provider"],
        "groq_model": values["groq_model"],
        "groq_fallback": values["groq_fallback"],
        "has_groq_key": bool(key),
        "groq_key_hint": key[-4:] if key else "",
    }
