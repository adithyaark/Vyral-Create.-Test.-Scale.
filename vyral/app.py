"""Vyral's web server: single-user on your PC, or multi-user in hosted mode."""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import shutil
import time
import traceback
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from fastapi.concurrency import run_in_threadpool

from . import (
    accounts, blobs, claude_api, groq, identity, llm, motion, photos, render, scenes, settings, store, tools, voice,
    writers,
)

STATIC = Path(__file__).parent / "static"
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
AUDIO_TYPES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_VARIANTS = 10
REMOVE_ATTEMPTS = 10
REMOVE_WAIT_SECONDS = 0.2

ACCESS_COOKIE = "vyral_access"
REFRESH_COOKIE = "vyral_refresh"
REFRESH_COOKIE_SECONDS = 60 * 60 * 24 * 30
# These work without a session, because they're how people sign in or check if they have.
OPEN_API_PATHS = {"/api/auth/me", "/api/auth/signup", "/api/auth/login", "/api/auth/forgot",
                  "/api/auth/session", "/api/auth/logout"}
hosted = tools.is_hosted


def _public_url() -> str:
    return os.environ.get("VYRAL_PUBLIC_URL", "http://localhost:8765").rstrip("/")


def _allowed_hosts() -> set[str]:
    host = urlparse(_public_url()).hostname
    return LOCAL_HOSTS | ({host} if host else set())


app = FastAPI(title="Vyral")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(_allowed_hosts()))

# Rendering and speech models are memory-heavy, so jobs run one at a time.
executor = ThreadPoolExecutor(max_workers=1)
jobs: dict[str, dict] = {}
owners: dict[str, str | None] = {}


