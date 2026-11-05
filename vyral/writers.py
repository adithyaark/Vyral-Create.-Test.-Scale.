"""Choose which AI writes the scripts, and switch if Claude's limit is reached."""
from __future__ import annotations

from . import groq, llm, settings


def _with_groq(product: dict, values: dict, job: dict) -> tuple[dict, str]:
    job["message"] = "Groq is writing your brief and scripts"
    copy = groq.generate_copy(product, values["groq_key"], values["groq_model"])
    return llm.check_claims(copy, product), f"Groq ({values['groq_model']})"


def write_copy(product: dict, job: dict) -> tuple[dict, str]:
    """Return the checked copy and the name of whichever AI wrote it."""
    values = settings.load()
    if values["script_provider"] == "groq":
        if not values["groq_key"]:
            raise llm.LLMError("Add your Groq API key in Settings first.")
        return _with_groq(product, values, job)

    job["message"] = "Claude is writing your brief and scripts"
    try:
        return llm.check_claims(llm.generate_copy(product), product), "Claude"
    except llm.LLMLimitError:
        if not (values["groq_fallback"] and values["groq_key"]):
            raise
    return _with_groq(product, values, job)
