"""The per-run test database mechanism (tests/scratch_db.py) — naming, lifecycle, sweep.

These run against the live Postgres the rest of the suite already needs, creating and
dropping throwaway databases of their own. Every sweep here goes through `_sweep`, which
spares the database this very session is running on — a test that reaped its own spine
would take the suite down with it.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable

import psycopg
import pytest

import scratch_db


def _session_db() -> str:
    return scratch_db.database_of(os.environ[scratch_db.BASE_URL_ENV])


def _sweep(max_age: int, keep: Iterable[str] = ()) -> list[str]:
    return scratch_db.sweep(max_age=max_age, keep=(_session_db(), *keep))


def _exists(name: str) -> bool:
    url = scratch_db.with_database(scratch_db.base_url(), scratch_db.MAINTENANCE_DB)
    with psycopg.connect(url, autocommit=True) as conn:
        row = conn.execute("SELECT 1 FROM pg_database WHERE datname = %s", (name,)).fetchone()
    return row is not None


@pytest.fixture
def throwaway():
    """Databases created by a test, dropped afterwards however the test ends."""
    made: list[str] = []

    def _make(name: str | None = None) -> str:
        name = name or scratch_db.unique_name()
        made.append(name)
        scratch_db.create(scratch_db.with_database(scratch_db.base_url(), name))
        return name

    yield _make
    for name in made:
        scratch_db.drop(name)


def test_unique_name_is_per_run_and_parseable():
    a, b = scratch_db.unique_name(), scratch_db.unique_name()
    assert a != b
    assert a.startswith(scratch_db.PREFIX)
    assert len(a) <= 63  # Postgres identifier limit
    match = scratch_db._SCRATCH_NAME.match(a)
    assert match is not None
    assert abs(int(match.group(1)) - time.time()) < 60  # the epoch the sweep reads


def test_resolve_defaults_to_a_unique_ephemeral_database(monkeypatch):
    monkeypatch.delenv(scratch_db.NAME_ENV, raising=False)
    monkeypatch.setenv(scratch_db.BASE_URL_ENV, "postgresql://u:p@h:5432/base")
    name, url, ephemeral = scratch_db.resolve()
    assert ephemeral
    assert name.startswith(scratch_db.PREFIX)
    assert url == f"postgresql://u:p@h:5432/{name}"


def test_resolve_honours_the_name_override_and_leaves_it_to_the_caller(monkeypatch):
    """A pinned name means the caller owns the lifecycle: kept, not dropped."""
    monkeypatch.setenv(scratch_db.NAME_ENV, "my_own_test_db")
    monkeypatch.setenv(scratch_db.BASE_URL_ENV, "postgresql://u:p@h:5432/base")
    name, url, ephemeral = scratch_db.resolve()
    assert (name, ephemeral) == ("my_own_test_db", False)
    assert url == "postgresql://u:p@h:5432/my_own_test_db"


def test_create_is_idempotent_and_drop_removes(throwaway):
    name = throwaway()
    assert _exists(name)
    scratch_db.create(scratch_db.with_database(scratch_db.base_url(), name))  # again: no-op
    assert _exists(name)
    scratch_db.drop(name)
    assert not _exists(name)
    scratch_db.drop(name)  # dropping an absent database is not an error


def test_drop_forces_off_a_straggler_connection(throwaway):
    """A leaked connection must not turn a finished run into an orphan database."""
    name = throwaway()
    straggler = psycopg.connect(scratch_db.with_database(scratch_db.base_url(), name))
    try:
        scratch_db.drop(name)
        assert not _exists(name)
    finally:
        straggler.close()


def test_sweep_reaps_aged_orphans_only(throwaway):
    old = throwaway(f"{scratch_db.PREFIX}{int(time.time()) - 86400}_999999_dead")
    young = throwaway()
    spared = throwaway()

    dropped = _sweep(3600, keep=(spared,))

    assert old in dropped
    assert not _exists(old)
    assert young not in dropped and _exists(young)    # too recent to be an orphan
    assert spared not in dropped and _exists(spared)  # explicitly kept


def test_sweep_reaps_the_restore_sibling(throwaway):
    """scripts/pg_restore_check.sh derives <scratch>_restore; the sweep must reap it too."""
    stem = f"{scratch_db.PREFIX}{int(time.time()) - 86400}_999997_dead"
    restore = throwaway(f"{stem}_restore")
    assert restore in _sweep(3600)
    assert not _exists(restore)


def test_sweep_never_touches_names_outside_the_grammar(throwaway):
    """An operator-pinned OMEGAHIVE_TEST_DB is not a scratch database, at any age.

    Note the threshold: every sweep in this file stays far above the age of a database a
    concurrently running suite could own. A short threshold here would reap the other
    suite's spine mid-run — which is precisely the collision this module prevents.
    """
    pinned = throwaway(f"{scratch_db.PREFIX}operator_pinned")
    assert scratch_db._SCRATCH_NAME.match(pinned) is None      # no epoch: not scratch
    assert scratch_db._SCRATCH_NAME.match("omegahive_test") is None  # the legacy shared DB
    assert pinned not in _sweep(3600)
    assert _exists(pinned)


def test_sweep_spares_a_database_that_is_in_use(throwaway):
    """Age alone cannot tell an abandoned database from a live long-running suite's."""
    name = throwaway(f"{scratch_db.PREFIX}{int(time.time()) - 86400}_999996_dead")
    live = psycopg.connect(scratch_db.with_database(scratch_db.base_url(), name))
    try:
        assert name not in _sweep(3600)
        assert _exists(name)
    finally:
        live.close()
    assert name in _sweep(3600)  # once nothing is connected, it is an orphan again


def test_sweep_disabled_by_a_zero_threshold(throwaway):
    old = throwaway(f"{scratch_db.PREFIX}{int(time.time()) - 86400}_999998_dead")
    assert _sweep(0) == []
    assert _exists(old)


def test_max_age_reads_the_environment(monkeypatch):
    monkeypatch.delenv(scratch_db.MAX_AGE_ENV, raising=False)
    assert scratch_db.max_age_s() == scratch_db.DEFAULT_MAX_AGE_S
    monkeypatch.setenv(scratch_db.MAX_AGE_ENV, "42")
    assert scratch_db.max_age_s() == 42


def test_this_session_runs_on_its_own_database():
    """End-to-end: the suite is not on the shared omegahive_test."""
    name = _session_db()
    assert name != "omegahive_test"
    assert scratch_db._SCRATCH_NAME.match(name) or os.environ.get(scratch_db.NAME_ENV)
    assert _exists(name)