@app.middleware("http")
async def reject_other_websites(request: Request, call_next):
    """Stop other websites open in the browser from using this local server."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in _allowed_hosts():
        return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)

    path = request.url.path
    fresh_session = None
    identity.set_user(None)
    if hosted() and path.startswith("/api/"):
        user, fresh_session = await _identify(request)
        identity.set_user(user)
        request.state.access_token = (fresh_session or {}).get("access_token") or request.cookies.get(ACCESS_COOKIE)
        if not user and path not in OPEN_API_PATHS:
            response = JSONResponse({"detail": "Please sign in."}, status_code=401)
            _clear_session(response)
            return response

    response = await call_next(request)
    if fresh_session and not getattr(request.state, "session_changed", False):
        _set_session(response, fresh_session)
    # Makes the browser check for updated pages instead of running an old copy.
    if not path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    elif hosted():
        response.headers.setdefault("Cache-Control", "no-store")
    return response


async def _identify(request: Request) -> tuple[dict | None, dict | None]:
    """Find the signed-in user from their cookies, renewing an expired session if possible."""
    access = request.cookies.get(ACCESS_COOKIE)
    if access:
        user = await asyncio.to_thread(accounts.verify_token, access)
        if user:
            return user, None
    refresh_token = request.cookies.get(REFRESH_COOKIE)
    if refresh_token:
        try:
            session = await asyncio.to_thread(accounts.refresh, refresh_token)
            user = await asyncio.to_thread(accounts.verify_token, session.get("access_token", ""))
            if user:
                return user, session
        except accounts.AuthError:
            pass
    return None, None


def _set_session(response: Response, session: dict) -> None:
    # HttpOnly keeps tokens away from page scripts, and SameSite limits cross-site use.
    options = {"httponly": True, "samesite": "lax", "path": "/", "secure": _public_url().startswith("https://")}
    response.set_cookie(ACCESS_COOKIE, session["access_token"], max_age=int(session.get("expires_in", 3600)), **options)
    if session.get("refresh_token"):
        response.set_cookie(REFRESH_COOKIE, session["refresh_token"], max_age=REFRESH_COOKIE_SECONDS, **options)


def _clear_session(response: Response) -> None:
    response.delete_cookie(ACCESS_COOKIE, path="/")
    response.delete_cookie(REFRESH_COOKIE, path="/")


@app.exception_handler(accounts.AuthError)
async def handle_auth_error(_request: Request, exc: accounts.AuthError):
    return JSONResponse({"detail": str(exc)}, status_code=exc.status)


@app.exception_handler(blobs.StorageError)
async def handle_storage_error(_request: Request, exc: blobs.StorageError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(store.NotFound)
async def handle_not_found(_request: Request, _exc: store.NotFound):
    return JSONResponse({"detail": "Project not found."}, status_code=404)


@app.exception_handler(Exception)
async def handle_unexpected(_request: Request, exc: Exception):
    if hosted():
        # Details go to the server log, because they can reveal internal information.
        traceback.print_exception(exc)
        return JSONResponse({"detail": "Something went wrong. Please try again."}, status_code=500)
    return JSONResponse({"detail": f"Something went wrong: {exc}"}, status_code=500)


def start_job(kind: str, project_id: str, work) -> dict:
    job = {"id": uuid.uuid4().hex, "kind": kind, "project": project_id, "status": "queued",
           "message": "Waiting to start", "done": 0, "total": 0, "error": None}
    jobs[job["id"]] = job
    user = identity.current()
    owners[job["id"]] = user["id"] if user else None

    def runner():
        job["status"] = "running"
        try:
            work(job)
            job["status"] = "done"
            job["message"] = "Finished"
        except (llm.LLMError, render.RenderError, blobs.StorageError, ValueError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        except Exception as exc:  # Reported to the UI so a job can't fail silently.
            job["status"] = "failed"
            if hosted():
                traceback.print_exception(exc)
                job["error"] = "Something went wrong. Please try again."
            else:
                job["error"] = f"Unexpected error: {exc}"

    # The job runs on another thread, so it carries over who it's working for.
    executor.submit(contextvars.copy_context().run, runner)
    return job


class NewProject(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class Product(BaseModel):
    name: str = Field(default="", max_length=200)
    description: str = Field(default="", max_length=8000)
    price: str = Field(default="", max_length=100)
    audience: str = Field(default="", max_length=1000)
    facts: str = Field(default="", max_length=8000)
    reviews: str = Field(default="", max_length=30000)
    link: str = Field(default="", max_length=1000)


class Hook(BaseModel):
    id: str
    text: str = Field(max_length=400)
    angle: str = ""
    approved: bool = False


class Script(BaseModel):
    id: str
    angle: str = ""
    body: str = Field(max_length=2000)
    cta: str = Field(default="", max_length=400)
    flags: list[str] = []
    approved: bool = False


class Copy(BaseModel):
    hooks: list[Hook]
    scripts: list[Script]


class Variant(BaseModel):
    script_id: str
    hook_id: str


class RenderRequest(BaseModel):
    variants: list[Variant] = Field(min_length=1, max_length=MAX_VARIANTS)
    voice_mode: str = Field(pattern="^(ai|own)$")
    voice: str = "af_heart"
    speed: float = Field(default=1.0, ge=0.7, le=1.4)


class Visuals(BaseModel):
    visual_mode: str = Field(pattern="^(photos|clips)$")
    brand_color: str | None = Field(default=None, pattern="^#[0-9a-fA-F]{6}$")


class Scene(BaseModel):
    text: str
    photo_id: str
    layout: str = Field(pattern=f"^({'|'.join(scenes.LAYOUTS)})$")
    motion: str = Field(pattern=f"^({'|'.join(scenes.MOTIONS)})$")
    headline: str = Field(default="", max_length=80)


class ScenePlan(BaseModel):
    text: str
    scenes: list[Scene]


class PastedCopy(BaseModel):
    text: str = Field(min_length=1, max_length=200000)


class GroqKey(BaseModel):
    key: str = Field(min_length=8, max_length=300)


class AnthropicKey(BaseModel):
    key: str = Field(min_length=8, max_length=300)


class AppSettings(BaseModel):
    script_provider: str = Field(pattern="^(claude|claude_api|groq)$")
    groq_model: str = Field(min_length=1, max_length=200)
    groq_fallback: bool
    anthropic_model: str | None = None
    anthropic_monthly_budget_usd: float | None = Field(default=None, ge=0, le=100000)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=200)


class ForgotPassword(BaseModel):
    email: str = Field(min_length=3, max_length=320)


class SessionTokens(BaseModel):
    access_token: str = Field(min_length=10, max_length=8000)
    refresh_token: str = Field(min_length=1, max_length=2000)


class NewPassword(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class ConfirmPassword(BaseModel):
    password: str = Field(min_length=1, max_length=200)


class Preview(BaseModel):
    voice: str
    speed: float = Field(default=1.0, ge=0.7, le=1.4)


def _remove_file(path: Path) -> None:
    """Delete a file, giving a browser that was streaming it time to let go."""
    for attempt in range(REMOVE_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            # Windows refuses to delete a file while anything has it open.
            if attempt < REMOVE_ATTEMPTS - 1:
                time.sleep(REMOVE_WAIT_SECONDS)
    raise HTTPException(409, f"{path.name} is in use. Close it and try again.")


def _save_to_cloud(project_id: str, sub: str, path: Path, content_type: str = "application/octet-stream") -> None:
    """On a hosted server, move a finished file to private storage."""
    if hosted():
        blobs.put(project_id, sub, path, content_type)
        path.unlink(missing_ok=True)


def _delete_from_cloud(project_id: str, sub: str, names: list[str]) -> None:
    if hosted():
        blobs.delete(project_id, sub, names)


def _job_folder(project_id: str) -> Path:
    """Where a job works: the project itself locally, or a temporary folder when hosted."""
    if not hosted():
        return store.project_dir(project_id)
    folder = store.PROJECTS.parent / "jobs" / uuid.uuid4().hex
    for sub in ("clips", "photos", "recordings", "voice", "videos"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    return folder


def _clean_job_folder(folder: Path) -> None:
    if hosted():
        shutil.rmtree(folder, ignore_errors=True)


def _use_ai_request() -> str | None:
    """Count a paid AI request against the user's allowance. Returns who to refund on failure."""
    user = identity.current()
    if not hosted() or not user:
        return None
    if not accounts.consume(user["id"], "ai_requests"):
        raise HTTPException(429, "You've used this month's AI requests. You can still write or paste scripts yourself.")
    return user["id"]


