"""Connection factory and schema migration — the ONLY place sqlite3.connect is called.

Implemented by: increment 1, agent "store".

Rules (Windows-proof):
- `mode='rw'`: create if missing, `journal_mode=WAL`, `busy_timeout=5000`, `foreign_keys=ON`, `synchronous=NORMAL`.
- `mode='ro'`: `file:...?mode=ro` URI (the file may still grow; used by the server for evidence.sqlite).
- `mode='immutable'`: `file:...?immutable=1` (frozen build files only; never for WAL databases that still change).
- Paths are converted to URIs with `Path.as_uri()` semantics on all platforms (drive letters, spaces, CJK); a UNC
  path `\\\\server\\share\\db.sqlite` becomes `file:////server/share/db.sqlite` (empty authority — SQLite rejects any
  other URI authority than localhost).
- `row_factory = sqlite3.Row`; `check_same_thread=False` (callers keep one connection per thread).
- `migrate()` executes the .sql file (CREATE … IF NOT EXISTS) and sets `PRAGMA user_version` to the file's
  version; `sql/migrations/<n>.sql` are applied in order when user_version < n (user.sqlite: 2.sql).
- Never VACUUM evidence.sqlite: `sentences` and `page_text` have TEXT primary keys, so VACUUM may renumber
  their rowids, on which the external-content FTS tables are keyed. If it ever happens, run
  `EvidenceStore.rebuild_fts()`.

Connections are opened in autocommit mode (`isolation_level=None`); the stores wrap each write in an explicit
`BEGIN IMMEDIATE … COMMIT` through `transaction()`.
"""
from __future__ import annotations

import logging
import sqlite3
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

log = logging.getLogger(__name__)

SQL_DIR = Path(__file__).with_name("sql")
MIGRATIONS_DIR = SQL_DIR / "migrations"
EVIDENCE_SCHEMA_VERSION = 1
USER_SCHEMA_VERSION = 2     # 2: index on note_targets.resolved_ref (migrations/2.sql)
BUILD_SCHEMA_VERSION = 1    # build.sqlite (increment 2)

_MODES = ("rw", "ro", "immutable")


def _is_unc(p: Path) -> bool:
    return p.drive.startswith("\\\\") or p.drive.startswith("//")


def file_uri(path: str | Path, mode: str = "rw") -> str:
    """`file:` URI for an absolute path (percent-encoded: drive letters, spaces and CJK are safe) plus the
    query string for `mode`: rw → ?mode=rwc, ro → ?mode=ro, immutable → ?immutable=1. UNC paths get an empty
    authority: `file:////server/share/dir/db.sqlite`."""
    if mode not in _MODES:
        raise ValueError(f"unknown connect mode {mode!r} (expected one of {_MODES})")
    p = Path(path)
    if not p.is_absolute():
        p = p.resolve()
    if _is_unc(p):
        segs = [s for s in p.as_posix().split("/") if s]
        uri = "file:////" + "/".join(urllib.parse.quote(s, safe="") for s in segs)
    else:
        uri = p.as_uri()
    if mode == "rw":
        return uri + "?mode=rwc"
    if mode == "ro":
        return uri + "?mode=ro"
    return uri + "?immutable=1"


def connect(path: str | Path, mode: str = "rw", busy_timeout_ms: int = 5000) -> sqlite3.Connection:
    p = Path(path)
    if not p.is_absolute():
        p = p.resolve()
    if mode == "rw":
        p.parent.mkdir(parents=True, exist_ok=True)
    elif mode in ("ro", "immutable"):
        if not p.exists():
            raise FileNotFoundError(f"database does not exist (mode={mode}): {p}")
    uri = file_uri(p, mode)
    conn = sqlite3.connect(uri, uri=True, timeout=busy_timeout_ms / 1000.0, check_same_thread=False,
                           isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    if mode == "rw":
        jm = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(jm).lower() != "wal":
            log.warning("journal_mode WAL not available for %s (got %s)", p, jm)
        conn.execute("PRAGMA synchronous = NORMAL")
    log.debug("sqlite connect %s mode=%s", p, mode)
    return conn


def user_version(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def migrate(conn: sqlite3.Connection, schema_file: str, target_version: int) -> None:
    """Run `sql/<schema_file>` (idempotent CREATE … IF NOT EXISTS), then every `sql/migrations/<n>.sql` with
    current < n <= target_version in order, and set PRAGMA user_version = target_version."""
    current = user_version(conn)
    sql_path = SQL_DIR / schema_file
    script = sql_path.read_text(encoding="utf-8")
    conn.executescript(script)
    for n in range(current + 1, target_version + 1):
        mig = MIGRATIONS_DIR / f"{n}.sql"
        if mig.exists():
            log.info("applying migration %s", mig.name)
            conn.executescript(mig.read_text(encoding="utf-8"))
    if current != target_version:
        conn.execute(f"PRAGMA user_version = {int(target_version)}")
        log.debug("%s: user_version %d -> %d", schema_file, current, target_version)


def attach(conn: sqlite3.Connection, alias: str, path: str | Path, mode: str = "ro") -> None:
    """ATTACH another database file under `alias` (URI form, so the same path rules apply)."""
    if not alias.isidentifier():
        raise ValueError(f"bad attach alias {alias!r}")
    p = Path(path)
    if not p.is_absolute():
        p = p.resolve()
    if mode != "rw" and not p.exists():
        raise FileNotFoundError(f"database does not exist (mode={mode}): {p}")
    conn.execute(f"ATTACH DATABASE ? AS {alias}", (file_uri(p, mode),))


@contextmanager
def transaction(conn: sqlite3.Connection, immediate: bool = True) -> Iterator[sqlite3.Connection]:
    """`BEGIN IMMEDIATE … COMMIT` (ROLLBACK on error). Nested use joins the outer transaction."""
    if conn.in_transaction:
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def open_evidence(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    """connect + migrate('evidence.sql') when mode == 'rw'."""
    conn = connect(path, mode)
    if mode == "rw":
        migrate(conn, "evidence.sql", EVIDENCE_SCHEMA_VERSION)
    return conn


def open_user(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    conn = connect(path, mode)
    if mode == "rw":
        migrate(conn, "user.sql", USER_SCHEMA_VERSION)
    return conn


def open_build(path: str | Path, mode: str = "rw") -> sqlite3.Connection:
    """connect + migrate('build.sql') when mode == 'rw'. Finished builds are opened with mode='immutable'."""
    conn = connect(path, mode)
    if mode == "rw":
        migrate(conn, "build.sql", BUILD_SCHEMA_VERSION)
    return conn
