"""App-wide settings, kept outside project folders so keys never travel with a project."""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import llm, store, tools

DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5-5"
DEFAULT_MONTHLY_BUDGET_USD = 10.0
# US dollars per million input and output tokens, from Anthropic's published rates.
# They're used to estimate spend, so the Anthropic Console remains the true bill.
ANTHROPIC_PRICES = {
    "claude-opus-5-5": (4.0, 20.0),
    "claude-sonnet-5-5": (2.0, 10.0),
    "claude-haiku-5-5": (0.10, 0.50),
}
DEFAULTS = {
    "script_provider": "claude",
    "groq_key": "",
    "groq_model": DEFAULT_GROQ_MODEL,
    "groq_fallback": True,
    "anthropic_key": "",
    "anthropic_model": DEFAULT_ANTHROPIC_MODEL,
    "anthropic_monthly_budget_usd": DEFAULT_MONTHLY_BUDGET_USD,
}
ENV_KEYS = {"groq_key": "GROQ_API_KEY", "anthropic_key": "ANTHROPIC_API_KEY"}
_lock = threading.RLock()


def is_hosted() -> bool:
    """A hosted server serves many customers, so it must not use a personal Claude plan."""
    return tools.is_hosted()


def _path() -> Path:
    return store.PROJECTS.parent / "settings.json"


def _usage_path() -> Path:
    return store.PROJECTS.parent / "usage.json"


def _saved() -> dict:
    with _lock:
        path = _path()
        saved = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return {**DEFAULTS, **saved}


def key_source(name: str) -> str | None:
    """Say where a key comes from: the server's environment, this PC's settings, or nowhere."""
    if os.environ.get(ENV_KEYS[name], "").strip():
        return "env"
    # A hosted server only trusts its environment, so an old saved key can't linger in use.
    return "saved" if not is_hosted() and _saved()[name] else None


def load() -> dict:
    """Return settings with the key that should actually be used for each service."""
    values = _saved()
    for name, variable in ENV_KEYS.items():
        source = key_source(name)
        values[name] = os.environ[variable].strip() if source == "env" else (values[name] if source else "")
    # A hosted server can't use the personal Claude plan, so it starts on a service with a key.
    if is_hosted() and values["script_provider"] == "claude":
        if values["anthropic_key"]:
            values["script_provider"] = "claude_api"
        elif values["groq_key"]:
            values["script_provider"] = "groq"
    return values


def _write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    temp.replace(path)


def update(**changes) -> dict:
    with _lock:
        # Built from the saved file, so keys from the environment are never written to disk.
        saved = {**_saved(), **changes}
        _write_json(_path(), saved)
        merged = load()
        return merged


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def month_usage() -> dict:
    with _lock:
        path = _usage_path()
        usage = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
    return usage.get(_month(), {"usd": 0.0, "requests": 0})


def record_spend(model: str, input_tokens: int, output_tokens: int) -> None:
    input_price, output_price = ANTHROPIC_PRICES.get(model, ANTHROPIC_PRICES[DEFAULT_ANTHROPIC_MODEL])
    cost = (input_tokens * input_price + output_tokens * output_price) / 1_000_000
    with _lock:
        path = _usage_path()
        usage = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        month = usage.setdefault(_month(), {"usd": 0.0, "requests": 0})
        month["usd"] += cost
        month["requests"] += 1
        _write_json(path, usage)


def check_budget(values: dict) -> None:
    """Refuse a new Claude API request once this month's estimated spend reaches the cap."""
    budget = values["anthropic_monthly_budget_usd"]
    if month_usage()["usd"] >= budget:
        raise llm.LLMLimitError(
            f"Your ${budget:.2f} monthly Claude API budget has been reached. Raise it in Settings, or use another option.")


def public(values: dict | None = None) -> dict:
    """Settings that are safe to send to the browser. Keys themselves never are."""
    values = values or load()
    usage = month_usage()
    groq_source, anthropic_source = key_source("groq_key"), key_source("anthropic_key")
    return {
        "script_provider": values["script_provider"],
        "groq_model": values["groq_model"],
        "groq_fallback": values["groq_fallback"],
        "has_groq_key": bool(groq_source),
        "groq_key_source": groq_source,
        "groq_key_hint": values["groq_key"][-4:] if groq_source == "saved" else "",
        "has_anthropic_key": bool(anthropic_source),
        "anthropic_key_source": anthropic_source,
        "anthropic_key_hint": values["anthropic_key"][-4:] if anthropic_source == "saved" else "",
        "anthropic_model": values["anthropic_model"],
        "anthropic_monthly_budget_usd": values["anthropic_monthly_budget_usd"],
        "anthropic_spent_usd": round(usage["usd"], 4),
        "anthropic_requests": usage["requests"],
        "hosted": is_hosted(),
    }
