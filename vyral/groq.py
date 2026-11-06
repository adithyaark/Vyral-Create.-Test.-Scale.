"""Write briefs, hooks and scripts through Groq's OpenAI-style API."""
from __future__ import annotations

import json
import os

import httpx

from . import llm

BASE_URL = os.environ.get("VYRAL_GROQ_URL", "https://api.groq.com/openai/v1")
TIMEOUT_SECONDS = 120
MAX_OUTPUT_TOKENS = 4000
ATTEMPTS = 2
# Audio and safety-filter models can't write scripts, so they aren't offered.
NOT_FOR_WRITING = ("whisper", "orpheus", "guard", "safeguard", "tts", "allam")


def _client(key: str) -> httpx.Client:
    return httpx.Client(
        base_url=BASE_URL,
        timeout=TIMEOUT_SECONDS,
        headers={"Authorization": f"Bearer {key}", "User-Agent": "Vyral/1.0"},
    )


def _error_for(response: httpx.Response) -> llm.LLMError:
    """Explain a failed request. Groq's reply is summarized, and keys are never included."""
    status = response.status_code
    if status in (401, 403):
        return llm.LLMError("Groq rejected your API key. Check it in Settings.")
    if status == 404:
        return llm.LLMError("Groq doesn't recognize that model. Choose another in Settings.")
    if status == 413:
        return llm.LLMError("This request is too big for your Groq limits. Shorten the reviews or description.")
    if status == 429:
        wait = response.headers.get("retry-after")
        timing = f" Try again in about {wait} seconds." if wait else " Try again later."
        return llm.LLMError(f"You've reached a Groq rate limit.{timing}")
    if status >= 500:
        return llm.LLMError("Groq is having a problem right now. Please try again.")
    return llm.LLMError(f"Groq couldn't complete the request (error {status}).")


def list_models(key: str) -> list[str]:
    """Return the writing models this key can use. This also confirms the key works."""
    try:
        with _client(key) as client:
            response = client.get("/models")
    except httpx.HTTPError as exc:
        raise llm.LLMError("Couldn't reach Groq. Check your internet connection.") from exc
    if response.status_code != 200:
        raise _error_for(response)
    try:
        ids = [item["id"] for item in response.json()["data"]]
    except (ValueError, KeyError, TypeError) as exc:
        raise llm.LLMError("Groq sent a reply Vyral couldn't read.") from exc
    return sorted(model for model in ids if not any(word in model for word in NOT_FOR_WRITING))


def generate_copy(product: dict, key: str, model: str) -> dict:
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": llm.SYSTEM_PROMPT},
            {"role": "user", "content": llm.build_prompt(product)},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": MAX_OUTPUT_TOKENS,
    }
    problem: llm.LLMError | None = None
    with _client(key) as client:
        for _ in range(ATTEMPTS):
            try:
                response = client.post("/chat/completions", json=payload)
            except httpx.TimeoutException:
                problem = llm.LLMError("Groq took too long to respond. Please try again.")
                continue
            except httpx.HTTPError as exc:
                raise llm.LLMError("Couldn't reach Groq. Check your internet connection.") from exc
            # Groq uses 400 when a model's reply wasn't valid JSON, which a retry can fix.
            retryable = response.status_code >= 500 or (
                response.status_code == 400 and "json_validate_failed" in response.text)
            if response.status_code != 200 and not retryable:
                raise _error_for(response)
            if response.status_code != 200:
                problem = _error_for(response)
                continue
            try:
                text = response.json()["choices"][0]["message"]["content"]
                return llm.parse_pasted(text or "")
            except (ValueError, KeyError, IndexError, TypeError):
                problem = llm.LLMError("Groq sent a reply Vyral couldn't read. Please try again.")
            except llm.LLMError:
                problem = llm.LLMError("Groq's reply wasn't in the expected format. Please try again.")
    raise problem or llm.LLMError("Groq couldn't write the scripts. Please try again.")
