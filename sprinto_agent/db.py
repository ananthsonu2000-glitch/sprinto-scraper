"""SQLite connection, migrations, and transactions (stdlib sqlite3)."""
from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path

from . import clock

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    if path != ":memory:":
        conn.execute("PRAGMA journal_mode=WAL")
    return conn


def migrate(conn: sqlite3.Connection) -> list[str]:
    conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations(version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    done = {r[0] for r in conn.execute("SELECT version FROM schema_migrations")}
    applied = []
    for f in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if f.stem in done:
            continue
        sql = f.read_text(encoding="utf-8")
        conn.executescript(
            "BEGIN;\n" + sql + f"\nINSERT INTO schema_migrations VALUES('{f.stem}','{clock.iso(clock.now())}');\nCOMMIT;"
        )
        applied.append(f.stem)
    return applied


@contextlib.contextmanager
def tx(conn: sqlite3.Connection):
    """Short IMMEDIATE transaction (takes the write lock up front). Do not nest."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
