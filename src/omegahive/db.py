"""psycopg connection helper + a tiny ordered-SQL migration runner.

Migrations are numbered .sql files in migrations/. The runner tracks which have
been applied in a schema_migrations table and applies the rest in filename order,
each in its own transaction.
"""

from __future__ import annotations

from pathlib import Path

import psycopg

from .config import get_settings

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_MIGRATIONS_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename   TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


def connect(database_url: str | None = None) -> psycopg.Connection:
    url = database_url or get_settings().database_url
    return psycopg.connect(url)


def _applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(_MIGRATIONS_TABLE)
        cur.execute("SELECT filename FROM schema_migrations")
        return {r[0] for r in cur.fetchall()}
    # commit handled by caller's transaction context


def migrate(conn: psycopg.Connection, migrations_dir: Path | None = None) -> list[str]:
    """Apply all pending migrations in filename order. Returns those applied."""
    directory = migrations_dir or MIGRATIONS_DIR
    files = sorted(p for p in directory.glob("*.sql"))

    with conn.transaction():
        done = _applied(conn)

    applied: list[str] = []
    for path in files:
        if path.name in done:
            continue
        sql = path.read_text()
        with conn.transaction():
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute(
                    "INSERT INTO schema_migrations (filename) VALUES (%s)", (path.name,)
                )
        applied.append(path.name)
    return applied
