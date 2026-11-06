import pytest


@pytest.fixture
def hosted_admin(monkeypatch):
    """Act as a signed-in hosted administrator, without contacting Supabase."""
    from vyral import app as app_module

    async def identify(_request):
        return {"id": "00000000-0000-0000-0000-000000000001", "email": "admin@example.com"}, None

    monkeypatch.setattr(app_module, "_identify", identify)
    monkeypatch.setenv("VYRAL_HOSTED", "1")
    monkeypatch.setenv("VYRAL_ADMIN_EMAILS", "admin@example.com")
