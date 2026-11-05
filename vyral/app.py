"""Vyral's local web server. It only listens on this computer."""
from __future__ import annotations

import re
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import groq, llm, motion, photos, render, scenes, settings, store, tools, voice, writers

STATIC = Path(__file__).parent / "static"
VIDEO_TYPES = {".mp4", ".mov", ".m4v", ".webm", ".mkv", ".avi"}
PHOTO_TYPES = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
AUDIO_TYPES = {".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac", ".webm"}
LOCAL_HOSTS = {"127.0.0.1", "localhost"}
MAX_VARIANTS = 10
REMOVE_ATTEMPTS = 10
REMOVE_WAIT_SECONDS = 0.2

app = FastAPI(title="Vyral")
app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(LOCAL_HOSTS))

# Rendering and speech models are memory-heavy, so jobs run one at a time.
executor = ThreadPoolExecutor(max_workers=1)
jobs: dict[str, dict] = {}


@app.middleware("http")
async def reject_other_websites(request: Request, call_next):
    """Stop other websites open in the browser from using this local server."""
    origin = request.headers.get("origin")
    if origin and urlparse(origin).hostname not in LOCAL_HOSTS:
        return JSONResponse({"detail": "Requests from other websites are not allowed."}, status_code=403)
    response = await call_next(request)
    # Makes the browser check for updated pages instead of running an old copy.
    if not request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


@app.exception_handler(store.NotFound)
async def handle_not_found(_request: Request, _exc: store.NotFound):
    return JSONResponse({"detail": "Project not found."}, status_code=404)


@app.exception_handler(Exception)
async def handle_unexpected(_request: Request, exc: Exception):
    return JSONResponse({"detail": f"Something went wrong: {exc}"}, status_code=500)


def start_job(kind: str, project_id: str, work) -> dict:
    job = {"id": uuid.uuid4().hex, "kind": kind, "project": project_id, "status": "queued",
           "message": "Waiting to start", "done": 0, "total": 0, "error": None}
    jobs[job["id"]] = job

    def runner():
        job["status"] = "running"
        try:
            work(job)
            job["status"] = "done"
            job["message"] = "Finished"
        except (llm.LLMError, render.RenderError, ValueError) as exc:
            job["status"] = "failed"
            job["error"] = str(exc)
        except Exception as exc:  # Reported to the UI so a job can't fail silently.
            job["status"] = "failed"
            job["error"] = f"Unexpected error: {exc}"

    executor.submit(runner)
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


class AppSettings(BaseModel):
    script_provider: str = Field(pattern="^(claude|groq)$")
    groq_model: str = Field(min_length=1, max_length=200)
    groq_fallback: bool


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


def _copy_texts(project: dict, approved_only: bool = False) -> dict[str, str]:
    """Map hook and script IDs to the words they speak."""
    texts = {item["id"]: scenes.item_text(item) for item in project["hooks"] + project["scripts"]
             if item["approved"] or not approved_only}
    # Blank hooks and scripts have nothing to plan or say.
    return {key: text for key, text in texts.items() if text or not approved_only}


@app.get("/api/settings")
def get_settings():
    return settings.public()


@app.put("/api/settings")
def save_settings(request: AppSettings):
    if request.script_provider == "groq" and not settings.load()["groq_key"]:
        raise HTTPException(400, "Add your Groq API key first.")
    return settings.public(settings.update(**request.model_dump()))


@app.put("/api/settings/groq-key")
def save_groq_key(request: GroqKey):
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
    return settings.public(settings.update(groq_key="", script_provider="claude"))


@app.get("/api/settings/groq-models")
def groq_models():
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
    return store.create(request.name.strip())


@app.get("/api/projects/{project_id}")
def get_project(project_id: str):
    return store.load(project_id)