def _require_editable_key(name: str) -> None:
    """Keys from the server's environment can't be changed through the browser."""
    if settings.is_hosted():
        raise HTTPException(403, "API keys on a hosted server are set in its environment.")
    if settings.key_source(name) == "env":
        raise HTTPException(409, "This key is set in the server's environment. Change it there.")


def _copy_texts(project: dict, approved_only: bool = False) -> dict[str, str]:
    """Map hook and script IDs to the words they speak."""
    texts = {item["id"]: scenes.item_text(item) for item in project["hooks"] + project["scripts"]
             if item["approved"] or not approved_only}
    # Blank hooks and scripts have nothing to plan or say.
    return {key: text for key, text in texts.items() if text or not approved_only}


def _require_admin() -> None:
    """On a hosted server, only listed admins can change app-wide settings."""
    if hosted() and not identity.is_admin():
        raise HTTPException(403, "Only an administrator can change this.")


def _check_password_length(password: str) -> None:
    if len(password) < accounts.MIN_PASSWORD_LENGTH:
        raise HTTPException(400, f"Use a password of at least {accounts.MIN_PASSWORD_LENGTH} characters.")


@app.get("/api/auth/me")
def auth_me():
    user = identity.current()
    if not hosted():
        return {"hosted": False, "user": None}
    if not user:
        return {"hosted": True, "user": None}
    return {"hosted": True, "user": user, "is_admin": identity.is_admin(),
            "usage": accounts.usage(user["id"]), "limits": accounts.limits()}


def _require_hosted() -> None:
    if not hosted():
        raise HTTPException(404, "Accounts are only used on a hosted server.")


@app.post("/api/auth/signup")
def auth_signup(request: Credentials):
    _require_hosted()
    _check_password_length(request.password)
    accounts.sign_up(request.email.strip(), request.password, _public_url())
    # The same reply either way, so this can't be used to find out who has an account.
    return {"message": "Check your email for a link to confirm your account."}


@app.post("/api/auth/login")
def auth_login(request: Credentials, http_request: Request):
    _require_hosted()
    session = accounts.sign_in(request.email.strip(), request.password)
    response = JSONResponse({"ok": True})
    _set_session(response, session)
    http_request.state.session_changed = True
    return response


@app.post("/api/auth/forgot")
def auth_forgot(request: ForgotPassword):
    _require_hosted()
    accounts.request_password_reset(request.email.strip(), _public_url())
    return {"message": "If that email has an account, a reset link is on its way."}


@app.post("/api/auth/session")
def auth_session(request: SessionTokens, http_request: Request):
    """Start a session from an email link, after checking the token is genuine."""
    _require_hosted()
    if not accounts.verify_token(request.access_token):
        raise HTTPException(401, "That link has expired. Please request a new one.")
    response = JSONResponse({"ok": True})
    _set_session(response, {**request.model_dump(), "expires_in": 3600})
    http_request.state.session_changed = True
    return response


@app.post("/api/auth/logout")
def auth_logout(http_request: Request):
    _require_hosted()
    token = getattr(http_request.state, "access_token", None)
    if token and identity.current():
        try:
            accounts.sign_out(token)
        except accounts.AuthError:
            pass  # The cookies are still removed below, which signs this browser out.
    response = JSONResponse({"ok": True})
    _clear_session(response)
    http_request.state.session_changed = True
    return response


@app.post("/api/auth/password")
def auth_change_password(request: NewPassword, http_request: Request):
    _require_hosted()
    _check_password_length(request.password)
    accounts.change_password(http_request.state.access_token, request.password)
    return {"ok": True}


