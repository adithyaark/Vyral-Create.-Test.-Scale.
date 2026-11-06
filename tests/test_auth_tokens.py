"""Check token handling with locally created keys, so no network is needed."""
import os
import tempfile
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec

os.environ.setdefault("VYRAL_DATA", tempfile.mkdtemp(prefix="vyral-test-"))

from fastapi.testclient import TestClient  # noqa: E402

from vyral import accounts, identity  # noqa: E402
from vyral.app import app  # noqa: E402

URL = "https://example-project.supabase.co"
KEY = ec.generate_private_key(ec.SECP256R1())
OTHER_KEY = ec.generate_private_key(ec.SECP256R1())
client = TestClient(app, base_url="http://127.0.0.1")


class FakeSigningKey:
    key = KEY.public_key()


class FakeJwks:
    def get_signing_key_from_jwt(self, _token):
        return FakeSigningKey()


@pytest.fixture(autouse=True)
def setup(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", URL)
    monkeypatch.setattr(accounts, "_jwks_client", FakeJwks())


def token(key=KEY, algorithm="ES256", **changes):
    claims = {"sub": "user-1", "email": "a@example.com", "aud": "authenticated", "iss": f"{URL}/auth/v1",
              "exp": int(time.time()) + 600, **changes}
    return jwt.encode({name: value for name, value in claims.items() if value is not None}, key, algorithm=algorithm)


def test_valid_token_gives_the_user():
    assert accounts.verify_token(token()) == {"id": "user-1", "email": "a@example.com"}


@pytest.mark.parametrize("bad", [
    pytest.param(lambda: token(OTHER_KEY), id="signed by another key"),
    pytest.param(lambda: token(exp=int(time.time()) - 10), id="expired"),
    pytest.param(lambda: token(aud="anon"), id="wrong audience"),
    pytest.param(lambda: token(iss="https://evil.example/auth/v1"), id="wrong issuer"),
    pytest.param(lambda: token(exp=None), id="no expiry"),
    pytest.param(lambda: jwt.encode({"sub": "user-1"}, "secret-value-long-enough-for-hs256-testing", algorithm="HS256"), id="shared secret"),
    pytest.param(lambda: jwt.encode({"sub": "user-1"}, None, algorithm="none"), id="unsigned"),
    pytest.param(lambda: "not-a-token", id="garbage"),
])
def test_bad_tokens_are_rejected(bad):
    assert accounts.verify_token(bad()) is None


def test_hosted_api_requires_sign_in(monkeypatch):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    assert client.get("/api/projects").status_code == 401


def test_forged_cookie_is_rejected(monkeypatch):
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    response = client.get("/api/projects", cookies={"vyral_access": token(OTHER_KEY)})
    assert response.status_code == 401


def test_local_mode_does_not_need_sign_in():
    assert client.get("/api/projects").status_code == 200


def test_settings_are_admin_only(monkeypatch, hosted_admin):
    monkeypatch.setenv("VYRAL_ADMIN_EMAILS", "someone-else@example.com")
    body = {"script_provider": "groq", "groq_model": "m", "groq_fallback": True}
    assert client.put("/api/settings", json=body).status_code == 403
    assert client.get("/api/settings/groq-models").status_code == 403
    visible = client.get("/api/settings").json()
    assert "has_groq_key" not in visible and "anthropic_spent_usd" not in visible


def test_admin_check_uses_the_exact_email(monkeypatch):
    monkeypatch.setenv("VYRAL_ADMIN_EMAILS", "Admin@Example.com, other@example.com")
    identity.set_user({"id": "1", "email": "admin@example.com"})
    assert identity.is_admin()
    identity.set_user({"id": "1", "email": "admin@example.com.evil.test"})
    assert not identity.is_admin()
    identity.set_user(None)
    assert not identity.is_admin()
