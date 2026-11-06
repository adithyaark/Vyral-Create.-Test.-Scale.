"""Who the current request or background job is working for."""
from __future__ import annotations

import contextvars
import os

_user: contextvars.ContextVar[dict | None] = contextvars.ContextVar("vyral_user", default=None)


def set_user(user: dict | None) -> None:
    _user.set(user)


def current() -> dict | None:
    return _user.get()


def is_admin() -> bool:
    """Admins are listed by verified email in the server's environment."""
    user = current()
    if not user or not user.get("email"):
        return False
    admins = {email.strip().lower() for email in os.environ.get("VYRAL_ADMIN_EMAILS", "").split(",") if email.strip()}
    return user["email"].lower() in admins