@app.delete("/api/account")
def delete_account(request: ConfirmPassword, http_request: Request):
    """Permanently delete the signed-in user's files, projects, usage and account."""
    _require_hosted()
    user = identity.current()
    try:
        accounts.sign_in(user["email"], request.password)
    except accounts.AuthError as exc:
        raise HTTPException(403 if exc.status == 400 else exc.status, "That password is incorrect.")
    if any(owner == user["id"] and jobs[job_id]["status"] in ("queued", "running")
           for job_id, owner in owners.items()):
        raise HTTPException(409, "Wait for your running jobs to finish, then try again.")
    project_ids = [project["id"] for project in store.list_all()]
    blobs.delete_user_files(user["id"])
    # Deleting the user also removes their database rows, through the table references.
    accounts.delete_user(user["id"])
    for project_id in project_ids:
        shutil.rmtree(store.project_dir(project_id), ignore_errors=True)
    response = JSONResponse({"ok": True})
    _clear_session(response)
    http_request.state.session_changed = True
    return response


@app.get("/api/settings")
def get_settings():
    if hosted() and not identity.is_admin():
        # Customers only need to know which service will write their scripts.
        values = settings.load()
        return {"hosted": True, "script_provider": values["script_provider"], "groq_model": values["groq_model"],
                "anthropic_model": values["anthropic_model"]}
    return settings.public()


@app.put("/api/settings")
def save_settings(request: AppSettings):
    _require_admin()
    current = settings.load()
    if request.script_provider == "groq" and not current["groq_key"]:
        raise HTTPException(400, "Add your Groq API key first.")
    if request.script_provider == "claude_api" and not current["anthropic_key"]:
        raise HTTPException(400, "Add your Anthropic API key first.")
    if request.script_provider == "claude" and settings.is_hosted():
        raise HTTPException(400, "The Claude plan isn't available on a hosted server.")
    if request.anthropic_model and request.anthropic_model not in settings.ANTHROPIC_PRICES:
        raise HTTPException(400, "Choose one of the listed Claude models.")
    changes = {name: value for name, value in request.model_dump().items() if value is not None}
    return settings.public(settings.update(**changes))


@app.put("/api/settings/groq-key")
def save_groq_key(request: GroqKey):
    _require_editable_key("groq_key")
    key = request.key.strip()
    if any(character.isspace() for character in key):
        raise HTTPException(400, "That doesn't look like an API key. Copy it again without spaces.")
    # Checked first, so a mistyped key is never saved.
    try:
        models = groq.list_models(key)
    except llm.LLMError as exc:
        raise HTTPException(400, str(exc))
    saved = settings.public(settings.update(groq_key=key, script_provider="groq"))
    return {**saved, "models": models}


@app.delete("/api/settings/groq-key")
def delete_groq_key():
    _require_editable_key("groq_key")
    current = settings.load()
    changes = {"groq_key": ""}
    if current["script_provider"] == "groq":
        changes["script_provider"] = "claude_api" if current["anthropic_key"] else "claude"
    return settings.public(settings.update(**changes))


@app.put("/api/settings/anthropic-key")
def save_anthropic_key(request: AnthropicKey):
    _require_editable_key("anthropic_key")
    key = request.key.strip()
    if any(character.isspace() for character in key):
        raise HTTPException(400, "That doesn't look like an API key. Copy it again without spaces.")
    # Checked first, so a mistyped key is never saved.
    try:
        claude_api.check_key(key, settings.load()["anthropic_model"])
    except llm.LLMError as exc:
        raise HTTPException(400, str(exc))
    return settings.public(settings.update(anthropic_key=key, script_provider="claude_api"))


@app.delete("/api/settings/anthropic-key")
def delete_anthropic_key():
    _require_editable_key("anthropic_key")
    current = settings.load()
    changes = {"anthropic_key": ""}
    if current["script_provider"] == "claude_api":
        changes["script_provider"] = "groq" if current["groq_key"] else "claude"
    return settings.public(settings.update(**changes))


@app.get("/api/settings/groq-models")
def groq_models():
    _require_admin()
    key = settings.load()["groq_key"]
    if not key:
        raise HTTPException(400, "Add your Groq API key first.")
    try:
        return {"models": groq.list_models(key)}
    except llm.LLMError as exc:
        raise HTTPException(400, str(exc))


@app.get("/api/status")
def get_status():
    return tools.status()


@app.get("/api/voices")
def get_voices():
    return voice.VOICES


