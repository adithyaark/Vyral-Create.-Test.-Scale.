"""Sign-in through Supabase Auth, token checks, and per-user monthly limits."""
from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx
import jwt

from . import db

TIMEOUT_SECONDS = 20
MIN_PASSWORD_LENGTH = 8
# Only public-key algorithms are accepted, so a token can't be forged with a shared secret.
TOKEN_ALGORITHMS = ["ES256", "RS256"]
JWKS_CACHE_SECONDS = 600
_jwks_client: jwt.PyJWKClient | None = None

KNOWN_ERRORS = {
    "invalid_credentials": "That email or password is incorrect.",
    "email_not_confirmed": "Confirm your email first, using the link we sent you.",
    "weak_password": "Choose a stronger password.",
    "same_password": "Choose a password that's different from your current one.",
    "over_email_send_rate_limit": "Too many emails were sent recently. Please wait a while and try again.",
    "over_request_rate_limit": "Too many attempts. Please wait a while and try again.",
    "signup_disabled": "New sign-ups are turned off right now.",
    "email_address_invalid": "Enter a valid email address.",
    "validation_failed": "Check your email and password, then try again.",
    "session_expired": "That link has expired. Please request a new one.",
    "bad_jwt": "That link has expired. Please request a new one.",
}


class AuthError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _url() -> str:
    return os.environ["SUPABASE_URL"].rstrip("/")


def _public_headers() -> dict:
    return {"apikey": os.environ["SUPABASE_PUBLISHABLE_KEY"]}


def _secret_headers() -> dict:
    return {"apikey": os.environ["SUPABASE_SECRET_KEY"]}


def _call(method: str, path: str, **kwargs) -> dict:
    try:
        response = httpx.request(method, f"{_url()}/auth/v1{path}", timeout=TIMEOUT_SECONDS, **kwargs)
    except httpx.HTTPError as exc:
        raise AuthError("Couldn't reach the sign-in service. Please try again.", 503) from exc
    if response.is_success:
        return response.json() if response.content else {}
    try:
        code = response.json().get("error_code") or response.json().get("code")
    except ValueError:
        code = None
    if response.status_code == 429 and code not in KNOWN_ERRORS:
        code = "over_request_rate_limit"
    # Other replies from Supabase aren't shown, in case they contain internal details.
    message = KNOWN_ERRORS.get(code, "Sign-in couldn't be completed. Please try again.")
    raise AuthError(message, 429 if response.status_code == 429 else 400)


def _jwks() -> jwt.PyJWKClient:
    global _jwks_client
    if _jwks_client is None:
        _jwks_client = jwt.PyJWKClient(
            f"{_url()}/auth/v1/.well-known/jwks.json", cache_keys=True, lifespan=JWKS_CACHE_SECONDS,
            headers={"User-Agent": "Vyral/1.0"}, timeout=TIMEOUT_SECONDS,
        )
    return _jwks_client


def verify_token(token: str) -> dict | None:
    """Return the user for a valid access token, or None for anything else."""
    try:
        key = _jwks().get_signing_key_from_jwt(token)
        claims = jwt.decode(
            token, key.key, algorithms=TOKEN_ALGORITHMS, audience="authenticated",
            issuer=f"{_url()}/auth/v1", options={"require": ["exp", "sub", "iss", "aud"]},
        )
    except (jwt.PyJWTError, ValueError):
        return None
    return {"id": claims["sub"], "email": claims.get("email", "")}


def sign_in(email: str, password: str) -> dict:
    return _call("POST", "/token", params={"grant_type": "password"}, headers=_public_headers(),
                 json={"email": email, "password": password})


def refresh(refresh_token: str) -> dict:
    return _call("POST", "/token", params={"grant_type": "refresh_token"}, headers=_public_headers(),
                 json={"refresh_token": refresh_token})


def sign_up(email: str, password: str, redirect_to: str) -> dict:
    return _call("POST", "/signup", params={"redirect_to": redirect_to}, headers=_public_headers(),
                 json={"email": email, "password": password})


def request_password_reset(email: str, redirect_to: str) -> None:
    _call("POST", "/recover", params={"redirect_to": redirect_to}, headers=_public_headers(), json={"email": email})


def change_password(access_token: str, password: str) -> None:
    _call("PUT", "/user", headers={**_public_headers(), "Authorization": f"Bearer {access_token}"},
          json={"password": password})


def sign_out(access_token: str) -> None:
    _call("POST", "/logout", headers={**_public_headers(), "Authorization": f"Bearer {access_token}"})


def delete_user(user_id: str) -> None:
    _call("DELETE", f"/admin/users/{user_id}", headers=_secret_headers())


def _limit(name: str, default: int) -> int:
    try:
        return max(int(os.environ.get(name, default)), 0)
    except ValueError:
        return default


def limits() -> dict:
    return {
        "videos": _limit("VYRAL_MONTHLY_VIDEOS", 20),
        "ai_requests": _limit("VYRAL_MONTHLY_AI_REQUESTS", 50),
        "projects": _limit("VYRAL_MAX_PROJECTS", 25),
        "upload_mb": _limit("VYRAL_MAX_UPLOAD_MB", 50),
        "files_per_project": _limit("VYRAL_MAX_FILES_PER_PROJECT", 60),
    }


def _month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def usage(user_id: str) -> dict:
    with db.as_server() as connection:
        row = connection.execute("select videos, ai_requests from vyral.usage where owner_id = %s and month = %s",
                                 [user_id, _month()]).fetchone()
    return {"videos": row[0] if row else 0, "ai_requests": row[1] if row else 0}


def consume(user_id: str, kind: str, amount: int = 1) -> bool:
    """Atomically use part of the monthly allowance, so parallel requests can't exceed it."""
    assert kind in ("videos", "ai_requests")
    allowance = limits()[kind]
    if amount > allowance:
        return False
    # The column name comes from the fixed list checked above.
    query = f"""
        insert into vyral.usage (owner_id, month, {kind}) values (%(user)s, %(month)s, %(amount)s)
        on conflict (owner_id, month) do update set {kind} = vyral.usage.{kind} + %(amount)s
        where vyral.usage.{kind} + %(amount)s <= %(allowance)s
        returning {kind}
    """
    with db.as_server() as connection:
        row = connection.execute(query, {"user": user_id, "month": _month(), "amount": amount,
                                         "allowance": allowance}).fetchone()
    return row is not None


def release(user_id: str, kind: str, amount: int = 1) -> None:
    """Give allowance back when work fails, so users aren't charged for it."""
    assert kind in ("videos", "ai_requests")
    if amount <= 0:
        return
    with db.as_server() as connection:
        connection.execute(
            f"update vyral.usage set {kind} = greatest({kind} - %s, 0) where owner_id = %s and month = %s",
            [amount, user_id, _month()])
