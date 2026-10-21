"""Connection factory and schema migration — the ONLY place sqlite3.connect is called.

Implemented by: increment 1, agent "store".

Rules (Windows-proof):
- `mode='rw'`: create if missing, `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`.
- `mode='ro'`: `file:...?mode=ro` URI (the file may still grow; used by the server for evidence.sqlite).
- `mode='immutable'`: `file:...?immutable=1` (frozen build files only; never for WAL databases that still change).
- Paths are converted to URIs with `Path.as_uri()` semantics on all platforms (drive letters, spaces, CJK).
- `row_factory = sqlite3.Row`; `check_same_thread=False` (callers keep one connection per thread).
- `migrate()` executes the .sql file (CREATE … IF NOT EXISTS) and sets `PRAGMA user_version` to the file's
  version; later versions will add `sql/migrations/<n>.sql` applied in order when user_version < n.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

SQL_DIR = Path(__file__).with_name("sql")
EVIDENCE_SCHEMA_VERSION = 1
USER_SCHEMA_VERSION = 1


def connect(path: str | Path, mode: str = "rw", busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    raise NotImplementedError("increment 1 / store")


def migrate(conn: sqlite3.Connection, schema_file: str, target_version: int) -> None:
    raise NotImplementedError("increment 1 / store")


def attach(conn: sqlite3.Connection, alias: str, path: str | Path, mode: str = "ro") -> None:
    raise NotImplementedError("increment 1 / store")


def open_evidence(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    """connect + migrate('evidence.sql') when mode == 'rw'."""
    raise NotImplementedError("increment 1 / store")


def open_user(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    raise NotImplementedError("increment 1 / store")