@app.delete("/api/projects/{project_id}")
def delete_project(project_id: str):
    store.load(project_id)
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

    def work(job):
        result, source = writers.write_copy(product, job)
        _apply_copy(project_id, result, source)

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
    with path.open("wb") as target:
        while chunk := await upload.read(1024 * 1024):
            target.write(chunk)
    return file_id, path


@app.post("/api/projects/{project_id}/clips")
async def upload_clips(project_id: str, files: list[UploadFile] = File(...)):
    store.load(project_id)
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
        added.append({"id": file_id, "file": path.name, "name": upload.filename, "duration": round(duration, 2)})
    project = store.update(project_id, lambda item: item["clips"].extend(added))
    return {"project": project, "rejected": rejected}


@app.delete("/api/projects/{project_id}/clips/{clip_id}")
def delete_clip(project_id: str, clip_id: str):
    project = store.load(project_id)
    for clip in project["clips"]:
        if clip["id"] == clip_id:
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
    store.load(project_id)
    folder = store.project_dir(project_id) / "photos"
    added, rejected = [], []
    for upload in files:
        _, raw_path = await _save_upload(upload, folder, PHOTO_TYPES)
        photo_id = uuid.uuid4().hex[:12]
        try:
            width, height = photos.ingest(raw_path, folder / f"{photo_id}.jpg")
            photos.make_thumbnail(folder / f"{photo_id}.jpg", folder / f"{photo_id}_thumb.jpg")
            added.append({"id": photo_id, "name": upload.filename, "width": width, "height": height})
        except photos.PhotoError:
            rejected.append(upload.filename)
        finally:
            raw_path.unlink(missing_ok=True)
    project = store.update(project_id, lambda item: item["photos"].extend(added))
    return {"project": project, "rejected": rejected}


@app.delete("/api/projects/{project_id}/photos/{photo_id}")
def delete_photo(project_id: str, photo_id: str):
    def change(project):
        if photo_id not in {photo["id"] for photo in project["photos"]}:
            return
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
    return FileResponse(store.project_dir(project_id) / "photos" / f"{photo_id}_thumb.jpg", media_type="image/jpeg")


@app.post("/api/projects/{project_id}/plan-scenes")
def plan_scenes(project_id: str):
    project = store.load(project_id)
    items = _copy_texts(project, approved_only=True)
    if not project["photos"]:
        raise HTTPException(400, "Upload at least one product photo first.")
    if not any(key.startswith("H") for key in items) or not any(key.startswith("S") for key in items):
        raise HTTPException(400, "Approve at least one hook and one script first.")
    folder = store.project_dir(project_id) / "photos"
    thumbnails = {photo["id"]: folder / f"{photo['id']}_thumb.jpg" for photo in project["photos"]}

    def work(job):
        job["message"] = "Claude is matching your photos to the script"
        plans = scenes.plan_scenes(project["product"]["name"] or project["name"], items, thumbnails)
        store.update(project_id, lambda item: item["scene_plans"].update(plans))

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
    store.load(project_id)
    _, path = await _save_upload(file, store.project_dir(project_id) / "recordings", AUDIO_TYPES)
    try:
        render.probe_duration(path)
    except render.RenderError:
        path.unlink(missing_ok=True)
        raise HTTPException(400, "That file could not be read as audio.")

    def change(project):
        old = project["recordings"].get(key)
        if old:
            (store.project_dir(project_id) / "recordings" / old).unlink(missing_ok=True)
        project["recordings"][key] = path.name
    return store.update(project_id, change)


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

    folder = store.project_dir(project_id)
    clips = [{"path": str(folder / "clips" / clip["file"]), "duration": clip["duration"]}
             for clip in project["clips"]]
    photo_paths = {photo["id"]: folder / "photos" / f"{photo['id']}.jpg" for photo in project["photos"]}

    def work(job):
        job["total"] = len(request.variants)
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
    path = store.project_dir(project_id) / "videos" / name
    return FileResponse(path, media_type="video/mp4", filename=name if download else None)


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found.")
    return job


app.mount("/", StaticFiles(directory=STATIC, html=True), name="static")
