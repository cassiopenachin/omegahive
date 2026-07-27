"""One Postgres database per test run — the mechanism, shared by all three test paths.

Parallel workers run suites concurrently against the one stack Postgres. A single shared
`omegahive_test` makes those runs collide: the committing port harness issues
`TRUNCATE events, runs RESTART IDENTITY` at fixture setup, which deadlocks against a
concurrent run's readers (AccessExclusiveLock vs AccessShareLock) and, when it wins,
deletes the other run's committed rows and resets its seq sequence. So each run gets its
own database instead — isolation matching the worker isolation it runs under.

Naming: `omegahive_test_<epoch>_<pid>_<rand>`. The epoch is what `sweep` reads, so a run
killed before its drop leaves a self-dating scratch database the next run can reap.

Environment:
  OMEGAHIVE_TEST_DATABASE_URL  base DSN — server, credentials, and the fallback
                               maintenance database. Its database component is no longer
                               what tests use; the per-run name replaces it.
  OMEGAHIVE_TEST_DB            pin the database name. Then the caller owns the lifecycle:
                               it is created if absent and never dropped.
  OMEGAHIVE_TEST_DB_MAX_AGE_S  orphan sweep threshold in seconds (default 6h; 0 disables).

Callers — one mechanism, three paths:
  * host `uv run pytest`       tests/conftest.py, pytest_sessionstart / -finish
  * compose `test` service     the same conftest, in-container
  * scripts/deploy_checks.sh   the CLI below, `python /app/tests/scratch_db.py …`
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import sys
import time
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import psycopg
from psycopg import sql

BASE_URL_ENV = "OMEGAHIVE_TEST_DATABASE_URL"
NAME_ENV = "OMEGAHIVE_TEST_DB"
MAX_AGE_ENV = "OMEGAHIVE_TEST_DB_MAX_AGE_S"

DEFAULT_BASE_URL = "postgresql://omegahive:omegahive@localhost:5432/omegahive_test"
DEFAULT_MAX_AGE_S = 6 * 60 * 60
PREFIX = "omegahive_test_"
# Always present on a Postgres instance, so CREATE/DROP DATABASE has somewhere to run
# from even on a host where the base DSN's database does not exist yet.
MAINTENANCE_DB = "postgres"

# The names sweep is allowed to reap: this module's grammar, plus the `_restore` sibling
# scripts/pg_restore_check.sh derives from it. Anything else — including a pinned
# OMEGAHIVE_TEST_DB or the legacy shared `omegahive_test` — is left alone.
_SCRATCH_NAME = re.compile(rf"^{PREFIX}(\d+)_\d+_[0-9a-f]+(?:_restore)?$")


def base_url() -> str:
    return os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL


def with_database(url: str, dbname: str) -> str:
    """The same DSN pointed at a different database."""
    return urlunsplit(urlsplit(url)._replace(path=f"/{dbname}"))


def database_of(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def unique_name() -> str:
    return f"{PREFIX}{int(time.time())}_{os.getpid()}_{secrets.token_hex(2)}"


def max_age_s() -> int:
    raw = os.environ.get(MAX_AGE_ENV)
    return DEFAULT_MAX_AGE_S if raw is None else int(raw)


def resolve(url: str | None = None) -> tuple[str, str, bool]:
    """(database name, full DSN, ephemeral). Ephemeral runs are dropped on session exit;
    a pinned OMEGAHIVE_TEST_DB is not — naming it makes the caller its owner."""
    base = url or base_url()
    pinned = os.environ.get(NAME_ENV)
    if pinned:
        return pinned, with_database(base, pinned), False
    name = unique_name()
    return name, with_database(base, name), True


def _admin(url: str) -> psycopg.Connection:
    """An autocommit connection able to CREATE/DROP DATABASE (never to the target itself)."""
    try:
        return psycopg.connect(with_database(url, MAINTENANCE_DB), autocommit=True)
    except psycopg.OperationalError:
        # Instance without a `postgres` database: fall back to the base DSN's own.
        return psycopg.connect(url, autocommit=True)


def create(url: str) -> str:
    """Create the database `url` points at if it is not already there. Returns its name."""
    name = database_of(url)
    with _admin(url) as conn:
        if conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone():
            return name
        try:
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        except psycopg.errors.DuplicateDatabase:
            pass  # a concurrent run with the same pinned name got there first
    return name


def drop(name: str, url: str | None = None) -> None:
    """Drop `name`, forcing off any straggler backends (a leaked connection must not
    turn a finished run into an orphan database)."""
    with _admin(url or base_url()) as conn:
        conn.execute(
            sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(sql.Identifier(name))
        )


def sweep(
    url: str | None = None,
    max_age: int | None = None,
    keep: Iterable[str] = (),
) -> list[str]:
    """Drop scratch databases older than `max_age` seconds — the orphans left by runs that
    were killed before their own drop.

    A database with a connected backend is never reaped, however old: age alone cannot tell
    an abandoned database from a long-running suite's, and reaping a live run's spine is the
    very failure this whole mechanism exists to prevent. Best-effort otherwise — a database
    another run has already dropped is skipped rather than failing the sweep.
    """
    age = max_age_s() if max_age is None else max_age
    if age <= 0:
        return []
    cutoff = time.time() - age
    spared = set(keep)
    dropped: list[str] = []
    with _admin(url or base_url()) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{PREFIX}%",)
        ).fetchall()
        in_use = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT datname FROM pg_stat_activity WHERE datname IS NOT NULL"
            ).fetchall()
        }
        for (name,) in rows:
            match = _SCRATCH_NAME.match(name)
            if match is None or name in spared or name in in_use:
                continue
            if int(match.group(1)) >= cutoff:
                continue
            try:
                conn.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)").format(
                        sql.Identifier(name)
                    )
                )
            except psycopg.Error:
                continue
            dropped.append(name)
    return dropped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="per-run scratch test databases")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new", help="create a unique scratch database; print its DSN")
    dropper = sub.add_parser("drop", help="drop one scratch database by name")
    dropper.add_argument("name")
    sweeper = sub.add_parser("sweep", help="drop scratch databases older than the threshold")
    sweeper.add_argument("--max-age-s", type=int, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "new":
        _, url, _ = resolve()
        create(url)
        print(url)
    elif args.cmd == "drop":
        drop(args.name)
    else:
        for name in sweep(max_age=args.max_age_s):
            print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
