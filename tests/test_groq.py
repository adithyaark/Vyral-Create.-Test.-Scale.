import json
import os
import tempfile

import httpx
import pytest

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import groq, llm, settings, store, writers  # noqa: E402
from vyral.app import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")
KEY = "gsk_testkey_abcd"
PRODUCT = {"name": "Cup", "description": "A 300 ml ceramic cup.", "price": "$12", "audience": "", "facts": "", "reviews": ""}
REPLY = {
    "brief": {"summary": "s", "pain_points": ["p"], "angles": ["a"], "missing_info": []},
    "hooks": [{"text": "A better morning cup.", "angle": "a"}],
    "scripts": [{"angle": "a", "body": "This 300 ml ceramic cup costs 12 dollars.", "cta": "Buy it.", "flags": []}],
}
MODELS = {"data": [{"id": "openai/gpt-oss-120b"}, {"id": "llama-3.1-8b-instant"}, {"id": "whisper-large-v3"},
                   {"id": "meta-llama/llama-prompt-guard-2-22m"}, {"id": "canopylabs/orpheus-v1-english"}]}


@pytest.fixture(autouse=True)
def clean_settings():
    path = store.PROJECTS.parent / "settings.json"
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


@pytest.fixture
def fake_groq(monkeypatch):
    """Replace Groq with scripted replies and record what Vyral sends."""
    calls = []
    replies = []

    def handler(request):
        calls.append(request)
        if request.url.path.endswith("/models"):
            return replies.pop(0) if replies else httpx.Response(200, json=MODELS)
        reply = replies.pop(0) if replies else chat(REPLY)
        return reply

    def make_client(key):
        return httpx.Client(base_url="https://groq.test/openai/v1", transport=httpx.MockTransport(handler),
                            headers={"Authorization": f"Bearer {key}"})

    monkeypatch.setattr(groq, "_client", make_client)
    return calls, replies


def chat(content):
    text = content if isinstance(content, str) else json.dumps(content)
    return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})


def test_request_uses_rules_json_mode_and_key(fake_groq):
    calls, _ = fake_groq
    copy = groq.generate_copy(PRODUCT, KEY, "openai/gpt-oss-120b")
    assert copy["hooks"][0]["text"] == "A better morning cup."
    body = json.loads(calls[0].content)
    assert calls[0].url.path.endswith("/chat/completions")
    assert calls[0].headers["authorization"] == f"Bearer {KEY}"
    assert body["model"] == "openai/gpt-oss-120b"
    assert body["response_format"] == {"type": "json_object"}
    assert "Never invent statistics" in body["messages"][0]["content"]
    assert "300 ml ceramic cup" in body["messages"][1]["content"]


def test_bad_reply_is_retried_once(fake_groq):
    calls, replies = fake_groq
    replies.append(chat("not json"))
    assert groq.generate_copy(PRODUCT, KEY, "m")["scripts"]
    assert len(calls) == 2


def test_json_validation_failure_is_retried(fake_groq):
    calls, replies = fake_groq
    replies.append(httpx.Response(400, json={"error": {"code": "json_validate_failed"}}))
    assert groq.generate_copy(PRODUCT, KEY, "m")["scripts"]
    assert len(calls) == 2


def test_gives_up_after_two_bad_replies(fake_groq):
    calls, replies = fake_groq
    replies.extend([chat("nope"), chat('{"hooks": []}')])
    with pytest.raises(llm.LLMError, match="expected format"):
        groq.generate_copy(PRODUCT, KEY, "m")
    assert len(calls) == 2


@pytest.mark.parametrize("status,headers,message", [
    (401, {}, "rejected your API key"),
    (404, {}, "doesn't recognize that model"),
    (413, {}, "too big"),
    (429, {"retry-after": "42"}, "about 42 seconds"),
    (429, {}, "rate limit"),
    (400, {}, "error 400"),
])
def test_errors_are_explained_without_retrying(fake_groq, status, headers, message):
    calls, replies = fake_groq
    replies.append(httpx.Response(status, headers=headers, text=f"echo {KEY}"))
    with pytest.raises(llm.LLMError, match=message) as caught:
        groq.generate_copy(PRODUCT, KEY, "m")
    assert KEY not in str(caught.value)
    assert len(calls) == 1


def test_server_errors_retry_then_explain(fake_groq):
    calls, replies = fake_groq
    replies.extend([httpx.Response(503), httpx.Response(503)])
    with pytest.raises(llm.LLMError, match="having a problem"):
        groq.generate_copy(PRODUCT, KEY, "m")
    assert len(calls) == 2


def test_only_writing_models_are_listed(fake_groq):
    assert groq.list_models(KEY) == ["llama-3.1-8b-instant", "openai/gpt-oss-120b"]


def test_invalid_key_is_not_saved(fake_groq):
    _, replies = fake_groq
    replies.append(httpx.Response(401))
    response = client.put("/api/settings/groq-key", json={"key": KEY})
    assert response.status_code == 400
    assert settings.load()["groq_key"] == ""


