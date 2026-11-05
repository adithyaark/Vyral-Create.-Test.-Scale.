"""Project storage: one folder per project with a JSON file and its media."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from .tools import ROOT

PROJECTS = Path(os.environ.get("VYRAL_DATA", ROOT / "data")) / "projects"
_lock = threading.RLock()
_ID = re.compile(r"^[a-f0-9]{12}$")


class NotFound(KeyError):
    pass


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def project_dir(project_id: str) -> Path:
    # IDs come from URLs, so the strict format also blocks path traversal.
    if not _ID.match(project_id):
        raise NotFound(project_id)
    return PROJECTS / project_id


def create(name: str) -> dict:
    project = {
        "id": new_id(),
        "name": name,
        "product": {"name": name, "description": "", "price": "", "audience": "",
                    "facts": "", "reviews": "", "link": ""},
        "brief": None,
        "copy_source": None,
        "hooks": [],
        "scripts": [],
        "visual_mode": "photos",
        "brand_color": None,
        "photos": [],
        "scene_plans": {},
        "clips": [],
        "recordings": {},
        "videos": [],
    }
    folder = project_dir(project["id"])
    for sub in ("clips", "photos", "recordings", "voice", "videos"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    save(project)
    return project


def load(project_id: str) -> dict:
    path = project_dir(project_id) / "project.json"
    with _lock:
        if not path.is_file():
            raise NotFound(project_id)
        project = json.loads(path.read_text(encoding="utf-8"))
    # Projects made before photo mode keep working with their video clips.
    project.setdefault("visual_mode", "clips")
    project.setdefault("brand_color", None)
    project.setdefault("photos", [])
    project.setdefault("scene_plans", {})
    project.setdefault("copy_source", None)
    return project


def save(project: dict) -> None:
    folder = project_dir(project["id"])
    temp = folder / "project.json.tmp"
    with _lock:
        temp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(folder / "project.json")


def update(project_id: str, change) -> dict:
    """Apply `change` to the latest saved project without losing concurrent edits."""
    with _lock:
        project = load(project_id)
        change(project)
        save(project)
        return project


def list_all() -> list[dict]:
    if not PROJECTS.is_dir():
        return []
    folders = sorted(PROJECTS.iterdir(), key=lambda item: item.stat().st_mtime, reverse=True)
    summaries = []
    for folder in folders:
        if (folder / "project.json").is_file():
            project = load(folder.name)
            summaries.append({"id": project["id"], "name": project["name"],
                              "videos": len(project["videos"])})
    return summaries
