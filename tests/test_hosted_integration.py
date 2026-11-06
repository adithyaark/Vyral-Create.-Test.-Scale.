"""Isolation tests against a real Supabase project, using two throwaway users.

Run with VYRAL_TEST_SUPABASE=1. They create temporary users and delete them afterwards.
"""
import io
import os
import re
import tempfile
import time
import uuid

import httpx
import pytest
from dotenv import load_dotenv
from fastapi.routing import APIRoute
from PIL import Image

pytestmark = pytest.mark.skipif(os.environ.get("VYRAL_TEST_SUPABASE") != "1", reason="needs a real Supabase project")

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import accounts, blobs, db, identity  # noqa: E402
from vyral.app import app  # noqa: E402
from vyral.tools import ROOT  # noqa: E402

PASSWORD = "test-" + uuid.uuid4().hex
OPEN = {"/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/forgot", "/api/auth/session", "/api/auth/logout"}
ADMIN_ONLY_ANYWAY = {"/api/settings", "/api/settings/groq-key", "/api/settings/anthropic-key", "/api/settings/groq-models"}


def make_user():
    email = f"vyral-test-{uuid.uuid4().hex[:12]}@example.com"
    response = httpx.post(f"{os.environ['SUPABASE_URL'].rstrip('/')}/auth/v1/admin/users",
                          headers={"apikey": os.environ["SUPABASE_SECRET_KEY"]},
                          json={"email": email, "password": PASSWORD, "email_confirm": True}, timeout=30)
    assert response.status_code in (200, 201), response.status_code
    return {"id": response.json()["id"], "email": email}


