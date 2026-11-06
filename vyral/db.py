"""Postgres connections for hosted mode."""
from __future__ import annotations

import atexit
import json
import os
import threading
from contextlib import contextmanager
from pathlib import Path

from psycopg_pool import ConnectionPool

_pool: ConnectionPool | None = None
_lock = threading.Lock()
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"


def _get_pool() -> ConnectionPool:
    global _pool
    with _lock:
        if _pool is None:
            # Kept small because the shared pooler limits connections. Prepared
            # statements stay off so this also works through a connection pooler.
            _pool = ConnectionPool(
                os.environ["SUPABASE_DB_URL"], min_size=0, max_size=4, timeout=20, open=True,
                check=ConnectionPool.check_connection,
                kwargs={"sslmode": "require", "prepare_threshold": None, "connect_timeout": 15},
            )
    return _pool


@contextmanager
def as_user(user_id: str):
    """Open a transaction where row-level security limits queries to this user."""
    with _get_pool().connection() as connection, connection.transaction():
        claims = json.dumps({"sub": user_id, "role": "authenticated"})
        # One statement, so distant servers don't pay for an extra round trip. Both
        # settings end with the transaction, so they can't leak to the next user.
        connection.execute(
            "select set_config('request.jwt.claims', %s, true), set_config('role', 'authenticated', true)", [claims])
        yield connection


@contextmanager
def as_server():
    """Open a transaction for server-only work, such as changing usage counts."""
    with _get_pool().connection() as connection, connection.transaction():
        yield connection


def close() -> None:
    global _pool
    with _lock:
        if _pool is not None:
            _pool.close()
            _pool = None


atexit.register(close)


def migrate() -> list[str]:
    applied = []
    for path in sorted(MIGRATIONS.glob("*.sql")):
        with as_server() as connection:
            connection.execute(path.read_text(encoding="utf-8"))
        applied.append(path.name)
    return applied
