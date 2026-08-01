"""The two-role write path (migrations/0003_two_roles.sql): the grants, not the wiring.

`SET LOCAL ROLE` rather than a second DSN, deliberately. The roles are CLUSTER-global, so
a test that gave them passwords in order to connect as them would rewrite the credentials
of whatever deployment shares this Postgres — including a live one that had already cut
over. `SET LOCAL ROLE` drops to exactly the same privilege set (permission checks read
`current_user`, and the superuser bypass goes with it), is scoped to the surrounding
savepoint, and mutates nothing. The DSN-level form of the open-test — two genuinely
separate credentials — is deployment check 6 (`omegahive deploy-checks`), which asserts it
against the real DSNs on a deployment that has them.
"""

from __future__ import annotations

import psycopg

_APPEND = """
INSERT INTO events (run_id, logical_ts, actor_role, actor_id, event_type, payload)
VALUES ('two-role-test', 1, 'instrument', 'probe', 'note.posted', '{}'::jsonb)
"""


def _refused(conn, role: str, statement: str) -> bool:
    """True iff Postgres refused `statement` to `role` **on privilege**.

    Each attempt gets its own savepoint, so a refusal aborts the probe and not the test's
    outer transaction. Any other error propagates: a statement that fails for an unrelated
    reason must not read as a passing privilege check.

    TWO nested savepoints, and both are load-bearing. The outer one scopes `SET LOCAL ROLE`
    and lets that statement's OWN refusal propagate: `SET ROLE` raises InsufficientPrivilege
    when the session user is neither superuser nor a member of `role`, and swallowing it
    would make every "cannot" test here pass vacuously — proving nothing about the grants,
    on exactly the non-superuser session this whole change pushes towards. The inner one is
    what the probed statement's refusal unwinds; without it the failed statement leaves the
    transaction aborted and every later command in the test raises InFailedSqlTransaction.
    """
    with conn.transaction():
        conn.execute(f"SET LOCAL ROLE {role}")
        try:
            with conn.transaction():
                conn.execute(statement)
        except psycopg.errors.InsufficientPrivilege:
            return True
        return False


def test_reader_cannot_append(conn):
    assert _refused(conn, "hive_reader", _APPEND)


def test_reader_cannot_update_or_delete_events(conn):
    assert _refused(conn, "hive_reader", "UPDATE events SET payload = '{}'::jsonb WHERE seq = 1")
    assert _refused(conn, "hive_reader", "DELETE FROM events WHERE seq = 1")


def test_reader_cannot_register_a_run(conn):
    assert _refused(conn, "hive_reader", "INSERT INTO runs (run_id) VALUES ('two-role-test')")


def test_reader_can_read_the_spine(conn):
    """The refusals above must be about WRITING. A credential that could not read either
    would pass every other test here while making the UI and the notifier useless."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE hive_reader")
        for table in ("events", "runs", "schema_migrations"):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone() is not None


def test_gateway_can_append(conn):
    """The other half of the open-test: the write credential really does write — through
    the sequence and the correlation trigger, not merely past the table's ACL."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE hive_gateway")
        conn.execute("INSERT INTO runs (run_id) VALUES ('two-role-test') ON CONFLICT DO NOTHING")
        row = conn.execute(_APPEND + " RETURNING seq, event_id, correlation_id").fetchone()
    assert row is not None
    seq, event_id, correlation_id = row
    assert seq > 0                      # the sequence grant
    assert event_id is not None         # the DB-side default
    assert correlation_id == event_id   # the trigger ran under the gateway role


def test_gateway_cannot_delete_events(conn):
    """Append-only is a grant, not a convention: the write credential has no DELETE."""
    assert _refused(conn, "hive_gateway", "DELETE FROM events WHERE seq = 1")


def test_gateway_cannot_rewrite_a_recorded_event(conn):
    """The UPDATE grant is COLUMN-scoped, and this is why. A DELETE leaves a gap in `seq`;
    an overwritten actor_id or event_type leaves nothing at all, so a table-wide UPDATE
    would make the log rewritable without a trace by the very credential every emit uses."""
    for column in ("event_type = 'note.posted'", "actor_id = 'someone-else'",
                   "run_id = 'other-run'", "logical_ts = 0"):
        assert _refused(conn, "hive_gateway", f"UPDATE events SET {column} WHERE seq = 1")


def test_gateway_can_update_only_the_two_columns_the_system_writes(conn):
    """The coalescing counter and the generation bump — the only two updates the running
    system performs — must still work, or the grant is too tight to be correct."""
    with conn.transaction():
        conn.execute("SET LOCAL ROLE hive_gateway")
        conn.execute("INSERT INTO runs (run_id) VALUES ('two-role-test') ON CONFLICT DO NOTHING")
        seq = conn.execute(_APPEND + " RETURNING seq").fetchone()[0]
        conn.execute(
            "UPDATE events SET payload = jsonb_set(payload, '{coalesced_count}', to_jsonb(2))"
            " WHERE seq = %s",
            (seq,),
        )
        conn.execute(
            "UPDATE runs SET generation = generation + 1 WHERE run_id = 'two-role-test'"
        )


def test_gateway_cannot_change_the_schema(conn):
    """DDL stays with the owner — a gateway that could alter the log's shape would make
    the migration ledger fiction."""
    assert _refused(conn, "hive_gateway", "ALTER TABLE events ADD COLUMN probe TEXT")


def test_membership_runs_gateway_to_reader_only(conn):
    """Direction matters: gateway is a member of reader, never the reverse. Were it
    symmetric, a session authenticated as hive_reader could SET ROLE its way into the write
    grants and the split would be theatre.

    Asserted on the catalog rather than by attempting `SET ROLE`, because SET ROLE is
    authorized against the SESSION user — here the superuser owner — so the attempt would
    succeed in this fixture no matter which way the membership ran, and prove nothing.
    """
    rows = conn.execute(
        """
        SELECT member.rolname, grp.rolname
          FROM pg_auth_members m
          JOIN pg_roles member ON member.oid = m.member
          JOIN pg_roles grp    ON grp.oid    = m.roleid
         WHERE member.rolname IN ('hive_reader', 'hive_gateway')
           AND grp.rolname    IN ('hive_reader', 'hive_gateway')
        """
    ).fetchall()
    assert rows == [("hive_gateway", "hive_reader")]
