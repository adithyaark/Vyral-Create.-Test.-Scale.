"""Choose which AI writes the scripts and plans scenes, and switch when a limit is reached."""
from __future__ import annotations

from pathlib import Path

from . import claude_api, groq, llm, scenes, settings


def _with_groq(product: dict, values: dict, job: dict) -> tuple[dict, str]:
    job["message"] = "Groq is writing your brief and scripts"
    copy = groq.generate_copy(product, values["groq_key"], values["groq_model"])
    return llm.check_claims(copy, product), f"Groq ({values['groq_model']})"


def _with_claude_api(product: dict, values: dict, job: dict) -> tuple[dict, str]:
    job["message"] = "Claude is writing your brief and scripts"
    copy = claude_api.generate_copy(product, values)
    return llm.check_claims(copy, product), f"Claude API ({values['anthropic_model']})"


def _with_claude_plan(product: dict, _values: dict, job: dict) -> tuple[dict, str]:
    if settings.is_hosted():
        raise llm.LLMError("The Claude plan isn't available on a hosted server. Choose another script writer in Settings.")
    job["message"] = "Claude is writing your brief and scripts"
    return llm.check_claims(llm.generate_copy(product), product), "Claude"


def write_copy(product: dict, job: dict) -> tuple[dict, str]:
    """Return the checked copy and the name of whichever AI wrote it."""
    values = settings.load()
    provider = values["script_provider"]
    if provider == "groq":
        if not values["groq_key"]:
            raise llm.LLMError("Add your Groq API key in Settings first.")
        return _with_groq(product, values, job)
    if provider == "claude_api" and not values["anthropic_key"]:
        raise llm.LLMError("Add your Anthropic API key in Settings first.")

    write = _with_claude_api if provider == "claude_api" else _with_claude_plan
    try:
        return write(product, values, job)
    except llm.LLMLimitError:
        if not (values["groq_fallback"] and values["groq_key"]):
            raise
    return _with_groq(product, values, job)


def plan_scenes(product_name: str, items: dict[str, str], thumbnails: dict[str, Path]) -> dict:
    """Plan scenes with the API when a key is saved, otherwise with the Claude plan."""
    values = settings.load()
    if values["anthropic_key"]:
        return claude_api.plan_scenes(product_name, items, thumbnails, values)
    if settings.is_hosted():
        raise llm.LLMError("Add an Anthropic API key to plan scenes, or choose Plan automatically.")
    return scenes.plan_scenes(product_name, items, thumbnails)
