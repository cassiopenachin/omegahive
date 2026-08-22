"""sole-write-path: the privilege boundary the order buys, tested as privilege.

The order's definition of done (b) is explicit about what counts: an emit through the
gateway succeeds, and a direct INSERT attempted as the read role fails **on privilege** —
"not on convention, not on a code path that chose not to try". So this asks the database,
never the source: it applies whatever migrations the candidate wrote to a scratch
database, then switches role inside a transaction and tries the writes.

Two roles are named by the order itself — `hive_gateway` writes, `hive_reader` reads — so
naming them here is reading the order, not the patch. Everything else is derived from what
the order says the roles may do, and an implementation that grants it differently but
reaches the same boundary passes.

**What is measurable here, stated rather than assumed.** Roles are cluster-global while
grants are per-database, and this check runs against a scratch database on whatever cluster
the host offers — which on a deployment that has already cut over ALREADY HAS both roles,
with passwords. So role existence and the no-credential-in-a-migration rule are reported
and not scored: a green there could be the host's own history. What IS scored is the grant
matrix in the scratch database, because nothing but the candidate's own migrations put it
there. A check that passes at the pre-task baseline as readily as at the accepted outcome
measures nothing, and this is the half that would have.

`SET LOCAL ROLE` rather than a login: the order requires that a migration in the repository
carry no credential, so the roles legitimately have no password and cannot authenticate at
all until the operator's cutover. Role switching inside a transaction is the only way to
ask the question without inventing the credential the order forbids. A session that is not
a superuser or a member of both roles cannot switch, and that is reported as an environment
failure rather than as a red cell — a check that silently passes because it could not run
is the defect this instrument exists to avoid.

    uv run --frozen python sole_write_path_property.py

Runs inside a candidate root, makes and drops its own scratch database, and never touches
the durable one.
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg

sys.path.insert(0, os.path.abspath("src"))

BASE_URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)
READER, GATEWAY = "hive_reader", "hive_gateway"


class EnvironmentFailure(RuntimeError):
    """The check could not be run. Never reported as a failed property."""


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _admin(url: str):
    conn = psycopg.connect(_with_db(url, "postgres"))
    conn.autocommit = True
    return conn


def _as_role(conn, role: str, sql: str, params: tuple = ()) -> str | None:
    """Run one statement with the session role set, and report the SQLSTATE if it fails.

    Returns None on success, the SQLSTATE on refusal. The role switch itself failing is an
    environment failure, not a result: a swallowed InsufficientPrivilege on `SET ROLE`
    would make every "cannot" case pass vacuously.
    """
    with conn.cursor() as cur:
        try:
            cur.execute("BEGIN")
            cur.execute(f'SET LOCAL ROLE "{role}"')
        except psycopg.Error as exc:  # noqa: PERF203 — the two failures mean different things
            conn.rollback()
            raise EnvironmentFailure(
                f"could not SET LOCAL ROLE {role}: {exc}. This session is neither a "
                "superuser nor a member of both roles, so the privilege question cannot "
                "be asked here."
            ) from exc
        try:
            cur.execute(sql, params)
        except psycopg.Error as exc:
            code = exc.sqlstate or "unknown"
            conn.rollback()
            return code
    conn.rollback()
    return None


def _roles_exist(conn) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rolname FROM pg_roles WHERE rolname = ANY(%s)", ([READER, GATEWAY],)
        )
        return sorted(r[0] for r in cur.fetchall())


def _has_password(conn, role: str) -> bool:
    with conn.cursor() as cur:
        cur.execute("SELECT rolpassword IS NOT NULL FROM pg_authid WHERE rolname = %s", (role,))
        row = cur.fetchone()
    return bool(row and row[0])


INSERT_EVENT = """
INSERT INTO events (run_id, logical_ts, actor_role, actor_id, event_type, task_id, payload)
VALUES (%s, 1, 'worker', 'probe', 'task.reported', 'probe-task', '{}'::jsonb)
"""


def run_cases(conn, run_id: str) -> list[str]:
    findings: list[str] = []

    present = _roles_exist(conn)
    if present != sorted([GATEWAY, READER]):
        return [
            f"FAIL roles: this cluster has {present or 'neither role'}; the order names "
            f"{GATEWAY} and {READER}, and the migration is what creates them"
        ]
    print(f"pass  both roles exist: {', '.join(present)}")
    if any(_has_password(conn, r) for r in (GATEWAY, READER)):
        # Cluster-global state, so on a host whose durable deployment has already cut over
        # this says nothing about the candidate. Reported, never scored — see the module
        # docstring's note on what is measurable here and what is not.
        print(
            "NOTE at least one role already carries a password on this cluster. Roles are "
            "cluster-global, so that may be the host's own cutover rather than anything the "
            "attempt did; whether a migration carries a credential is a rubric question here"
        )

    # The open test, both halves.
    code = _as_role(conn, READER, INSERT_EVENT, (run_id,))
    if code == "42501":
        print("pass  the read role's direct INSERT is refused on privilege (42501)")
    elif code is None:
        findings.append(
            "FAIL open-test: the read role inserted an event. The whole order is that it "
            "cannot."
        )
    else:
        findings.append(
            f"FAIL open-test: the read role's INSERT failed with SQLSTATE {code}, not "
            "insufficient_privilege (42501). A refusal for another reason is not the "
            "boundary the order buys — it is a code path that chose not to try."
        )

    code = _as_role(conn, GATEWAY, INSERT_EVENT, (run_id,))
    if code is None:
        print("pass  the gateway role can append an event")
    else:
        findings.append(
            f"FAIL open-test: the gateway role cannot append (SQLSTATE {code}). Moving the "
            "write path onto a role that cannot write is the failure mode this half exists "
            "to catch."
        )

    code = _as_role(conn, READER, "SELECT count(*) FROM events")
    if code is None:
        print("pass  the read role can still read events")
    else:
        findings.append(f"FAIL read: the read role cannot SELECT from events (SQLSTATE {code})")

    # Append-only, against the credential every emit uses. A DELETE leaves a gap in the
    # sequence; an overwrite of the identity columns leaves nothing at all.
    for label, sql in (
        ("delete an event", "DELETE FROM events WHERE seq = -1"),
        ("rewrite an event's actor", "UPDATE events SET actor_id = 'x' WHERE seq = -1"),
        ("rewrite an event's type", "UPDATE events SET event_type = 'x' WHERE seq = -1"),
        ("rewrite an event's run", "UPDATE events SET run_id = 'x' WHERE seq = -1"),
    ):
        code = _as_role(conn, GATEWAY, sql)
        if code == "42501":
            print(f"pass  the gateway role cannot {label}")
        elif code is None:
            findings.append(
                f"FAIL append-only: the gateway role can {label}. That is the credential "
                "every emit already holds, so a table-wide write grant lets any emit path "
                "rewrite recorded history."
            )
        else:
            findings.append(
                f"FAIL append-only: {label} failed with SQLSTATE {code} rather than a "
                "privilege refusal"
            )

    code = _as_role(conn, GATEWAY, "CREATE TABLE taskbench_probe (x int)")
    if code == "42501":
        print("pass  the gateway role cannot do DDL")
    elif code is None:
        findings.append("FAIL scope: the gateway role can create tables; DDL is the owner's")

    return findings


def main() -> int:
    if not Path("migrations").is_dir():
        print("FAIL missing: no migrations/ directory in the candidate tree")
        return 1
    name = f"taskbench_swp_{uuid.uuid4().hex[:12]}"
    try:
        admin = _admin(BASE_URL)
    except psycopg.Error as exc:
        print(f"FAIL environment: cannot reach Postgres at the test DSN: {exc}")
        return 1
    findings: list[str] = []
    try:
        with admin.cursor() as cur:
            cur.execute(f'CREATE DATABASE "{name}"')
        conn = psycopg.connect(_with_db(BASE_URL, name))
        try:
            from omegahive import db as ohdb

            applied = ohdb.migrate(conn)
            conn.commit()
            print(f"applied {len(applied)} migration(s): {', '.join(applied)}")
            findings = run_cases(conn, f"probe-{uuid.uuid4().hex[:8]}")
        except EnvironmentFailure as exc:
            print(f"FAIL environment: {exc}")
            return 1
        finally:
            conn.close()
    finally:
        try:
            with admin.cursor() as cur:
                cur.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                cur.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            admin.close()

    for f in findings:
        print(f)
    if findings:
        print(f"\n{len(findings)} property failure(s)")
        return 1
    print("\nok: the gateway is the only writer, and the boundary is a privilege boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
