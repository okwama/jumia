"""SQLite connection + migration runner. See Readme.md #6, #14 (WAL mode), #15."""

import sqlite3
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


def connect(db_path: str | Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (filename TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row[0] for row in conn.execute("SELECT filename FROM schema_migrations")}

    for path in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if path.name in applied:
            continue
        conn.executescript(path.read_text())
        conn.execute(
            "INSERT INTO schema_migrations (filename, applied_at) VALUES (?, datetime('now'))",
            (path.name,),
        )
        conn.commit()


def get_connection(db_path: str | Path) -> sqlite3.Connection:
    """Shared by the CLI and dashboard: ensure the parent dir exists,
    connect, apply pending migrations. Callers own a short-lived
    connection per request/command -- see dashboard/app.py's note on why
    that's simpler than sharing one connection across threads."""
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(db_path)
    migrate(conn)
    return conn
