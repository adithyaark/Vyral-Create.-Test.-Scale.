"""Project storage: one folder per project with a JSON file and its media."""
from __future__ import annotations

import json
import os
import re
import threading
import uuid
from pathlib import Path

from psycopg.types.json import Jsonb

from . import db, identity
from .tools import ROOT, is_hosted

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


def _owner() -> str:
    """Hosted data always belongs to someone, so no signed-in user means no access."""
    user = identity.current()
    if not user:
        raise NotFound("no signed-in user")
    return user["id"]


def _with_defaults(project: dict) -> dict:
    # Projects made before photo mode keep working with their video clips.
    project.setdefault("visual_mode", "clips")
    project.setdefault("brand_color", None)
    project.setdefault("photos", [])
    project.setdefault("scene_plans", {})
    project.setdefault("copy_source", None)
    return project


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
    if is_hosted():
        owner = _owner()
        with db.as_user(owner) as connection:
            connection.execute(
                "insert into vyral.projects (id, owner_id, name, data) values (%s, %s, %s, %s)",
                [project["id"], owner, name, Jsonb(project)])
        return project
    folder = project_dir(project["id"])
    for sub in ("clips", "photos", "recordings", "voice", "videos"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    save(project)
    return project


def load(project_id: str) -> dict:
    path = project_dir(project_id) / "project.json"
    if is_hosted():
        owner = _owner()
        with db.as_user(owner) as connection:
            row = connection.execute("select data from vyral.projects where id = %s and owner_id = %s",
                                     [project_id, owner]).fetchone()
        if not row:
            raise NotFound(project_id)
        return _with_defaults(row[0])
    with _lock:
        if not path.is_file():
            raise NotFound(project_id)
        project = json.loads(path.read_text(encoding="utf-8"))
    return _with_defaults(project)


def save(project: dict) -> None:
    folder = project_dir(project["id"])
    temp = folder / "project.json.tmp"
    with _lock:
        temp.write_text(json.dumps(project, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(folder / "project.json")


def update(project_id: str, change) -> dict:
    """Apply `change` to the latest saved project without losing concurrent edits."""
    if is_hosted():
        project_dir(project_id)
        owner = _owner()
        with db.as_user(owner) as connection:
            # The row stays locked until saved, so parallel edits take turns.
            row = connection.execute(
                "select data from vyral.projects where id = %s and owner_id = %s for update",
                [project_id, owner]).fetchone()
            if not row:
                raise NotFound(project_id)
            project = _with_defaults(row[0])
            change(project)
            connection.execute(
                "update vyral.projects set name = %s, data = %s, updated_at = now() where id = %s and owner_id = %s",
                [project["name"], Jsonb(project), project_id, owner])
        return project
    with _lock:
        project = load(project_id)
        change(project)
        save(project)
        return project


def count() -> int:
    """How many projects the signed-in user has. Only used in hosted mode."""
    owner = _owner()
    with db.as_user(owner) as connection:
        return connection.execute("select count(*) from vyral.projects where owner_id = %s", [owner]).fetchone()[0]


def delete(project_id: str) -> None:
    """Remove a hosted project's record. Local projects are removed with their folder."""
    project_dir(project_id)
    owner = _owner()
    with db.as_user(owner) as connection:
        connection.execute("delete from vyral.projects where id = %s and owner_id = %s", [project_id, owner])


def list_all() -> list[dict]:
    if is_hosted():
        owner = _owner()
        with db.as_user(owner) as connection:
            rows = connection.execute(
                "select id, name, jsonb_array_length(coalesce(data->'videos', '[]'::jsonb)) from vyral.projects "
                "where owner_id = %s order by updated_at desc", [owner]).fetchall()
        return [{"id": row[0], "name": row[1], "videos": row[2]} for row in rows]
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
