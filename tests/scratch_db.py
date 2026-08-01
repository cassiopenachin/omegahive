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
  OMEGAHIVE_TEST_DATABASE_URL  base DSN — server and credentials. Its database component is
                               no longer what tests use; the per-run name replaces it, and
                               CREATE/DROP run from the `postgres` maintenance database.
  OMEGAHIVE_TEST_DB            pin the database name. Then the caller owns the lifecycle:
                               it is created if absent and never dropped.
  OMEGAHIVE_TEST_DB_MAX_AGE_S  orphan sweep threshold in seconds (default 6h; 0 disables).

Callers — one mechanism, three paths:
  * host `uv run pytest`       tests/conftest.py, pytest_sessionstart / -finish
  * compose `test` service     the same conftest, in-container
  * scripts/deploy_checks.sh   the CLI below, `python /app/tests/scratch_db.py …`

`roleurls` is the deploy-checks path's one extra need: once a deployment has cut over to
the two-role scheme, redirecting the harness at a scratch spine means redirecting all
three credentials, not just one — leaving the gateway or owner DSN pointing at the durable
database would send the harness's writes there. Deriving those DSNs is the same
database-component swap this module already owns, so it lives here rather than in a second
copy of the URL surgery.
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
# initdb creates this on every instance, so CREATE/DROP DATABASE always has somewhere to run
# from — including on a host where the base DSN's own database does not exist yet.
MAINTENANCE_DB = "postgres"


def _name_re(prefix: str = PREFIX) -> re.Pattern[str]:
    """The names a sweep is allowed to reap: this module's grammar, plus the `_restore`
    sibling scripts/pg_restore_check.sh derives from it. Anything else — a pinned
    OMEGAHIVE_TEST_DB, the legacy shared `omegahive_test` — is left alone."""
    return re.compile(rf"^{re.escape(prefix)}(\d+)_\d+_[0-9a-f]+(?:_restore)?$")


_SCRATCH_NAME = _name_re()


def base_url() -> str:
    return os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL


def with_database(url: str, dbname: str) -> str:
    """The same DSN pointed at a different database."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.netloc:
        # libpq also accepts "host=… dbname=…" and socket-only URIs; rewriting those by URL
        # surgery silently produces a corrupt DSN, so refuse them by name instead.
        raise ValueError(
            f"{BASE_URL_ENV} must be a postgresql://host/db URL, got {url!r}"
        )
    return urlunsplit(parts._replace(path=f"/{dbname}"))


def database_of(url: str) -> str:
    return urlsplit(url).path.lstrip("/")


def unique_name() -> str:
    """The random tail carries the real uniqueness: in a container every run is pid 1, so
    `<epoch>_<pid>` alone collides between two runs starting in the same second — and a
    collision silently recreates the shared-database bug this module removes."""
    return f"{PREFIX}{int(time.time())}_{os.getpid()}_{secrets.token_hex(8)}"


def max_age_s() -> int:
    # `or`, not `is None`: compose and .env deliver a declared-but-empty variable as "".
    return int(os.environ.get(MAX_AGE_ENV) or DEFAULT_MAX_AGE_S)


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
    """An autocommit connection able to CREATE/DROP DATABASE.

    Always the maintenance database, never the target: you cannot drop the database you are
    connected to. `postgres` is created by initdb on every instance, so if it is missing that
    deserves an error naming it — a fallback here would connect to the target and turn every
    drop into `cannot drop the currently open database`, and would also swallow the ordinary
    wrong-host / wrong-password failures behind a misleading message.
    """
    return psycopg.connect(with_database(url, MAINTENANCE_DB), autocommit=True)


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
    prefix: str = PREFIX,
) -> list[str]:
    """Drop scratch databases older than `max_age` seconds — the orphans left by runs that
    were killed before their own drop.

    What protects a live run is the age margin — hours against a suite measured in seconds —
    so callers must not shrink `max_age` towards a plausible run length. A database with a
    connected backend is also skipped, but that is a second line of defence only: the per-test
    connection churn leaves a live run backend-free a noticeable fraction of the time, so it
    cannot be relied on alone. Best-effort otherwise — a database another run has already
    dropped is skipped rather than failing the sweep.

    `prefix` narrows which databases are in scope at all. Production sweeps leave it at the
    default; tests/test_scratch_db.py passes a probe prefix of its own so that its fabricated
    orphans and a concurrently running copy of the suite stay invisible to each other.
    """
    age = max_age_s() if max_age is None else max_age
    if age <= 0:
        return []
    cutoff = time.time() - age
    spared = set(keep)
    matcher = _name_re(prefix)
    dropped: list[str] = []
    with _admin(url or base_url()) as conn:
        rows = conn.execute(
            "SELECT datname FROM pg_database WHERE datname LIKE %s", (f"{prefix}%",)
        ).fetchall()
        in_use = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT datname FROM pg_stat_activity WHERE datname IS NOT NULL"
            ).fetchall()
        }
        for (name,) in rows:
            match = matcher.match(name)
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


def role_urls(name: str) -> dict[str, str]:
    """The configured read and write DSNs, pointed at the scratch database `name`.

    A literal "-" for a role this deployment has not configured — a pre-cutover host has no
    gateway DSN, and the caller must then leave the variable unset so the fallback in
    db.connect_gateway() applies. Never invent one: a fabricated DSN would authenticate as a
    role that may not exist and fail far from the cause.

    "-" rather than an empty string because the caller is a shell script that cannot
    otherwise distinguish "not configured" from "this command never ran" — and those two
    have opposite consequences. An old image without this subcommand exits non-zero and
    prints nothing, which read as "not configured" would leave the harness's write
    credential pointing at the DURABLE database.
    """
    from omegahive.config import get_settings  # in-container import; not needed on the host

    settings = get_settings()
    return {
        "reader": with_database(settings.database_url, name) if settings.database_url else "-",
        "gateway": (
            with_database(settings.gateway_database_url, name)
            if settings.gateway_database_url
            else "-"
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="per-run scratch test databases")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("new", help="create a unique ephemeral scratch database; print 'name DSN'")
    urler = sub.add_parser("url", help="print the DSN for a database name on this server")
    urler.add_argument("name")
    roler = sub.add_parser(
        "roleurls", help="print '<role> <DSN>' per line for the read and write credentials"
    )
    roler.add_argument("name")
    dropper = sub.add_parser("drop", help="drop one scratch database by name")
    dropper.add_argument("name")
    sweeper = sub.add_parser("sweep", help="drop scratch databases older than the threshold")
    sweeper.add_argument("--max-age-s", type=int, default=None)
    args = parser.parse_args(argv)

    if args.cmd == "new":
        # Always ephemeral, never the pinned OMEGAHIVE_TEST_DB: the shell caller drops what
        # this hands back, and a pinned name is the operator's to keep, not ours to destroy.
        # Name and DSN are printed together so no caller has to parse a database name out of
        # a URL -- a base DSN carrying query parameters defeats a naive `${url##*/}`.
        url = with_database(base_url(), unique_name())
        print(f"{create(url)} {url}")
    elif args.cmd == "url":
        print(with_database(base_url(), args.name))
    elif args.cmd == "roleurls":
        for role, url in role_urls(args.name).items():
            print(f"{role} {url}")
    elif args.cmd == "drop":
        drop(args.name)
    else:
        for name in sweep(max_age=args.max_age_s):
            print(name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