def signed_in(user):
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    response = client.post("/api/auth/login", json={"email": user["email"], "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client


@pytest.fixture(scope="module")
def world():
    load_dotenv(ROOT / ".env", override=False)
    patcher = pytest.MonkeyPatch()
    patcher.setenv("VYRAL_HOSTED", "1")
    patcher.setenv("VYRAL_ADMIN_EMAILS", "")
    patcher.setenv("VYRAL_MONTHLY_VIDEOS", "2")
    patcher.setenv("VYRAL_MAX_PROJECTS", "3")
    db.migrate()
    alice, bob = make_user(), make_user()
    yield {"alice": alice, "bob": bob, "a": signed_in(alice), "b": signed_in(bob)}
    for user in (alice, bob):
        try:
            blobs.delete_user_files(user["id"])
            accounts.delete_user(user["id"])
        except Exception:  # The account-deletion test has already removed Alice.
            pass
    patcher.undo()


def jpeg(color=(200, 40, 40)):
    buffer = io.BytesIO()
    Image.new("RGB", (900, 900), color).save(buffer, "JPEG")
    return buffer.getvalue()


@pytest.fixture(scope="module")
def alice_project(world):
    client = world["a"]
    pid = client.post("/api/projects", json={"name": "Alice secret"}).json()["id"]
    client.put(f"/api/projects/{pid}/product", json={"name": "Alice secret", "description": "A red cup for coffee."})
    client.put(f"/api/projects/{pid}/copy", json={
        "hooks": [{"id": "H1", "text": "Your morning needs a better cup.", "approved": True}],
        "scripts": [{"id": "S1", "body": "This red cup holds your coffee nicely.", "cta": "Try it today.", "approved": True}],
    })
    upload = client.post(f"/api/projects/{pid}/photos", files=[("files", ("cup.jpg", jpeg(), "image/jpeg"))])
    assert upload.status_code == 200, upload.text
    client.post(f"/api/projects/{pid}/auto-plan")
    return pid


def api_routes():
    for route in app.routes:
        if isinstance(route, APIRoute) and route.path.startswith("/api/"):
            for method in route.methods - {"HEAD", "OPTIONS"}:
                yield method, route.path


def fill(path, pid):
    return re.sub(r"\{[a-z_]+\}", "x", path.replace("{project_id}", pid))


def test_signed_out_requests_are_refused_on_every_route(world):
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    checked = 0
    for method, path in api_routes():
        if path in OPEN:
            continue
        assert client.request(method, fill(path, "aaaaaaaaaaaa")).status_code == 401, (method, path)
        checked += 1
    assert checked > 25


def test_session_cookies_are_protected_and_tokens_stay_out_of_the_page(world):
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    response = client.post("/api/auth/login", json={"email": world["bob"]["email"], "password": PASSWORD})
    cookies = response.headers.get_list("set-cookie")
    assert len(cookies) == 2
    assert all("httponly" in c.lower() and "samesite=lax" in c.lower() for c in cookies)
    assert "token" not in response.text


def test_wrong_password_is_refused(world):
    client = TestClient(app, base_url="http://localhost")
    response = client.post("/api/auth/login", json={"email": world["bob"]["email"], "password": "wrong-password"})
    assert response.status_code == 400
    assert response.json()["detail"] == "That email or password is incorrect."


def test_other_user_cannot_touch_a_project_on_any_route(world, alice_project):
    b = world["b"]
    before = world["a"].get(f"/api/projects/{alice_project}").json()
    checked = 0
    for method, path in api_routes():
        if "{project_id}" not in path:
            continue
        # A 422 means the request was refused for its shape, before reaching any data.
        status = b.request(method, fill(path, alice_project)).status_code
        assert status in (404, 422), (method, path, status)
        checked += 1
    assert checked > 15
    valid = [
        ("PUT", "product", {"json": {"name": "Hacked", "description": "x"}}),
        ("PUT", "copy", {"json": {"hooks": [], "scripts": []}}),
        ("PUT", "visuals", {"json": {"visual_mode": "clips"}}),
        ("POST", "import-copy", {"json": {"text": "{}"}}),
        ("POST", "photos", {"files": [("files", ("a.jpg", jpeg(), "image/jpeg"))]}),
        ("POST", "render", {"json": {"variants": [{"script_id": "S1", "hook_id": "H1"}], "voice_mode": "ai"}}),
        ("PUT", "scene-plans/H1", {"json": {"text": "x", "scenes": []}}),
    ]
    for method, tail, options in valid:
        assert b.request(method, f"/api/projects/{alice_project}/{tail}", **options).status_code == 404, tail
    assert b.delete(f"/api/projects/{alice_project}").status_code == 404
    assert world["a"].get(f"/api/projects/{alice_project}").json() == before
    assert alice_project not in [p["id"] for p in b.get("/api/projects").json()]


def test_row_level_security_blocks_even_a_query_with_no_owner_check(world, alice_project):
    bob_id = world["bob"]["id"]
    with db.as_user(bob_id) as connection:
        assert connection.execute("select count(*) from vyral.projects where id = %s", [alice_project]).fetchone()[0] == 0
        owners = connection.execute("select distinct owner_id::text from vyral.projects").fetchall()
        assert all(row[0] == bob_id for row in owners)
        assert connection.execute("update vyral.projects set name = 'hacked'").rowcount == 0 or \
            connection.execute("select count(*) from vyral.projects where name = 'hacked' and id = %s", [alice_project]).fetchone()[0] == 0
    with pytest.raises(Exception):
        with db.as_user(bob_id) as connection:
            connection.execute("insert into vyral.projects (id, owner_id, name, data) values (%s, %s, 'x', '{}')",
                               [uuid.uuid4().hex[:12], world["alice"]["id"]])
    with pytest.raises(Exception):
        with db.as_user(bob_id) as connection:
            connection.execute("update vyral.usage set videos = 0")


def test_tables_are_not_reachable_through_supabase_public_api(world):
    url = os.environ["SUPABASE_URL"].rstrip("/")
    headers = {"apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"], "Accept-Profile": "vyral"}
    response = httpx.get(f"{url}/rest/v1/projects?select=*", headers=headers, timeout=30)
    assert response.status_code != 200 or response.json() == []


def test_photos_use_private_expiring_links(world, alice_project):
    a = world["a"]
    project = a.get(f"/api/projects/{alice_project}").json()
    photo = project["photos"][0]["id"]
    response = a.get(f"/api/projects/{alice_project}/photos/{photo}")
    assert response.status_code == 307
    link = response.headers["location"]
    assert "token=" in link
    assert httpx.get(link, timeout=30).status_code == 200
    plain = link.split("/object/sign/")[0] + "/object/public/" + link.split("/object/sign/")[1].split("?")[0]
    assert httpx.get(plain, timeout=30).status_code != 200
    unsigned = link.split("?")[0]
    assert httpx.get(unsigned, timeout=30).status_code != 200
    assert world["b"].get(f"/api/projects/{alice_project}/photos/{photo}").status_code == 404
    public_headers = {"apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"]}
    direct = link.split("/object/sign/")[0] + "/object/" + link.split("/object/sign/")[1].split("?")[0]
    assert httpx.get(direct, headers=public_headers, timeout=30).status_code != 200


