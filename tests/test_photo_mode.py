import os
import tempfile

import pytest
from PIL import Image

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import store  # noqa: E402
from vyral.app import app  # noqa: E402
from vyral.motion import SceneArt, readable_ink, scene_times  # noqa: E402
from vyral.render import HEIGHT, WIDTH  # noqa: E402
from vyral.scenes import MOTIONS, build_plan, clean_headline, split_lines  # noqa: E402
from vyral.voice import Word  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")


def test_split_lines_preserves_every_word():
    text = ("Tired of stuck lids? This small tool grips, turns, and opens jars in your kitchen without any batteries, "
            "cords, or charging at all, even on busy mornings. Try it.")
    lines = split_lines(text)
    assert " ".join(lines).split() == text.split()
    assert all(len(line.split()) <= 16 for line in lines)
    assert len(lines) > 2


def test_headline_cannot_add_claims():
    line = "It opens most jars in your kitchen."
    assert clean_headline("Opens most jars", line) == "Opens most jars"
    assert clean_headline("Opens 100% of jars", line) == ""
    assert clean_headline("Clinically proven", line) == ""
    assert clean_headline(None, line) == ""


def test_plan_fills_gaps_and_rejects_unknown_values():
    text = "Open it fast. Keep it close by."
    reply = {"plans": {"S1": [{"line": 1, "photo": "other.jpg", "layout": "bad", "motion": "bad", "headline": "Open it"}]}}
    plan = build_plan({"S1": text}, reply, {"p1.jpg": "abc"})["S1"]
    assert [scene["text"] for scene in plan["scenes"]] == split_lines(text)
    first = plan["scenes"][0]
    assert first["photo_id"] == "abc" and first["layout"] == "fit" and first["headline"] == "Open it"


def test_scene_times_follow_spoken_words():
    words = [Word(str(i), float(i), i + 0.5) for i in range(5)]
    timed = scene_times([{"text": "0 1"}, {"text": "2 3 4"}], words, 5.3)
    assert (timed[0].start, timed[0].end, timed[1].start, timed[1].end) == (0.0, 2.0, 2.0, 5.3)


@pytest.mark.parametrize("motion", MOTIONS)
@pytest.mark.parametrize("layout", ["fit", "full"])
def test_scene_frames_are_full_size(tmp_path, motion, layout):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (900, 600), (200, 40, 40)).save(path)
    art = SceneArt({"motion": motion, "layout": layout, "headline": "Grip it"}, path, (90, 40, 200), (90, 40, 200))
    for progress, elapsed in ((0, 0), (0.5, 1), (1, 2)):
        assert art.frame(progress, elapsed).size == (WIDTH, HEIGHT)


@pytest.mark.parametrize("motion", MOTIONS)
def test_full_screen_motion_never_shows_empty_edges(tmp_path, motion):
    path = tmp_path / "photo.jpg"
    Image.new("RGB", (1000, 1000), (255, 255, 255)).save(path)
    art = SceneArt({"motion": motion, "layout": "full", "headline": ""}, path, (0, 0, 0), (0, 0, 0))
    for progress, elapsed in ((0, 0), (0.5, 0.1), (1, 3)):
        frame = art.frame(progress, elapsed)
        corners = [frame.getpixel(point) for point in ((0, 0), (1079, 0), (0, 1919), (1079, 1919))]
        assert all(min(corner) > 240 for corner in corners)


def test_ink_choice_is_readable():
    assert readable_ink((10, 10, 30)) == (255, 255, 255)
    assert readable_ink((250, 240, 120)) == (15, 15, 20)


def test_cutout_cleanup_removes_small_islands_only():
    from vyral.photos import remove_specks
    alpha = Image.new("L", (100, 100), 0)
    alpha.paste(255, (20, 20, 70, 70))
    alpha.paste(255, (85, 85, 88, 88))
    cleaned = remove_specks(alpha)
    assert cleaned.getpixel((40, 40)) == 255
    assert cleaned.getpixel((86, 86)) == 0


def new_project():
    return client.post("/api/projects", json={"name": "Test"}).json()


def approved_project():
    pid = new_project()["id"]
    client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "Open it now.", "approved": True}],
        "scripts": [{"id": "S1", "body": "This opener grips lids well.", "cta": "Try it.", "approved": True}],
    })
    store.update(pid, lambda p: p["photos"].append({"id": "aaaaaaaaaaaa", "name": "a", "width": 1, "height": 1}))
    return pid


def scene(text):
    return {"text": text, "photo_id": "aaaaaaaaaaaa", "layout": "fit", "motion": "pop", "headline": ""}


def test_render_requires_scene_plan():
    pid = approved_project()
    response = client.post(f"/api/projects/{pid}/render", json={
        "variants": [{"script_id": "S1", "hook_id": "H1"}], "voice_mode": "ai", "voice": "af_heart",
    })
    assert response.status_code == 400
    assert "Plan the scenes" in response.json()["detail"]


def test_editing_copy_discards_only_changed_plans():
    pid = approved_project()
    store.update(pid, lambda p: p["scene_plans"].update({
        "H1": {"text": "Open it now.", "scenes": [scene("Open it now.")]},
        "S1": {"text": "This opener grips lids well. Try it.", "scenes": [scene("x")]},
    }))
    saved = client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "Open it now.", "approved": True}],
        "scripts": [{"id": "S1", "body": "Changed wording here.", "cta": "Try it.", "approved": True}],
    }).json()
    assert list(saved["scene_plans"]) == ["H1"]


def test_upload_rejects_fake_photo():
    pid = new_project()["id"]
    response = client.post(f"/api/projects/{pid}/photos", files=[("files", ("fake.jpg", b"nope", "image/jpeg"))])
    assert response.status_code == 200
    assert response.json()["rejected"] == ["fake.jpg"]
    assert response.json()["project"]["photos"] == []


def test_deleting_photo_discards_plans_using_it():
    pid = approved_project()
    store.update(pid, lambda p: p["scene_plans"].update(
        {"H1": {"text": "Open it now.", "scenes": [scene("Open it now.")]}}))
    saved = client.delete(f"/api/projects/{pid}/photos/aaaaaaaaaaaa").json()
    assert saved["photos"] == [] and saved["scene_plans"] == {}


def test_photo_path_tricks_are_not_served():
    pid = new_project()["id"]
    assert client.get(f"/api/projects/{pid}/photos/project.json").status_code == 404


def test_scene_edit_cannot_change_script_lines():
    pid = approved_project()
    response = client.put(f"/api/projects/{pid}/scene-plans/H1", json={
        "text": "Open it now.", "scenes": [scene("Different words here.")],
    })
    assert response.status_code == 400
