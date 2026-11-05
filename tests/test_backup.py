import json
import os
import tempfile

import pytest

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import llm, render, store  # noqa: E402
from vyral.app import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")

REPLY = {
    "brief": {"summary": "s", "pain_points": ["p"], "angles": ["a"], "missing_info": []},
    "hooks": [{"text": "A hook line.", "angle": "a"}],
    "scripts": [{"angle": "a", "body": "A script body here.", "cta": "Buy it.", "flags": ["check"]}],
}


def new_project(description="A product."):
    pid = client.post("/api/projects", json={"name": "Test"}).json()["id"]
    client.put(f"/api/projects/{pid}/product", json={"name": "Test", "description": description})
    return pid


@pytest.mark.parametrize("message", [
    "Claude usage limit reached. Your limit will reset at 3pm.",
    "You've hit your limit · resets 3pm",
    "You've reached your session limit",
])
def test_limit_errors_get_a_clear_message(message):
    with pytest.raises(llm.LLMError, match="usage limit"):
        llm.extract_json(json.dumps({"is_error": True, "result": message}))


def test_plain_text_limit_output_is_recognized():
    with pytest.raises(llm.LLMError, match="usage limit"):
        llm.extract_json("Claude usage limit reached")


def test_rate_limit_status_is_recognized():
    with pytest.raises(llm.LLMError, match="usage limit"):
        llm.extract_json(json.dumps({"is_error": True, "result": "Error", "api_error_status": 429}))


def test_other_errors_keep_their_message():
    with pytest.raises(llm.LLMError, match="Something else"):
        llm.extract_json(json.dumps({"is_error": True, "result": "Something else broke"}))


def test_prompt_contains_rules_and_product():
    pid = new_project("A very special jar opener.")
    prompt = client.get(f"/api/projects/{pid}/script-prompt").json()["prompt"]
    assert "Never invent statistics" in prompt
    assert "A very special jar opener." in prompt


def test_prompt_needs_description():
    pid = new_project("")
    assert client.get(f"/api/projects/{pid}/script-prompt").status_code == 400


@pytest.mark.parametrize("wrap", [
    "{}",
    "Here is your JSON:\n```json\n{}\n```\nHope that helps!",
])
def test_import_accepts_reply_with_extra_wording(wrap):
    pid = new_project()
    saved = client.post(f"/api/projects/{pid}/import-copy", json={"text": wrap.format(json.dumps(REPLY))
                        if False else wrap.replace("{}", json.dumps(REPLY))}).json()
    assert saved["hooks"][0] == {"id": "H1", "text": "A hook line.", "angle": "a", "approved": False}
    assert saved["scripts"][0]["id"] == "S1" and saved["scripts"][0]["approved"] is False
    assert saved["scripts"][0]["flags"] == ["check"]


@pytest.mark.parametrize("text", ["no json at all", '{"hooks": [', '{"hooks": [], "scripts": []}', "[1, 2]"])
def test_import_rejects_bad_replies_without_changing_project(text):
    pid = new_project()
    response = client.post(f"/api/projects/{pid}/import-copy", json={"text": text})
    assert response.status_code == 400
    assert store.load(pid)["scripts"] == []


def test_blank_items_are_not_planned_or_rendered():
    pid = new_project()
    client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "   ", "approved": True}],
        "scripts": [{"id": "S1", "body": "", "approved": True}],
    })
    store.update(pid, lambda p: p["photos"].append({"id": "aaaaaaaaaaaa", "name": "a", "width": 1, "height": 1}))
    assert client.post(f"/api/projects/{pid}/auto-plan").status_code == 400
    response = client.post(f"/api/projects/{pid}/render", json={
        "variants": [{"script_id": "S1", "hook_id": "H1"}], "voice_mode": "ai", "voice": "af_heart",
    })
    assert response.status_code == 400


def test_auto_plan_uses_every_photo_without_claude(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError("Claude must not be called")

    monkeypatch.setattr(llm, "run_claude", fail)
    pid = new_project()
    client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "Open it now.", "approved": True}],
        "scripts": [{"id": "S1", "body": "This opener grips lids well. It is easy to use.", "cta": "Try it today.", "approved": True}],
    })
    ids = ["aaaaaaaaaaaa", "bbbbbbbbbbbb"]
    store.update(pid, lambda p: p["photos"].extend({"id": i, "name": i, "width": 1, "height": 1} for i in ids))
    plans = client.post(f"/api/projects/{pid}/auto-plan").json()["scene_plans"]
    assert set(plans) == {"H1", "S1"}
    scenes = plans["H1"]["scenes"] + plans["S1"]["scenes"]
    assert {scene["photo_id"] for scene in scenes} == set(ids)
    assert all(scene["layout"] == "fit" and scene["headline"] == "" for scene in scenes)


def test_gpu_failure_retries_on_cpu(monkeypatch):
    monkeypatch.setattr(render, "_gpu_works", True)
    used = []

    def work(args):
        used.append(args)
        if args is render.GPU_VIDEO_ARGS:
            raise render.RenderError("GPU failed")
        return 12.0

    assert render.with_best_encoder(work) == 12.0
    assert used == [render.GPU_VIDEO_ARGS, render.CPU_VIDEO_ARGS]
    assert render._gpu_works is False


def test_cpu_failure_is_reported(monkeypatch):
    monkeypatch.setattr(render, "_gpu_works", False)

    def work(_args):
        raise render.RenderError("Real problem")

    with pytest.raises(render.RenderError, match="Real problem"):
        render.with_best_encoder(work)
