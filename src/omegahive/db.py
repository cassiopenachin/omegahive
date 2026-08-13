"""psycopg connection helpers + a tiny ordered-SQL migration runner.

Two helpers, because there are two credentials (migration 0003, the two-role scheme):
`connect()` is the READ path (`hive_reader`) and `connect_gateway()` is the WRITE path
(`hive_gateway`). Which one a call site uses is the code's statement about whether it
writes; whether those DSNs are actually different roles is the deployment's. A call site
that writes through connect() fails on privilege at the INSERT once a deployment has cut
over — loudly, at the boundary, which is the point of the split.

Migrations are numbered .sql files in migrations/. The runner tracks which have
been applied in a schema_migrations table and applies the rest in filename order,
each in its own transaction. Migrations are DDL and run as the database OWNER — a
third, operator-held credential that no long-running service carries.
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
    """The read path. Every consumer that does not emit uses this and nothing else."""
    url = database_url or get_settings().database_url
    return psycopg.connect(url)


def connect_gateway(database_url: str | None = None) -> psycopg.Connection:
    """The write path — the connection the gateway appends through.

    Falls back to the read DSN when OMEGAHIVE_GATEWAY_DATABASE_URL is unset, so a
    deployment that has not yet cut over (and every host test run, which owns its own
    scratch database) behaves exactly as it did under a single role. An explicit
    `database_url` still wins, which is what lets a test point both paths at one
    scratch database or at two deliberately different ones.
    """
    settings = get_settings()
    url = database_url or settings.gateway_database_url or settings.database_url
    return psycopg.connect(url)


def connect_owner(database_url: str | None = None) -> psycopg.Connection:
    """The schema path — the database owner, for DDL. `db-migrate` and nothing else.

    Same fallback rule as connect_gateway(): unset means the deployment has not cut over.
    """
    settings = get_settings()
    url = database_url or settings.owner_database_url or settings.database_url
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
