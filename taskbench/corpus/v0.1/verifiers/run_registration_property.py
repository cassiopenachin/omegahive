"""run-registration: the order's property, against whatever the candidate built.

The order states an outcome — *every run touched by an emit carries a generation token from
its first event* — and enumerates the cases that pin it. This exercises those cases through
the candidate's own gateway, so an implementation that reaches the outcome a different way
passes. It never imports a helper the historical patch happened to add and never inspects
source text.

Case three is the one that matters and the one the closed review caught: a REPLAYED emit
against a run that already has events but no registry row must still register it. An
implementation that opens the run after the idempotency early-return passes the other cases
and fails this one.

This script runs inside a candidate root at the task's historical baseline, so it depends
only on interfaces that existed there: `omegahive.db.connect/migrate`, `EventLog`,
`Gateway`, `Policy`, `Actor`. It makes and drops its own scratch database and never touches
the durable one.

    uv run --frozen python run_registration_property.py
"""

from __future__ import annotations

import os
import sys
import uuid
from urllib.parse import urlsplit, urlunsplit

import psycopg

sys.path.insert(0, os.path.abspath("src"))

BASE_URL = os.environ.get(
    "OMEGAHIVE_TEST_DATABASE_URL",
    "postgresql://omegahive:omegahive@localhost:5432/omegahive_test",
)


def _with_db(url: str, name: str) -> str:
    parts = urlsplit(url)
    return urlunsplit((parts.scheme, parts.netloc, f"/{name}", parts.query, parts.fragment))


def _admin(url: str):
    conn = psycopg.connect(_with_db(url, "postgres"))
    conn.autocommit = True
    return conn


def _generation(conn, run_id: str):
    with conn.cursor() as cur:
        cur.execute("SELECT generation FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return row[0] if row else None


def _count_rows(conn, run_id: str) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM runs WHERE run_id = %s", (run_id,))
        row = cur.fetchone()
    return int(row[0]) if row else 0


def run_cases(conn) -> list[str]:
    from omegahive.clock import LogicalClock
    from omegahive.events.envelope import Actor
    from omegahive.events.log import EventLog
    from omegahive.gateway import Gateway, Policy

    actor = Actor(id="operator", role="human")
    failures: list[str] = []

    def emit(run_id: str, task_id: str, key: str) -> None:
        gateway = Gateway(EventLog(conn, LogicalClock(), run_id), Policy())
        gateway.emit(
            actor=actor,
            event_type="task.created",
            payload={"title": "taskbench property probe", "task_type": "chore"},
            task_id=task_id,
            idempotency_key=key,
        )

    # 1 — a first emit against a fresh run registers it.
    run_a = f"tb-{uuid.uuid4().hex[:10]}"
    emit(run_a, "t1", f"{run_a}-1")
    gen = _generation(conn, run_a)
    if gen is None:
        failures.append("case 1: a first emit did not register the run")

    # 2 — a second emit is a registration no-op.
    emit(run_a, "t2", f"{run_a}-2")
    if _generation(conn, run_a) != gen:
        failures.append("case 2: a second emit changed the generation; the open is not a no-op")
    if _count_rows(conn, run_a) != 1:
        failures.append("case 2: more than one registry row for the run")

    # 3 — the replay case: events present, registry row removed, next emit is a REPLAY.
    run_b = f"tb-{uuid.uuid4().hex[:10]}"
    key = f"{run_b}-replay"
    emit(run_b, "t1", key)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM runs WHERE run_id = %s", (run_b,))
    conn.commit()
    if _generation(conn, run_b) is not None:
        failures.append("case 3 setup: the registry row was not removable")
    emit(run_b, "t1", key)  # same key ⇒ dedups at the idempotency check
    if _generation(conn, run_b) is None:
        failures.append(
            "case 3: a REPLAYED emit against a run with events but no registry row did not "
            "register it — the open happens after the idempotency early-return"
        )

    # 4 — a rejected emit still registers its run: a gateway.rejected event is log content
    #     a client can hold a cursor over, so the run it lands on needs a generation too.
    run_c = f"tb-{uuid.uuid4().hex[:10]}"
    try:
        gw = Gateway(EventLog(conn, LogicalClock(), run_c), Policy())
        gw.emit(
            actor=Actor(id="w1", role="worker"),
            event_type="task.created",
            payload={"title": "not a worker's to create", "task_type": "chore"},
            task_id="t1",
            idempotency_key=f"{run_c}-rejected",
        )
    except Exception as exc:  # noqa: BLE001 — a refusal is the point; its shape is not
        print(f"note: the rejected-emit case raised {type(exc).__name__}: {exc}")
    if _generation(conn, run_c) is None:
        failures.append(
            "case 4: a run whose only emit was rejected carries no generation token"
        )

    # 5 — the generation then bumps on a registered run.
    if _generation(conn, run_b) is not None:
        before = _generation(conn, run_b)
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE runs SET generation = generation + 1 WHERE run_id = %s"
                " RETURNING generation",
                (run_b,),
            )
            row = cur.fetchone()
        conn.commit()
        if not row or row[0] != before + 1:
            failures.append("case 5: the generation did not bump on a registered run")
    return failures


def main() -> int:
    from omegahive.db import connect, migrate

    scratch = f"taskbench_probe_{uuid.uuid4().hex[:12]}"
    admin = _admin(BASE_URL)
    with admin.cursor() as cur:
        cur.execute(f'CREATE DATABASE "{scratch}"')
    try:
        conn = connect(_with_db(BASE_URL, scratch))
        migrate(conn)
        conn.commit()
        failures = run_cases(conn)
        conn.close()
    finally:
        with admin.cursor() as cur:
            cur.execute(f'DROP DATABASE IF EXISTS "{scratch}" WITH (FORCE)')
        admin.close()

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        print(f"\n{len(failures)} property case(s) failed")
        return 1
    print("ok — every registration property case holds")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except psycopg.OperationalError as exc:
        print(f"FAIL no reachable Postgres for the property probe: {exc}")
        raise SystemExit(1) from exc