def test_customers_cannot_change_settings_or_see_keys(world):
    a = world["a"]
    secret = os.environ.get("GROQ_API_KEY", "")
    assert a.put("/api/settings", json={"script_provider": "groq", "groq_model": "m", "groq_fallback": True}).status_code == 403
    assert a.put("/api/settings/groq-key", json={"key": "some-long-key-value"}).status_code == 403
    assert a.get("/api/settings/groq-models").status_code == 403
    assert not secret or secret not in a.get("/api/settings").text
    assert a.get("/api/status").status_code == 200


def test_uploads_over_the_limit_are_refused(world, alice_project, monkeypatch):
    monkeypatch.setenv("VYRAL_MAX_UPLOAD_MB", "1")
    big = b"x" * (2 * 1024 * 1024)
    response = world["a"].post(f"/api/projects/{alice_project}/photos", files=[("files", ("big.jpg", big, "image/jpeg"))])
    assert response.status_code == 413


def test_project_limit(world):
    b = world["b"]
    created = [b.post("/api/projects", json={"name": f"P{i}"}) for i in range(3)]
    assert [response.status_code for response in created] == [200, 200, 200]
    assert b.post("/api/projects", json={"name": "One too many"}).status_code == 429
    for response in created:
        assert b.delete(f"/api/projects/{response.json()['id']}").status_code == 200


def test_render_saves_to_cloud_and_counts_usage(world, alice_project):
    a = world["a"]
    alice = world["alice"]["id"]
    # Over the allowance of 2, so it's refused before any work happens.
    too_many = [{"script_id": "S1", "hook_id": "H1"}] * 3
    assert a.post(f"/api/projects/{alice_project}/render", json={"variants": too_many, "voice_mode": "ai"}).status_code == 429
    assert accounts.usage(alice)["videos"] == 0

    response = a.post(f"/api/projects/{alice_project}/render",
                      json={"variants": [{"script_id": "S1", "hook_id": "H1"}], "voice_mode": "ai"})
    assert response.status_code == 200, response.text
    job = response.json()
    assert world["b"].get(f"/api/jobs/{job['id']}").status_code == 404
    for _ in range(300):
        job = a.get(f"/api/jobs/{job['id']}").json()
        if job["status"] in ("done", "failed"):
            break
        time.sleep(1)
    assert job["status"] == "done", job["error"]
    project = a.get(f"/api/projects/{alice_project}").json()
    name = project["videos"][0]["file"]
    link = a.get(f"/api/projects/{alice_project}/videos/{name}").headers["location"]
    video = httpx.get(link, timeout=60)
    assert video.status_code == 200 and video.content[4:8] == b"ftyp"
    assert accounts.usage(alice)["videos"] == 1
    assert world["b"].get(f"/api/projects/{alice_project}/videos/{name}").status_code == 404
    assert a.get("/api/auth/me").json()["usage"]["videos"] == 1


def test_allowance_cannot_be_exceeded_and_can_be_refunded(world):
    bob = world["bob"]["id"]
    assert accounts.consume(bob, "videos", 2)
    assert not accounts.consume(bob, "videos", 1)
    accounts.release(bob, "videos", 1)
    assert accounts.consume(bob, "videos", 1)
    accounts.release(bob, "videos", 2)
    assert accounts.usage(bob)["videos"] == 0


def test_deleting_an_account_removes_everything(world, alice_project):
    a = world["a"]
    alice = world["alice"]
    identity.set_user(alice)
    assert blobs._walk(f"users/{alice['id']}")
    identity.set_user(None)
    assert a.request("DELETE", "/api/account", json={"password": "wrong-password"}).status_code == 403
    assert a.get(f"/api/projects/{alice_project}").status_code == 200

    assert a.request("DELETE", "/api/account", json={"password": PASSWORD}).status_code == 200
    assert blobs._walk(f"users/{alice['id']}") == []
    with db.as_server() as connection:
        assert connection.execute("select count(*) from vyral.projects where owner_id = %s", [alice["id"]]).fetchone()[0] == 0
        assert connection.execute("select count(*) from vyral.usage where owner_id = %s", [alice["id"]]).fetchone()[0] == 0
    client = TestClient(app, base_url="http://localhost")
    assert client.post("/api/auth/login", json={"email": alice["email"], "password": PASSWORD}).status_code == 400
    assert a.get("/api/projects").status_code == 401


