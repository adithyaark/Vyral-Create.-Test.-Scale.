"""Write scripts and plan scenes through the Claude API, paid for by an API key."""
from __future__ import annotations

import base64
import json
from pathlib import Path

import anthropic

from . import llm, scenes, settings

DEFAULT_MODEL = settings.DEFAULT_ANTHROPIC_MODEL
MAX_TOKENS = 16000
TIMEOUT_SECONDS = 180.0
MAX_RETRIES = 2
EFFORT = "medium"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# Haiku has no server-side fallback, so the parameter is left off for it.
MODELS_WITH_FALLBACK = {"claude-opus-5-5", "claude-sonnet-5-5"}


def _strict(properties: dict) -> dict:
    return {"type": "object", "properties": properties, "required": list(properties), "additionalProperties": False}


_TEXT = {"type": "string"}
_TEXT_LIST = {"type": "array", "items": _TEXT}
COPY_SCHEMA = _strict({
    "brief": _strict({"summary": _TEXT, "pain_points": _TEXT_LIST, "angles": _TEXT_LIST, "missing_info": _TEXT_LIST}),
    "hooks": {"type": "array", "items": _strict({"text": _TEXT, "angle": _TEXT})},
    "scripts": {"type": "array", "items": _strict({"angle": _TEXT, "body": _TEXT, "cta": _TEXT, "flags": _TEXT_LIST})},
})
SCENE_SCHEMA = _strict({
    "scenes": {"type": "array", "items": _strict({
        "item": _TEXT,
        "line": {"type": "integer"},
        "photo": _TEXT,
        "layout": {"type": "string", "enum": list(scenes.LAYOUTS)},
        "motion": {"type": "string", "enum": list(scenes.MOTIONS)},
        "headline": _TEXT,
    })},
})


def _client(key: str) -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=key, timeout=TIMEOUT_SECONDS, max_retries=MAX_RETRIES)


def _explain(exc: anthropic.AnthropicError) -> llm.LLMError:
    """Turn an API failure into a message that's safe to show the user."""
    if isinstance(exc, anthropic.AuthenticationError):
        return llm.LLMError("Anthropic rejected your API key. Check it in Settings.")
    if isinstance(exc, anthropic.PermissionDeniedError):
        return llm.LLMError("Your Anthropic API key doesn't have access to this. Check it in Settings.")
    if isinstance(exc, anthropic.NotFoundError):
        return llm.LLMError("Anthropic doesn't recognize that model. Choose another in Settings.")
    if isinstance(exc, anthropic.RateLimitError):
        wait = exc.response.headers.get("retry-after")
        timing = f" Try again in about {wait} seconds." if wait else " Try again later."
        return llm.LLMError(f"You've reached an Anthropic rate limit.{timing}")
    if isinstance(exc, anthropic.BadRequestError):
        if "credit balance" in exc.message.lower():
            return llm.LLMError("Your Anthropic account is out of credits. Add credits, then try again.")
        return llm.LLMError("Anthropic couldn't accept this request. Shorten the product details and try again.")
    if isinstance(exc, anthropic.APITimeoutError):
        return llm.LLMError("Claude took too long to respond. Please try again.")
    if isinstance(exc, anthropic.APIConnectionError):
        return llm.LLMError("Couldn't reach Anthropic. Check your internet connection.")
    if isinstance(exc, anthropic.APIStatusError):
        if exc.status_code >= 500:
            return llm.LLMError("Anthropic is having a problem right now. Please try again.")
        return llm.LLMError(f"Anthropic couldn't complete the request (error {exc.status_code}).")
    return llm.LLMError("Anthropic couldn't complete the request.")


def check_key(key: str, model: str = DEFAULT_MODEL) -> None:
    """Confirm a key works, using a lookup that doesn't spend any tokens."""
    try:
        _client(key).models.retrieve(model)
    except anthropic.AnthropicError as exc:
        raise _explain(exc) from exc


def _request(values: dict, system: str, content, schema: dict) -> dict:
    """Make one request within the monthly budget and return its parsed JSON."""
    key, model = values["anthropic_key"], values["anthropic_model"]
    settings.check_budget(values)
    options = {"betas": [FALLBACK_BETA], "fallbacks": "default"} if model in MODELS_WITH_FALLBACK else {}
    try:
        response = _client(key).beta.messages.create(
            model=model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": content}],
            output_config={"effort": EFFORT, "format": {"type": "json_schema", "schema": schema}},
            **options,
        )
    except anthropic.AnthropicError as exc:
        raise _explain(exc) from exc

    settings.record_spend(model, response.usage.input_tokens, response.usage.output_tokens)
    if response.stop_reason == "refusal":
        raise llm.LLMError("Claude declined this request. Review the product details, or write the scripts yourself.")
    if response.stop_reason == "max_tokens":
        raise llm.LLMError("Claude's reply was too long and got cut off. Shorten the product details and try again.")
    text = next((block.text for block in response.content if block.type == "text"), "")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise llm.LLMError("Claude's reply wasn't in the expected format. Please try again.") from exc


def generate_copy(product: dict, values: dict) -> dict:
    return llm.validate_copy(_request(values, llm.COPY_RULES, llm.build_prompt(product), COPY_SCHEMA))


def plan_scenes(product_name: str, items: dict[str, str], thumbnails: dict[str, Path], values: dict) -> dict:
    """Plan scenes with the photos attached, keeping the same safeguards as other planners."""
    photo_ids: dict[str, str] = {}
    content: list[dict] = []
    for number, (photo_id, path) in enumerate(thumbnails.items(), 1):
        name = f"p{number}.jpg"
        photo_ids[name] = photo_id
        content.append({"type": "text", "text": f"Photo {name}:"})
        content.append({"type": "image", "source": {
            "type": "base64", "media_type": "image/jpeg",
            "data": base64.standard_b64encode(path.read_bytes()).decode("ascii"),
        }})
    content.append({"type": "text", "text": scenes.ad_data_text(product_name, items)})

    reply = _request(values, scenes.SCENE_RULES, content, SCENE_SCHEMA)
    plans: dict[str, list] = {}
    for scene in reply.get("scenes", []):
        plans.setdefault(scene.get("item"), []).append(scene)
    return scenes.build_plan(items, {"plans": plans}, photo_ids)
