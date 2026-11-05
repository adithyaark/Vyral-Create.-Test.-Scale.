import io
import os
import shutil
import tempfile

import pytest
from PIL import Image

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import app as app_module  # noqa: E402
from vyral import store  # noqa: E402

client = TestClient(app_module.app, base_url="http://127.0.0.1")


def project_with_video(name="a.mp4"):
    pid = client.post("/api/projects", json={"name": "Test"}).json()["id"]
    path = store.project_dir(pid) / "videos" / name
    path.write_bytes(b"video")
    store.update(pid, lambda p: p["videos"].append({"file": name, "key": "S1-H1", "seconds": 1}))
    return pid, path


def jpeg_bytes():
    buffer = io.BytesIO()
    Image.new("RGB", (900, 900), (200, 40, 40)).save(buffer, "JPEG")
    return buffer.getvalue()


def test_older_project_without_photos_folder_can_upload():
    pid = client.post("/api/projects", json={"name": "Old"}).json()["id"]
    shutil.rmtree(store.project_dir(pid) / "photos")
    response = client.post(f"/api/projects/{pid}/photos", files=[("files", ("a.jpg", jpeg_bytes(), "image/jpeg"))])
    assert response.status_code == 200
    assert len(response.json()["project"]["photos"]) == 1


def test_delete_video_removes_file_and_record():
    pid, path = project_with_video()
    saved = client.delete(f"/api/projects/{pid}/videos/a.mp4").json()
    assert saved["videos"] == []
    assert not path.exists()


def test_delete_unknown_video_is_not_found():
    pid, path = project_with_video()
    assert client.delete(f"/api/projects/{pid}/videos/project.json").status_code == 404
    assert path.exists()


@pytest.mark.skipif(os.name != "nt", reason="Only Windows locks open files")
def test_video_in_use_is_kept_and_reported(monkeypatch):
    monkeypatch.setattr(app_module, "REMOVE_WAIT_SECONDS", 0.01)
    pid, path = project_with_video()
    with path.open("rb"):
        response = client.delete(f"/api/projects/{pid}/videos/a.mp4")
    assert response.status_code == 409
    assert "in use" in response.json()["detail"]
    assert len(store.load(pid)["videos"]) == 1
    assert client.delete(f"/api/projects/{pid}/videos/a.mp4").json()["videos"] == []


@pytest.mark.skipif(os.name != "nt", reason="Only Windows locks open files")
def test_project_in_use_is_not_half_deleted():
    pid, path = project_with_video()
    with path.open("rb"):
        response = client.delete(f"/api/projects/{pid}")
    assert response.status_code == 409
    assert store.load(pid)["videos"]
    assert path.exists()
    assert client.delete(f"/api/projects/{pid}").status_code == 200
    assert client.get(f"/api/projects/{pid}").status_code == 404