@app.post("/api/voice-preview")
def voice_preview(request: Preview):
    if request.voice not in voice.VOICES:
        raise HTTPException(400, "Unknown voice.")
    preview_dir = store.PROJECTS.parent / "previews"
    preview_dir.mkdir(parents=True, exist_ok=True)
    out = preview_dir / f"{request.voice}.wav"
    voice.synthesize("Here's how this voice sounds on your next ad.", request.voice, request.speed, out)
    return FileResponse(out, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@app.get("/api/projects")
def list_projects():
    return store.list_all()


@app.post("/api/projects")
def create_project(request: NewProject):
    if hosted() and store.count() >= accounts.limits()["projects"]:
        raise HTTPException(429, "You've reached your project limit. Delete a project to make room.")
    return store.create(request.name.strip())


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return store.load(project_id)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    store.load(project_id)
    if hosted():
        blobs.delete_project(project_id)
        store.delete(project_id)
        return {"ok": True}
    # Renaming first means a file in use can't leave the project half deleted.
    folder = store.project_dir(project_id)
    trash = folder.with_name(f".deleted-{project_id}")
    try:
        folder.rename(trash)
    except PermissionError:
        raise HTTPException(409, "A file in this project is in use. Close it and try again.")
    shutil.rmtree(trash, ignore_errors=True)
    return {"ok": True}


@app.put("/api/projects/{project_id}/product")
def save_product(project_id: str, product: Product):
    def change(project):
        project["product"] = product.model_dump()
        if product.name.strip():
            project["name"] = product.name.strip()
    return store.update(project_id, change)


@app.put("/api/projects/{project_id}/copy")
def save_copy(project_id: str, copy: Copy):
    def change(project):
        project["hooks"] = [hook.model_dump() for hook in copy.hooks]
        project["scripts"] = [script.model_dump() for script in copy.scripts]
        # A plan is tied to exact wording, so edited copy needs planning again.
        current = _copy_texts(project)
        project["scene_plans"] = {key: plan for key, plan in project["scene_plans"].items()
                                  if current.get(key) == plan["text"]}
    return store.update(project_id, change)


@app.post("/api/projects/{project_id}/generate")
def generate(project_id: str):
    product = store.load(project_id)["product"]
    if not product["description"].strip():
        raise HTTPException(400, "Add a product description first.")

    values = settings.load()
    if values["script_provider"] == "groq" and not values["groq_key"]:
        raise HTTPException(400, "Add your Groq API key in Settings first.")
    if values["script_provider"] == "claude_api" and not values["anthropic_key"]:
        raise HTTPException(400, "Add your Anthropic API key in Settings first.")

    billed_user = _use_ai_request()

    def work(job):
        try:
            result, source = writers.write_copy(product, job)
            _apply_copy(project_id, result, source)
        except Exception:
            if billed_user:
                accounts.release(billed_user, "ai_requests")
            raise

    return start_job("generate", project_id, work)


def _apply_copy(project_id: str, result: dict, source: str) -> dict:
    def change(project):
        project["brief"] = result["brief"]
        project["copy_source"] = source
        project["hooks"] = [{"id": f"H{i}", **hook, "approved": False}
                            for i, hook in enumerate(result["hooks"], 1)]
        project["scripts"] = [{"id": f"S{i}", **script, "approved": False}
                              for i, script in enumerate(result["scripts"], 1)]
        project["scene_plans"] = {}
    return store.update(project_id, change)


@app.get("/api/projects/{project_id}/script-prompt")
def script_prompt(project_id: str):
    product = store.load(project_id)["product"]
    if not product["description"].strip():
        raise HTTPException(400, "Add a product description in the Product step first.")
    return {"prompt": llm.full_prompt(product)}


@app.post("/api/projects/{project_id}/import-copy")
def import_copy(project_id: str, pasted: PastedCopy):
    product = store.load(project_id)["product"]
    try:
        result = llm.check_claims(llm.parse_pasted(pasted.text), product)
    except llm.LLMError as exc:
        raise HTTPException(400, str(exc))
    return _apply_copy(project_id, result, "Another AI")


async def _save_upload(upload: UploadFile, folder: Path, allowed: set[str]) -> tuple[str, Path]:
    # Stored under a generated name, so the uploaded filename can't choose the path.
    suffix = Path(upload.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, f"Unsupported file type: {suffix or 'none'}")
    # Projects made by older versions may not have every folder yet.
    folder.mkdir(parents=True, exist_ok=True)
    file_id = uuid.uuid4().hex[:12]
    path = folder / f"{file_id}{suffix}"
    limit_mb = accounts.limits()["upload_mb"] if hosted() else None
    written = 0
    with path.open("wb") as target:
        while chunk := await upload.read(1024 * 1024):
            written += len(chunk)
            if limit_mb is not None and written > limit_mb * 1024 * 1024:
                target.close()
                path.unlink(missing_ok=True)
                raise HTTPException(413, f"{upload.filename} is over the {limit_mb} MB limit.")
            target.write(chunk)
    return file_id, path


def _check_file_count(project: dict, adding: int) -> None:
    if not hosted():
        return
    existing = len(project["clips"]) + len(project["photos"]) + len(project["recordings"])
    if existing + adding > accounts.limits()["files_per_project"]:
        raise HTTPException(429, "This project has reached its file limit. Remove some files first.")


@app.post("/api/projects/{project_id}/clips")
async def upload_clips(project_id: str, files: list[UploadFile] = File(...)):
    _check_file_count(await run_in_threadpool(store.load, project_id), len(files))
    folder = store.project_dir(project_id) / "clips"
    added, rejected = [], []
    for upload in files:
        file_id, path = await _save_upload(upload, folder, VIDEO_TYPES)
        try:
            duration = render.probe_duration(path)
        except render.RenderError:
            path.unlink(missing_ok=True)
            rejected.append(upload.filename)
            continue
        await run_in_threadpool(_save_to_cloud, project_id, "clips", path)
        added.append({"id": file_id, "file": path.name, "name": upload.filename, "duration": round(duration, 2)})
    project = await run_in_threadpool(store.update, project_id, lambda item: item["clips"].extend(added))
    return {"project": project, "rejected": rejected}


@app.delete("/api/projects/{project_id}/clips/{clip_id}")
def delete_clip(project_id: str, clip_id: str):
    project = store.load(project_id)
    for clip in project["clips"]:
        if clip["id"] == clip_id:
            _delete_from_cloud(project_id, "clips", [clip["file"]])
            _remove_file(store.project_dir(project_id) / "clips" / clip["file"])

    def change(item):
        item["clips"] = [clip for clip in item["clips"] if clip["id"] != clip_id]
    return store.update(project_id, change)


@app.put("/api/projects/{project_id}/visuals")
def save_visuals(project_id: str, visuals: Visuals):
    def change(project):
        project["visual_mode"] = visuals.visual_mode
        project["brand_color"] = visuals.brand_color
    return store.update(project_id, change)


@app.post("/api/projects/{project_id}/photos")
async def upload_photos(project_id: str, files: list[UploadFile] = File(...)):
    _check_file_count(await run_in_threadpool(store.load, project_id), len(files))
    folder = store.project_dir(project_id) / "photos"
    added, rejected = [], []
    for upload in files:
        _, raw_path = await _save_upload(upload, folder, PHOTO_TYPES)
        photo_id = uuid.uuid4().hex[:12]
        try:
            width, height = photos.ingest(raw_path, folder / f"{photo_id}.jpg")
            photos.make_thumbnail(folder / f"{photo_id}.jpg", folder / f"{photo_id}_thumb.jpg")
            await run_in_threadpool(_save_to_cloud, project_id, "photos", folder / f"{photo_id}.jpg", "image/jpeg")
            await run_in_threadpool(_save_to_cloud, project_id, "photos", folder / f"{photo_id}_thumb.jpg", "image/jpeg")
            added.append({"id": photo_id, "name": upload.filename, "width": width, "height": height})
        except photos.PhotoError:
            rejected.append(upload.filename)
        finally:
            raw_path.unlink(missing_ok=True)
    project = await run_in_threadpool(store.update, project_id, lambda item: item["photos"].extend(added))
    return {"project": project, "rejected": rejected}


@app.delete("/api/projects/{project_id}/photos/{photo_id}")
def delete_photo(project_id: str, photo_id: str):
    def change(project):
        if photo_id not in {photo["id"] for photo in project["photos"]}:
            return
        _delete_from_cloud(project_id, "photos", [f"{photo_id}.jpg", f"{photo_id}_thumb.jpg"])
        for path in (store.project_dir(project_id) / "photos").glob(f"{photo_id}*"):
            path.unlink(missing_ok=True)
        project["photos"] = [photo for photo in project["photos"] if photo["id"] != photo_id]
        # Plans that used this photo must be made again.
        project["scene_plans"] = {key: plan for key, plan in project["scene_plans"].items()
                                  if all(scene["photo_id"] != photo_id for scene in plan["scenes"])}
    return store.update(project_id, change)


@app.get("/api/projects/{project_id}/photos/{photo_id}")
def get_photo(project_id: str, photo_id: str):
    project = store.load(project_id)
    # Only IDs recorded in the project are served, which rules out path tricks.
    if photo_id not in {photo["id"] for photo in project["photos"]}:
        raise HTTPException(404, "Photo not found.")
    if hosted():
        # A short-lived private link, created only after the ownership check above.
        return RedirectResponse(blobs.signed_url(project_id, "photos", f"{photo_id}_thumb.jpg"), status_code=307)
    return FileResponse(store.project_dir(project_id) / "photos" / f"{photo_id}_thumb.jpg", media_type="image/jpeg")


@app.post("/api/projects/{project_id}/plan-scenes")
def plan_scenes(project_id: str):
    project = store.load(project_id)
    items = _copy_texts(project, approved_only=True)
    if not project["photos"]:
        raise HTTPException(400, "Upload at least one product photo first.")
    if not any(key.startswith("H") for key in items) or not any(key.startswith("S") for key in items):
        raise HTTPException(400, "Approve at least one hook and one script first.")
    billed_user = _use_ai_request()
    job_folder = _job_folder(project_id)
    folder = job_folder / "photos"
    thumbnails = {photo["id"]: folder / f"{photo['id']}_thumb.jpg" for photo in project["photos"]}

    def work(job):
        try:
            job["message"] = "Claude is matching your photos to the script"
            if hosted():
                for path in thumbnails.values():
                    blobs.fetch(project_id, "photos", path.name, path)
            plans = writers.plan_scenes(project["product"]["name"] or project["name"], items, thumbnails)
            store.update(project_id, lambda item: item["scene_plans"].update(plans))
        except Exception:
            if billed_user:
                accounts.release(billed_user, "ai_requests")
            raise
        finally:
            _clean_job_folder(job_folder)

    return start_job("plan", project_id, work)


@app.post("/api/projects/{project_id}/auto-plan")
def auto_plan(project_id: str):
    project = store.load(project_id)
    items = _copy_texts(project, approved_only=True)
    if not project["photos"]:
        raise HTTPException(400, "Upload at least one product photo first.")
    if not any(key.startswith("H") for key in items) or not any(key.startswith("S") for key in items):
        raise HTTPException(400, "Approve at least one hook and one script first.")
    plans = scenes.auto_plan(items, [photo["id"] for photo in project["photos"]])
    return store.update(project_id, lambda item: item["scene_plans"].update(plans))


@app.put("/api/projects/{project_id}/scene-plans/{item_id}")
def save_scene_plan(project_id: str, item_id: str, plan: ScenePlan):
    def change(project):
        current = _copy_texts(project).get(item_id)
        known = {photo["id"] for photo in project["photos"]}
        if current is None or current != plan.text:
            raise HTTPException(409, "The script changed. Plan the scenes again.")
        if scenes.split_lines(current) != [scene.text for scene in plan.scenes]:
            raise HTTPException(400, "Scene lines must match the script.")
        if any(scene.photo_id not in known for scene in plan.scenes):
            raise HTTPException(400, "A scene uses a photo that no longer exists.")
        project["scene_plans"][item_id] = plan.model_dump()
    return store.update(project_id, change)


@app.post("/api/projects/{project_id}/recordings/{key}")
async def upload_recording(project_id: str, key: str, file: UploadFile = File(...)):
    if not re.fullmatch(r"S\d+-H\d+", key):
        raise HTTPException(400, "Invalid recording name.")
    _check_file_count(await run_in_threadpool(store.load, project_id), 1)
    _, path = await _save_upload(file, store.project_dir(project_id) / "recordings", AUDIO_TYPES)
    try:
        render.probe_duration(path)
    except render.RenderError:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "That file could not be read as audio.")
    name = path.name
    await run_in_threadpool(_save_to_cloud, project_id, "recordings", path)

    def change(project):
        old = project["recordings"].get(key)
        if old:
            _delete_from_cloud(project_id, "recordings", [old])
            (store.project_dir(project_id) / "recordings" / old).unlink(missing_ok=True)
        project["recordings"][key] = name
    return await run_in_threadpool(store.update, project_id, change)


