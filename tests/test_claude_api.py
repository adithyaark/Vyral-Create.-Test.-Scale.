"""Exercise the real Anthropic SDK against a local stand-in server."""
import json
import os
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from PIL import Image

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import claude_api, groq, llm, scenes, settings, store, writers  # noqa: E402
from vyral.app import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")
KEY = "sk-ant-test-key-wxyz"
PRODUCT = {"name": "Cup", "description": "A 300 ml ceramic cup.", "price": "", "audience": "", "facts": "", "reviews": ""}
COPY = {
    "brief": {"summary": "s", "pain_points": ["p"], "angles": ["a"], "missing_info": []},
    "hooks": [{"text": "A better morning cup.", "angle": "a"}, {"text": "97% agree.", "angle": "b"}],
    "scripts": [{"angle": "a", "body": "This 300 ml ceramic cup is nice.", "cta": "Buy it.", "flags": []}],
}


def message(text, stop="end_turn", input_tokens=1000, output_tokens=500):
    return {"id": "msg_1", "type": "message", "role": "assistant", "model": "claude-opus-5-5", "stop_reason": stop,
            "stop_sequence": None, "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            "content": [{"type": "thinking", "thinking": "", "signature": "x"}, {"type": "text", "text": text}]}


def api_error(kind, text):
    return {"type": "error", "error": {"type": kind, "message": text}}


@pytest.fixture
def api(monkeypatch):
    """Point the SDK at a local server that records requests and plays scripted replies."""
    state = {"requests": [], "replies": []}

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, status, body, headers=None):
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def _next(self, default):
            if state["replies"]:
                self._reply(*state["replies"].pop(0))
            else:
                self._reply(200, default)

        def do_GET(self):
            state["requests"].append({"path": self.path, "headers": dict(self.headers)})
            self._next({"id": "claude-opus-5-5", "type": "model", "display_name": "x", "created_at": "2026-01-01T00:00:00Z"})

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state["requests"].append({"path": self.path, "headers": dict(self.headers), "body": body})
            self._next(message(json.dumps(COPY)))

        def log_message(self, *_args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setenv("ANTHROPIC_BASE_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setattr(claude_api, "MAX_RETRIES", 0)
    yield state
    server.shutdown()


@pytest.fixture(autouse=True)
def clean_settings():
    folder = store.PROJECTS.parent
    for name in ("settings.json", "usage.json"):
        (folder / name).unlink(missing_ok=True)
    yield
    for name in ("settings.json", "usage.json"):
        (folder / name).unlink(missing_ok=True)


def values(**changes):
    return settings.update(anthropic_key=KEY, **changes)


def test_request_shape(api):
    claude_api.generate_copy(PRODUCT, values())
    request = api["requests"][0]
    body = request["body"]
    headers = {name.lower(): value for name, value in request["headers"].items()}
    assert headers["authorization"] == f"Bearer {KEY}"
    assert FALLBACK_HEADER in headers["anthropic-beta"]
    assert body["model"] == "claude-opus-5-5"
    assert body["fallbacks"] == "default"
    assert body["output_config"]["effort"] == "medium"
    assert body["output_config"]["format"]["type"] == "json_schema"
    assert "thinking" not in body and "temperature" not in body
    assert "Never invent statistics" in body["system"]
    assert "Reply with one JSON" not in body["system"]
    assert "300 ml ceramic cup" in body["messages"][0]["content"]


FALLBACK_HEADER = claude_api.FALLBACK_BETA


def test_haiku_skips_fallback(api):
    claude_api.generate_copy(PRODUCT, values(anthropic_model="claude-haiku-5-5"))
    assert "fallbacks" not in api["requests"][0]["body"]


def test_schemas_are_strict_everywhere():
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
    walk(claude_api.COPY_SCHEMA)
    walk(claude_api.SCENE_SCHEMA)


def test_spend_is_recorded_from_usage(api):
    claude_api.generate_copy(PRODUCT, values())
    usage = settings.month_usage()
    # 1,000 input tokens at $4 and 500 output tokens at $20 per million.
    assert usage["usd"] == pytest.approx(0.014)
    assert usage["requests"] == 1


def test_budget_stops_requests_before_spending(api):
    current = values(anthropic_monthly_budget_usd=0.01)
    claude_api.generate_copy(PRODUCT, current)
    count = len(api["requests"])
    with pytest.raises(llm.LLMLimitError, match="budget"):
        claude_api.generate_copy(PRODUCT, current)
    assert len(api["requests"]) == count


def test_refusal_is_reported_and_still_counted(api):
    api["replies"].append((200, message("", stop="refusal")))
    with pytest.raises(llm.LLMError, match="declined"):
        claude_api.generate_copy(PRODUCT, values())
    assert settings.month_usage()["requests"] == 1


def test_cut_off_reply_is_reported(api):
    api["replies"].append((200, message('{"brief":', stop="max_tokens")))
    with pytest.raises(llm.LLMError, match="cut off"):
        claude_api.generate_copy(PRODUCT, values())


@pytest.mark.parametrize("status,kind,text,expected", [
    (401, "authentication_error", "bad", "rejected your API key"),
    (403, "permission_error", "no", "doesn't have access"),
    (404, "not_found_error", "no", "doesn't recognize that model"),
    (429, "rate_limit_error", "slow", "rate limit"),
    (400, "invalid_request_error", "Your credit balance is too low", "out of credits"),
    (400, "invalid_request_error", "other", "couldn't accept"),
    (500, "api_error", "boom", "having a problem"),
])
def test_errors_are_explained_without_leaking_the_key(api, status, kind, text, expected):
    api["replies"].append((status, api_error(kind, text)))
    with pytest.raises(llm.LLMError, match=expected) as caught:
        claude_api.generate_copy(PRODUCT, values())
    assert KEY not in str(caught.value)


def test_rate_limit_includes_wait_time(api):
    api["replies"].append((429, api_error("rate_limit_error", "slow"), {"retry-after": "30"}))
    with pytest.raises(llm.LLMError, match="about 30 seconds"):
        claude_api.generate_copy(PRODUCT, values())


def test_invalid_key_is_not_saved(api):
    api["replies"].append((401, api_error("authentication_error", "bad")))
    assert client.put("/api/settings/anthropic-key", json={"key": KEY}).status_code == 400
    assert settings.load()["anthropic_key"] == ""


def test_valid_key_is_saved_without_spending_and_never_returned(api):
    response = client.put("/api/settings/anthropic-key", json={"key": f" {KEY} "})
    assert response.status_code == 200
    assert api["requests"][0]["path"].startswith("/v1/models/claude-opus-5-5")
    assert settings.load()["anthropic_key"] == KEY
    assert KEY not in response.text and KEY not in client.get("/api/settings").text
    assert response.json()["script_provider"] == "claude_api"
    assert response.json()["anthropic_key_hint"] == "wxyz"


def test_removing_key_switches_provider():
    settings.update(anthropic_key=KEY, script_provider="claude_api")
    assert client.delete("/api/settings/anthropic-key").json()["script_provider"] == "claude"
    settings.update(anthropic_key=KEY, groq_key="gsk_x", script_provider="claude_api")
    assert client.delete("/api/settings/anthropic-key").json()["script_provider"] == "groq"


def test_settings_reject_bad_values():
    base = {"script_provider": "claude", "groq_model": "m", "groq_fallback": True}
    assert client.put("/api/settings", json={**base, "script_provider": "claude_api"}).status_code == 400
    assert client.put("/api/settings", json={**base, "anthropic_model": "nope"}).status_code == 400
    assert client.put("/api/settings", json={**base, "anthropic_monthly_budget_usd": -1}).status_code == 422
    saved = client.put("/api/settings", json={**base, "anthropic_monthly_budget_usd": 0}).json()
    assert saved["anthropic_monthly_budget_usd"] == 0


def test_writer_checks_claims_and_names_source(api):
    values(script_provider="claude_api")
    copy, source = writers.write_copy(PRODUCT, {})
    assert source == "Claude API (claude-opus-5-5)"
    assert [hook["text"] for hook in copy["hooks"]] == ["A better morning cup."]


def test_budget_falls_back_to_groq(api, monkeypatch):
    monkeypatch.setattr(groq, "generate_copy", lambda *_args: COPY)
    settings.update(anthropic_key=KEY, script_provider="claude_api", groq_key="gsk_x", anthropic_monthly_budget_usd=0)
    assert writers.write_copy(PRODUCT, {})[1].startswith("Groq")
    assert api["requests"] == []


def test_api_errors_do_not_fall_back(api, monkeypatch):
    monkeypatch.setattr(groq, "generate_copy", lambda *_args: pytest.fail("Groq must not be called"))
    api["replies"].append((401, api_error("authentication_error", "bad")))
    settings.update(anthropic_key=KEY, script_provider="claude_api", groq_key="gsk_x")
    with pytest.raises(llm.LLMError, match="rejected"):
        writers.write_copy(PRODUCT, {})


def test_hosted_mode_blocks_personal_plan(monkeypatch, hosted_admin):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    monkeypatch.setattr(llm, "run_claude", lambda *_args, **_kwargs: pytest.fail("Claude Code must not run"))
    with pytest.raises(llm.LLMError, match="hosted"):
        writers.write_copy(PRODUCT, {})
    response = client.put("/api/settings", json={"script_provider": "claude", "groq_model": "m", "groq_fallback": True})
    assert response.status_code == 400
    with pytest.raises(llm.LLMError, match="Plan automatically"):
        writers.plan_scenes("Cup", {"H1": "Open it now."}, {})


def scene_reply(photo="p1.jpg"):
    return {"scenes": [
        {"item": "H1", "line": 1, "photo": photo, "layout": "product", "motion": "pop", "headline": "Open it"},
        {"item": "S1", "line": 1, "photo": "p2.jpg", "layout": "full", "motion": "zoom_in", "headline": "made up claim"},
    ]}


@pytest.fixture
def thumbs(tmp_path):
    paths = {}
    for name in ("aaa", "bbb"):
        path = tmp_path / f"{name}.jpg"
        Image.new("RGB", (64, 64), (200, 30, 30)).save(path)
        paths[name] = path
    return paths


def test_scene_planning_sends_labelled_photos(api, thumbs):
    api["replies"].append((200, message(json.dumps(scene_reply()))))
    settings.update(anthropic_key=KEY)
    plans = writers.plan_scenes("Cup", {"H1": "Open it now.", "S1": "Grip the lid well."}, thumbs)
    content = api["requests"][0]["body"]["messages"][0]["content"]
    assert [block["type"] for block in content] == ["text", "image", "text", "image", "text"]
    assert content[0]["text"] == "Photo p1.jpg:"
    assert content[1]["source"]["media_type"] == "image/jpeg"
    assert "Grip the lid well." in content[4]["text"]
    body = api["requests"][0]["body"]
    assert "tools" not in body
    assert plans["H1"]["scenes"][0]["photo_id"] == "aaa"
    assert plans["H1"]["scenes"][0]["headline"] == "Open it"
    # The headline rule still applies, so invented wording is dropped.
    assert plans["S1"]["scenes"][0]["photo_id"] == "bbb"
    assert plans["S1"]["scenes"][0]["headline"] == ""


def test_scene_planning_recovers_from_unknown_photo(api, thumbs):
    api["replies"].append((200, message(json.dumps(scene_reply(photo="elsewhere.jpg")))))
    settings.update(anthropic_key=KEY)
    plans = writers.plan_scenes("Cup", {"H1": "Open it now."}, thumbs)
    assert plans["H1"]["scenes"][0]["photo_id"] in thumbs


def test_scene_planning_without_key_uses_plan(monkeypatch):
    called = []
    monkeypatch.setattr(scenes, "plan_scenes", lambda *args: called.append(args) or {})
    writers.plan_scenes("Cup", {"H1": "Open it now."}, {})
    assert called


def test_key_is_not_written_into_projects(api):
    client.put("/api/settings/anthropic-key", json={"key": KEY})
    pid = client.post("/api/projects", json={"name": "T"}).json()["id"]
    assert KEY not in (store.project_dir(pid) / "project.json").read_text(encoding="utf-8")