def test_valid_key_is_saved_selected_and_never_returned(fake_groq):
    response = client.put("/api/settings/groq-key", json={"key": f"  {KEY}\n"})
    assert response.status_code == 200
    assert settings.load()["groq_key"] == KEY
    assert KEY not in response.text
    public = client.get("/api/settings")
    assert KEY not in public.text
    assert public.json()["script_provider"] == "groq"
    assert public.json()["groq_key_hint"] == "abcd"
    assert client.get("/api/settings/groq-models").json()["models"][0] == "llama-3.1-8b-instant"


def test_key_is_not_written_into_projects(fake_groq):
    client.put("/api/settings/groq-key", json={"key": KEY})
    pid = client.post("/api/projects", json={"name": "Test"}).json()["id"]
    assert KEY not in (store.project_dir(pid) / "project.json").read_text(encoding="utf-8")


def test_removing_key_returns_to_claude(fake_groq):
    client.put("/api/settings/groq-key", json={"key": KEY})
    saved = client.delete("/api/settings/groq-key").json()
    assert saved["has_groq_key"] is False and saved["script_provider"] == "claude"
    assert settings.load()["groq_key"] == ""


def test_groq_cannot_be_selected_without_key():
    response = client.put("/api/settings", json={
        "script_provider": "groq", "groq_model": "m", "groq_fallback": True})
    assert response.status_code == 400


def test_other_sites_cannot_change_settings():
    response = client.put("/api/settings/groq-key", json={"key": KEY}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def no_claude(*_args, **_kwargs):
    raise AssertionError("Claude must not be called")


def limit_reached(*_args, **_kwargs):
    raise llm.LLMLimitError(llm.LIMIT_MESSAGE)


def test_groq_setting_skips_claude(fake_groq, monkeypatch):
    monkeypatch.setattr(llm, "generate_copy", no_claude)
    settings.update(groq_key=KEY, script_provider="groq", groq_model="openai/gpt-oss-120b")
    copy, source = writers.write_copy(PRODUCT, {})
    assert source == "Groq (openai/gpt-oss-120b)" and copy["scripts"]


def test_claude_limit_falls_back_to_groq(fake_groq, monkeypatch):
    monkeypatch.setattr(llm, "generate_copy", limit_reached)
    settings.update(groq_key=KEY, script_provider="claude", groq_fallback=True)
    assert writers.write_copy(PRODUCT, {})[1].startswith("Groq")


def test_fallback_can_be_turned_off(fake_groq, monkeypatch):
    calls, _ = fake_groq
    monkeypatch.setattr(llm, "generate_copy", limit_reached)
    settings.update(groq_key=KEY, script_provider="claude", groq_fallback=False)
    with pytest.raises(llm.LLMLimitError):
        writers.write_copy(PRODUCT, {})
    assert calls == []


def test_other_claude_errors_do_not_fall_back(fake_groq, monkeypatch):
    calls, _ = fake_groq

    def broken(*_args, **_kwargs):
        raise llm.LLMError("Something else")

    monkeypatch.setattr(llm, "generate_copy", broken)
    settings.update(groq_key=KEY, script_provider="claude", groq_fallback=True)
    with pytest.raises(llm.LLMError, match="Something else"):
        writers.write_copy(PRODUCT, {})
    assert calls == []


def test_invented_numbers_are_flagged():
    copy = {**REPLY, "scripts": [{**REPLY["scripts"][0], "body": "This 300 ml cup is 97% better.", "flags": ["old"]}]}
    flags = llm.check_claims(copy, PRODUCT)["scripts"][0]["flags"]
    assert flags[0] == "old"
    assert any('"97"' in flag for flag in flags)
    assert not any('"300"' in flag for flag in flags)


def test_unsafe_hooks_are_removed():
    copy = {**REPLY, "hooks": [{"text": "Fine hook.", "angle": ""}, {"text": "97% of people agree.", "angle": ""},
                               {"text": "This cleared my skin.", "angle": ""}, {"text": "A 300 ml cup.", "angle": ""}]}
    assert [hook["text"] for hook in llm.check_claims(copy, PRODUCT)["hooks"]] == ["Fine hook.", "A 300 ml cup."]


def test_all_unsafe_hooks_is_an_error():
    with pytest.raises(llm.LLMError, match="couldn't verify"):
        llm.check_claims({**REPLY, "hooks": [{"text": "97% agree.", "angle": ""}]}, PRODUCT)


def test_testimonial_wording_is_flagged():
    copy = {**REPLY, "scripts": [{**REPLY["scripts"][0], "body": "I love this cup."}]}
    assert any("testimonial" in flag for flag in llm.check_claims(copy, PRODUCT)["scripts"][0]["flags"])


def test_generate_without_key_is_refused_up_front():
    pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
    client.put(f"/api/projects/{pid}/product", json={"name": "T", "description": "A thing."})
    settings.update(script_provider="groq", groq_key="")
    assert client.post(f"/api/projects/{pid}/generate").status_code == 400
