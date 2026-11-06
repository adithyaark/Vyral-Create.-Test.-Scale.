import json
import os
import tempfile

import pytest

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import settings, store  # noqa: E402
from vyral.app import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")
ENV_GROQ = "gsk_from_environment_9999"
ENV_ANTHROPIC = "sk-ant-from-environment-8888"
SAVED = "gsk_saved_in_file_1111"


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ("GROQ_API_KEY", "ANTHROPIC_API_KEY", "VYRAL_HOSTED"):
        monkeypatch.delenv(name, raising=False)
    path = store.PROJECTS.parent / "settings.json"
    path.unlink(missing_ok=True)
    yield
    path.unlink(missing_ok=True)


def settings_file():
    return (store.PROJECTS.parent / "settings.json").read_text(encoding="utf-8")


def test_environment_key_wins_over_saved_key(monkeypatch):
    settings.update(groq_key=SAVED)
    monkeypatch.setenv("GROQ_API_KEY", f" {ENV_GROQ}\n")
    assert settings.load()["groq_key"] == ENV_GROQ
    assert settings.key_source("groq_key") == "env"


def test_environment_keys_are_never_written_to_disk(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", ENV_GROQ)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_ANTHROPIC)
    settings.update(groq_model="openai/gpt-oss-20b")
    text = settings_file()
    assert ENV_GROQ not in text and ENV_ANTHROPIC not in text
    assert json.loads(text)["groq_key"] == ""


def test_environment_keys_are_never_sent_to_the_browser(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", ENV_GROQ)
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_ANTHROPIC)
    response = client.get("/api/settings")
    assert ENV_GROQ not in response.text and ENV_ANTHROPIC not in response.text
    body = response.json()
    assert body["has_groq_key"] and body["groq_key_source"] == "env"
    assert body["groq_key_hint"] == "" and body["anthropic_key_hint"] == ""


def test_environment_keys_cannot_be_changed_in_the_app(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", ENV_GROQ)
    assert client.put("/api/settings/groq-key", json={"key": "gsk_replacement_key"}).status_code == 409
    assert client.delete("/api/settings/groq-key").status_code == 409
    assert settings.load()["groq_key"] == ENV_GROQ


def test_hosted_server_ignores_saved_keys(monkeypatch, hosted_admin):
    settings.update(groq_key=SAVED)
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    assert settings.load()["groq_key"] == ""
    assert client.get("/api/settings").json()["has_groq_key"] is False


@pytest.mark.parametrize("path", ["groq-key", "anthropic-key"])
def test_hosted_server_refuses_key_changes(monkeypatch, hosted_admin, path):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    assert client.put(f"/api/settings/{path}", json={"key": "some-long-key-value"}).status_code == 403
    assert client.delete(f"/api/settings/{path}").status_code == 403


def test_hosted_server_can_use_environment_keys(monkeypatch, hosted_admin):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    monkeypatch.setenv("GROQ_API_KEY", ENV_GROQ)
    assert settings.load()["groq_key"] == ENV_GROQ
    saved = client.put("/api/settings", json={"script_provider": "groq", "groq_model": "m", "groq_fallback": True})
    assert saved.status_code == 200 and saved.json()["script_provider"] == "groq"


def test_hosted_server_never_defaults_to_personal_plan(monkeypatch):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    monkeypatch.setenv("GROQ_API_KEY", ENV_GROQ)
    assert settings.load()["script_provider"] == "groq"
    monkeypatch.setenv("ANTHROPIC_API_KEY", ENV_ANTHROPIC)
    assert settings.load()["script_provider"] == "claude_api"


def test_local_saved_key_still_works():
    settings.update(groq_key=SAVED)
    body = client.get("/api/settings").json()
    assert body["groq_key_source"] == "saved" and body["groq_key_hint"] == "1111"
    assert SAVED not in json.dumps(body)


def test_blank_environment_value_is_ignored(monkeypatch):
    settings.update(groq_key=SAVED)
    monkeypatch.setenv("GROQ_API_KEY", "   ")
    assert settings.load()["groq_key"] == SAVED