def _spoken_text(hook: dict, script: dict) -> str:
    return " ".join(part.strip() for part in (hook["text"], script["body"], script["cta"]) if part.strip())


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "product"


@app.post("/api/projects/{project_id}/render")
def render_videos(project_id: str, request: RenderRequest):
    project = store.load(project_id)
    hooks = {hook["id"]: hook for hook in project["hooks"]}
    scripts = {script["id"]: script for script in project["scripts"]}
    use_photos = project["visual_mode"] == "photos"
    plans = project["scene_plans"]
    if use_photos and not project["photos"]:
        raise HTTPException(400, "Upload at least one product photo first.")
    if not use_photos and not project["clips"]:
        raise HTTPException(400, "Upload at least one clip first.")
    if request.voice_mode == "ai" and request.voice not in voice.VOICES:
        raise HTTPException(400, "Unknown voice.")
    for variant in request.variants:
        hook, script = hooks.get(variant.hook_id), scripts.get(variant.script_id)
        # Approval is enforced here as well as in the UI, so nothing renders unreviewed.
        if not hook or not script or not hook["approved"] or not script["approved"]:
            raise HTTPException(400, "Only approved hooks and scripts can be rendered.")
        if not hook["text"].strip() or not script["body"].strip():
            raise HTTPException(400, "Add the missing wording before rendering.")
        if use_photos and (variant.hook_id not in plans or variant.script_id not in plans):
            raise HTTPException(400, "Plan the scenes for the selected scripts and hooks first.")
        key = f"{variant.script_id}-{variant.hook_id}"
        if request.voice_mode == "own" and key not in project["recordings"]:
            raise HTTPException(400, f"Upload your recording for {key} first.")

    user = identity.current()
    billed_user = user["id"] if hosted() and user else None
    if billed_user and not accounts.consume(billed_user, "videos", len(request.variants)):
        raise HTTPException(429, "That would go over this month's video allowance. Check your usage in Settings.")

    folder = _job_folder(project_id)
    clips = [{"path": str(folder / "clips" / clip["file"]), "duration": clip["duration"]}
             for clip in project["clips"]]
    photo_paths = {photo["id"]: folder / "photos" / f"{photo['id']}.jpg" for photo in project["photos"]}

    def download_files() -> None:
        if use_photos:
            for path in photo_paths.values():
                blobs.fetch(project_id, "photos", path.name, path)
        else:
            for clip in project["clips"]:
                blobs.fetch(project_id, "clips", clip["file"], folder / "clips" / clip["file"])
        if request.voice_mode == "own":
            for variant in request.variants:
                name = project["recordings"][f"{variant.script_id}-{variant.hook_id}"]
                blobs.fetch(project_id, "recordings", name, folder / "recordings" / name)

    def work(job):
        job["total"] = len(request.variants)
        try:
            if hosted():
                job["message"] = "Getting your files ready"
                download_files()
            render_all(job)
        finally:
            # Failed videos aren't charged, and temporary copies aren't kept on the server.
            if billed_user:
                accounts.release(billed_user, "videos", job["total"] - job["done"])
            _clean_job_folder(folder)

    def render_all(job):
        for index, variant in enumerate(request.variants):
            key = f"{variant.script_id}-{variant.hook_id}"
            label = f"{key} ({index + 1} of {job['total']})"
            text = _spoken_text(hooks[variant.hook_id], scripts[variant.script_id])
            if request.voice_mode == "ai":
                job["message"] = f"Creating voiceover for {label}"
                audio = folder / "voice" / f"{key}.wav"
                voice.synthesize(text, request.voice, request.speed, audio)
                job["message"] = f"Timing captions for {label}"
                heard = voice.transcribe(audio)
                script_words = voice.align_script(text, heard, render.probe_duration(audio))
                caption_words = script_words
            else:
                # The speaker may stray from the script, so show what they actually said.
                audio = folder / "recordings" / project["recordings"][key]
                job["message"] = f"Creating captions for {label}"
                caption_words = voice.transcribe(audio, accurate=True)
                script_words = voice.align_script(text, caption_words, render.probe_duration(audio))
            if not caption_words:
                raise render.RenderError(f"No speech was detected for {key}.")
            name = f"vyral_{_slug(project['name'])}_{key}_{uuid.uuid4().hex[:6]}.mp4"
            out_path = folder / "videos" / name
            if use_photos:
                job["message"] = f"Animating {label}"
                scene_list = plans[variant.hook_id]["scenes"] + plans[variant.script_id]["scenes"]
                seconds = motion.render_photo_video(scene_list, photo_paths, audio, script_words, caption_words,
                                                    out_path, project["brand_color"])
            else:
                job["message"] = f"Rendering {label}"
                seconds = render.render_variant(clips, audio, caption_words, out_path, rotation=index)
            if hosted():
                job["message"] = f"Saving {label}"
            _save_to_cloud(project_id, "videos", out_path, "video/mp4")
            entry = {"file": name, "key": key, "seconds": round(seconds, 1), "voice_mode": request.voice_mode,
                     "voice": request.voice if request.voice_mode == "ai" else None}
            store.update(project_id, lambda item, entry=entry: item["videos"].insert(0, entry))
            job["done"] = index + 1

    return start_job("render", project_id, work)


