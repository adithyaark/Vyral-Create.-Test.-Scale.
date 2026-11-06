"""Copy local projects into one user's account, without changing the local copies.

Preview:  uv run python -m vyral.import_local your@email.com
Import:   uv run python -m vyral.import_local your@email.com --apply
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from psycopg.types.json import Jsonb

from .tools import ROOT

SUBFOLDERS = ("clips", "photos", "recordings", "videos")


def find_user(email: str) -> dict | None:
    url = os.environ["SUPABASE_URL"].rstrip("/")
    headers = {"apikey": os.environ["SUPABASE_SECRET_KEY"]}
    page = 1
    while True:
        response = httpx.get(f"{url}/auth/v1/admin/users", params={"page": page, "per_page": 200},
                             headers=headers, timeout=30)
        response.raise_for_status()
        users = response.json().get("users", [])
        for user in users:
            if user.get("email", "").lower() == email.lower():
                return {"id": user["id"], "email": user["email"]}
        if len(users) < 200:
            return None
        page += 1


def _files_to_copy(folder: Path, project: dict) -> list[tuple[str, Path]]:
    """List the files a project refers to, which skips temporary and cached ones."""
    names = {
        "clips": [clip["file"] for clip in project.get("clips", [])],
        "photos": [name for photo in project.get("photos", [])
                   for name in (f"{photo['id']}.jpg", f"{photo['id']}_thumb.jpg")],
        "recordings": list(project.get("recordings", {}).values()),
        "videos": [video["file"] for video in project.get("videos", [])],
    }
    return [(sub, folder / sub / name) for sub in SUBFOLDERS for name in names[sub]]


def import_projects(user: dict, projects_root: Path, apply: bool) -> list[dict]:
    from . import blobs, db, identity

    identity.set_user(user)
    results = []
    for path in sorted(projects_root.glob("*/project.json")):
        folder = path.parent
        project = json.loads(path.read_text(encoding="utf-8"))
        files = _files_to_copy(folder, project)
        missing = [item.name for _, item in files if not item.is_file()]
        present = [(sub, item) for sub, item in files if item.is_file()]
        result = {"name": project["name"], "id": project["id"], "files": len(present),
                  "mb": round(sum(item.stat().st_size for _, item in present) / 1_000_000, 1),
                  "missing": len(missing), "status": "ready"}
        with db.as_server() as connection:
            row = connection.execute("select owner_id::text from vyral.projects where id = %s", [project["id"]]).fetchone()
        if row:
            result["status"] = "already imported" if row[0] == user["id"] else "belongs to another account"
        elif apply:
            # Files first, so a failed upload never leaves a project pointing at nothing.
            for sub, item in present:
                blobs.put(project["id"], sub, item, mimetypes.guess_type(item.name)[0] or "application/octet-stream")
            # Entries whose files are gone would only show broken items, so they're left out.
            project["videos"] = [video for video in project.get("videos", []) if (folder / "videos" / video["file"]).is_file()]
            project["clips"] = [clip for clip in project.get("clips", []) if (folder / "clips" / clip["file"]).is_file()]
            project["photos"] = [photo for photo in project.get("photos", []) if (folder / "photos" / f"{photo['id']}.jpg").is_file()]
            project["recordings"] = {key: name for key, name in project.get("recordings", {}).items()
                                     if (folder / "recordings" / name).is_file()}
            with db.as_user(user["id"]) as connection:
                connection.execute("insert into vyral.projects (id, owner_id, name, data) values (%s, %s, %s, %s)",
                                   [project["id"], user["id"], project["name"], Jsonb(project)])
            result["status"] = "imported"
        results.append(result)
    identity.set_user(None)
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email")
    parser.add_argument("--apply", action="store_true", help="copy the projects instead of only previewing")
    args = parser.parse_args()
    load_dotenv(ROOT / ".env", override=False)
    user = find_user(args.email)
    if not user:
        raise SystemExit("No account has that email. Sign up in Vyral first, then run this again.")
    from .store import PROJECTS
    for result in import_projects(user, PROJECTS, args.apply):
        print(f"{result['status']}: {result['name']} ({result['files']} files, {result['mb']} MB"
              f"{', ' + str(result['missing']) + ' missing' if result['missing'] else ''})")
    if not args.apply:
        print("This was a preview. Add --apply to copy these projects.")


if __name__ == "__main__":
    main()
