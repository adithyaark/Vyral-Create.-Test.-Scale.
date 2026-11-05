import os
import tempfile

os.environ["VYRAL_DATA"] = tempfile.mkdtemp(prefix="vyral-test-")

from fastapi.testclient import TestClient  # noqa: E402

from vyral.app import app  # noqa: E402

client = TestClient(app, base_url="http://127.0.0.1")


def new_project():
    return client.post("/api/projects", json={"name": "Test"}).json()


def test_other_websites_are_rejected():
    response = client.post("/api/projects", json={"name": "x"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_local_origin_is_allowed():
    response = client.post("/api/projects", json={"name": "x"}, headers={"Origin": "http://127.0.0.1:8765"})
    assert response.status_code == 200


def test_foreign_host_is_rejected():
    assert client.get("/api/status", headers={"Host": "evil.example"}).status_code == 400


def test_bad_project_id_is_not_found():
    assert client.get("/api/projects/..%2F..%2Fsecret").status_code == 404
    assert client.get("/api/projects/abc").status_code == 404


def test_generate_needs_description():
    project = new_project()
    assert client.post(f"/api/projects/{project['id']}/generate").status_code == 400


def test_upload_rejects_unsupported_type():
    project = new_project()
    response = client.post(f"/api/projects/{project['id']}/clips",
                           files=[("files", ("run.exe", b"x", "application/octet-stream"))])
    assert response.status_code == 400


def test_render_refuses_unapproved_scripts():
    project = new_project()
    pid = project["id"]
    client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "hook", "approved": False}],
        "scripts": [{"id": "S1", "body": "body", "approved": False}],
    })
    from vyral import store
    def add_clip(p):
        p["visual_mode"] = "clips"
        p["clips"].append({"id": "c", "file": "c.mp4", "name": "c", "duration": 5})
    store.update(pid, add_clip)
    response = client.post(f"/api/projects/{pid}/render", json={
        "variants": [{"script_id": "S1", "hook_id": "H1"}], "voice_mode": "ai", "voice": "af_heart",
    })
    assert response.status_code == 400
    assert "approved" in response.json()["detail"]


def test_video_outside_project_is_not_served():
    project = new_project()
    assert client.get(f"/api/projects/{project['id']}/videos/project.json").status_code == 404