def test_email_link_session_works_and_fake_links_do_not(world):
    user = make_user()
    try:
        session = accounts.sign_in(user["email"], PASSWORD)
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        fake = client.post("/api/auth/session", json={"access_token": "not-a-real-token", "refresh_token": "x"})
        assert fake.status_code == 401
        assert client.get("/api/projects").status_code == 401
        real = client.post("/api/auth/session", json={"access_token": session["access_token"],
                                                      "refresh_token": session["refresh_token"]})
        assert real.status_code == 200
        assert client.get("/api/auth/me").json()["user"]["email"] == user["email"]
        assert client.get("/api/projects").json() == []
        assert client.post("/api/auth/password", json={"password": "short"}).status_code == 400
        assert client.post("/api/auth/logout").status_code == 200
        assert client.get("/api/projects").status_code == 401
    finally:
        accounts.delete_user(user["id"])


def test_signup_refuses_short_password_without_contacting_supabase(world):
    client = TestClient(app, base_url="http://localhost")
    response = client.post("/api/auth/signup", json={"email": "someone@example.com", "password": "short"})
    assert response.status_code == 400


def test_expired_session_renews_from_the_refresh_cookie(world):
    user = make_user()
    try:
        session = accounts.sign_in(user["email"], PASSWORD)
        client = TestClient(app, base_url="http://localhost", follow_redirects=False)
        client.cookies.set("vyral_refresh", session["refresh_token"])
        response = client.get("/api/projects")
        assert response.status_code == 200
        assert any("vyral_access=" in cookie for cookie in response.headers.get_list("set-cookie"))
    finally:
        accounts.delete_user(user["id"])


def test_local_projects_import_only_into_the_chosen_account(world, tmp_path):
    from vyral import import_local

    owner, other = make_user(), make_user()
    pid = uuid.uuid4().hex[:12]
    folder = tmp_path / pid
    (folder / "videos").mkdir(parents=True)
    (folder / "videos" / "good.mp4").write_bytes(b"....ftypvideo")
    project = {"id": pid, "name": "Imported", "product": {}, "hooks": [], "scripts": [], "clips": [], "recordings": {},
               "photos": [], "scene_plans": {},
               "videos": [{"file": "good.mp4", "key": "S1-H1", "seconds": 1}, {"file": "gone.mp4", "key": "S1-H2", "seconds": 1}]}
    (folder / "project.json").write_text(__import__("json").dumps(project), encoding="utf-8")
    try:
        assert import_local.find_user(owner["email"])["id"] == owner["id"]
        preview = import_local.import_projects(owner, tmp_path, apply=False)
        assert preview[0]["status"] == "ready" and preview[0]["missing"] == 1
        assert signed_in(owner).get("/api/projects").json() == []

        assert import_local.import_projects(owner, tmp_path, apply=True)[0]["status"] == "imported"
        client = signed_in(owner)
        imported = client.get(f"/api/projects/{pid}").json()
        assert [video["file"] for video in imported["videos"]] == ["good.mp4"]
        link = client.get(f"/api/projects/{pid}/videos/good.mp4").headers["location"]
        assert httpx.get(link, timeout=30).content == b"....ftypvideo"

        intruder = signed_in(other)
        assert intruder.get(f"/api/projects/{pid}").status_code == 404
        assert intruder.get(f"/api/projects/{pid}/videos/good.mp4").status_code == 404
        assert import_local.import_projects(owner, tmp_path, apply=True)[0]["status"] == "already imported"
        assert import_local.import_projects(other, tmp_path, apply=True)[0]["status"] == "belongs to another account"
        assert (folder / "videos" / "good.mp4").is_file()
    finally:
        for user in (owner, other):
            blobs.delete_user_files(user["id"])
            accounts.delete_user(user["id"])