@app.delete("/api/projects/{project_id}/videos/{name}")
def delete_video(project_id: str, name: str):
    project = store.load(project_id)
    # Only names recorded in the project are deleted, which rules out path tricks.
    if name not in {video["file"] for video in project["videos"]}:
        raise HTTPException(404, "Video not found.")
    _delete_from_cloud(project_id, "videos", [name])
    _remove_file(store.project_dir(project_id) / "videos" / name)

    def change(item):
        item["videos"] = [video for video in item["videos"] if video["file"] != name]
    return store.update(project_id, change)


@app.get("/api/projects/{project_id}/videos/{name}")
def get_video(project_id: str, name: str, download: bool = False):
    project = store.load(project_id)
    # Only names recorded in the project are served, which rules out path tricks.
    if name not in {video["file"] for video in project["videos"]}:
        raise HTTPException(404, "Video not found.")
    if hosted():
        # A short-lived private link, created only after the ownership check above.
        return RedirectResponse(blobs.signed_url(project_id, "videos", name, name if download else None), status_code=307)
    path = store.project_dir(project_id) / "videos" / name
    return FileResponse(path, media_type="video/mp4", filename=name if download else None)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    user = identity.current()
    # Someone else's job looks exactly like one that doesn't exist.
    if not job or (hosted() and (not user or owners.get(job_id) != user["id"])):
        raise HTTPException(404, "Job not found.")
    return job


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
