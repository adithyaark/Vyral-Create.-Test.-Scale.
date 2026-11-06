"""Private cloud file storage for hosted mode, using Supabase Storage."""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import quote

import httpx

from . import identity

BUCKET = "vyral-media"
TIMEOUT_SECONDS = 300
SIGNED_URL_SECONDS = 3600
LIST_PAGE = 1000


class StorageError(RuntimeError):
    pass


def _base() -> str:
    return f"{os.environ['SUPABASE_URL'].rstrip('/')}/storage/v1"


def _headers() -> dict:
    # The secret key stays on the server. Browsers only ever get short-lived links.
    return {"apikey": os.environ["SUPABASE_SECRET_KEY"]}


def _check(response: httpx.Response, action: str) -> httpx.Response:
    if not response.is_success:
        if response.status_code == 413:
            raise StorageError("That file is too large to store.")
        raise StorageError(f"Cloud storage couldn't {action} (error {response.status_code}).")
    return response


def _key(user_id: str, project_id: str, sub: str, name: str) -> str:
    return f"users/{user_id}/projects/{project_id}/{sub}/{name}"


def _owner() -> str:
    user = identity.current()
    if not user:
        raise StorageError("No signed-in user for this file.")
    return user["id"]


def _object_url(key: str) -> str:
    return f"{_base()}/object/{BUCKET}/{quote(key)}"


def ensure_bucket(max_file_mb: int) -> None:
    body = {"id": BUCKET, "name": BUCKET, "public": False, "file_size_limit": max_file_mb * 1024 * 1024}
    response = httpx.get(f"{_base()}/bucket/{BUCKET}", headers=_headers(), timeout=TIMEOUT_SECONDS)
    if response.is_success:
        _check(httpx.put(f"{_base()}/bucket/{BUCKET}", headers=_headers(), json=body, timeout=TIMEOUT_SECONDS),
               "update the bucket")
    else:
        _check(httpx.post(f"{_base()}/bucket", headers=_headers(), json=body, timeout=TIMEOUT_SECONDS),
               "create the bucket")


def put(project_id: str, sub: str, path: Path, content_type: str = "application/octet-stream") -> None:
    with path.open("rb") as handle:
        response = httpx.post(
            _object_url(_key(_owner(), project_id, sub, path.name)),
            headers={**_headers(), "Content-Type": content_type, "x-upsert": "true"},
            content=handle, timeout=TIMEOUT_SECONDS,
        )
    _check(response, "save the file")


def fetch(project_id: str, sub: str, name: str, target: Path) -> Path:
    """Download a file for processing unless it's already there."""
    if target.is_file():
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    with httpx.stream("GET", _object_url(_key(_owner(), project_id, sub, name)), headers=_headers(),
                      timeout=TIMEOUT_SECONDS) as response:
        if not response.is_success:
            raise StorageError(f"Cloud storage couldn't load a file (error {response.status_code}).")
        with partial.open("wb") as handle:
            for chunk in response.iter_bytes():
                handle.write(chunk)
    partial.replace(target)
    return target


def signed_url(project_id: str, sub: str, name: str, download: str | None = None) -> str:
    response = _check(httpx.post(
        f"{_base()}/object/sign/{BUCKET}/{quote(_key(_owner(), project_id, sub, name))}",
        headers=_headers(), json={"expiresIn": SIGNED_URL_SECONDS}, timeout=TIMEOUT_SECONDS,
    ), "create a link")
    url = f"{_base()}{response.json()['signedURL']}"
    if download:
        url += f"&download={quote(download)}"
    return url


def delete(project_id: str, sub: str, names: list[str]) -> None:
    owner = _owner()
    _delete_keys([_key(owner, project_id, sub, name) for name in names])


def _delete_keys(keys: list[str]) -> None:
    if keys:
        _check(httpx.request("DELETE", f"{_base()}/object/{BUCKET}", headers=_headers(),
                             json={"prefixes": keys}, timeout=TIMEOUT_SECONDS), "delete files")


def _walk(prefix: str) -> list[str]:
    """List every file under a folder. Storage reports folders as entries without an ID."""
    found: list[str] = []
    offset = 0
    while True:
        response = _check(httpx.post(
            f"{_base()}/object/list/{BUCKET}", headers=_headers(), timeout=TIMEOUT_SECONDS,
            json={"prefix": prefix, "limit": LIST_PAGE, "offset": offset},
        ), "list files")
        entries = response.json()
        for entry in entries:
            path = f"{prefix}/{entry['name']}"
            found.extend(_walk(path) if entry.get("id") is None else [path])
        if len(entries) < LIST_PAGE:
            return found
        offset += LIST_PAGE


def delete_project(project_id: str) -> None:
    _delete_keys(_walk(f"users/{_owner()}/projects/{project_id}"))


def delete_user_files(user_id: str) -> int:
    keys = _walk(f"users/{user_id}")
    # Removed in batches, as a user can have many files.
    for start in range(0, len(keys), 500):
        _delete_keys(keys[start:start + 500])
    return len(keys)
