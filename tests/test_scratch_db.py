"""The per-run test database mechanism (tests/scratch_db.py) — naming, lifecycle, sweep.

These run against the live Postgres the rest of the suite already needs, creating and
dropping throwaway databases of their own. Two rules keep them honest under the very
concurrency they exist to defend, since another copy of this suite may be running against
the same server at the same instant:

  * every sweep here is scoped to PROBE, a prefix unique to this process, so these tests
    and a concurrent copy cannot see each other's fixtures. A production sweep ignores
    PROBE names too — `probe<hex>` is where it expects the epoch.
  * every sweep here uses a threshold far above the age any live run's database could
    have reached. A short one would reap a running suite's spine — this module's own bug.
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterable
from secrets import token_hex

import psycopg
import pytest

import scratch_db


def _session_db() -> str:
    return scratch_db.database_of(os.environ[scratch_db.BASE_URL_ENV])


# This process's private corner of the shared server (see the module docstring).
PROBE = f"{scratch_db.PREFIX}probe{token_hex(3)}_"


def _sweep(max_age: int, keep: Iterable[str] = ()) -> list[str]:
    return scratch_db.sweep(max_age=max_age, keep=(_session_db(), *keep), prefix=PROBE)


def _aged(seconds_ago: int) -> str:
    """A PROBE-scoped scratch name that looks `seconds_ago` old."""
    return f"{PROBE}{int(time.time()) - seconds_ago}_{os.getpid()}_{token_hex(2)}"


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
    old = throwaway(_aged(86400))
    young = throwaway(_aged(0))
    spared = throwaway(_aged(86400))

    dropped = _sweep(3600, keep=(spared,))

    assert old in dropped
    assert not _exists(old)
    assert young not in dropped and _exists(young)    # too recent to be an orphan
    assert spared not in dropped and _exists(spared)  # explicitly kept


def test_sweep_reaps_the_restore_sibling(throwaway):
    """scripts/pg_restore_check.sh derives <scratch>_restore; the sweep must reap it too."""
    restore = throwaway(f"{_aged(86400)}_restore")
    assert restore in _sweep(3600)
    assert not _exists(restore)


def test_sweep_never_touches_names_outside_the_grammar(throwaway):
    """An operator-pinned OMEGAHIVE_TEST_DB is not a scratch database, at any age."""
    pinned = throwaway(f"{PROBE}pinned_{os.getpid()}_{token_hex(2)}")
    assert scratch_db._name_re(PROBE).match(pinned) is None           # no epoch: not scratch
    assert scratch_db._SCRATCH_NAME.match("omegahive_test") is None   # the legacy shared DB
    assert pinned not in _sweep(3600)
    assert _exists(pinned)


def test_a_production_sweep_ignores_this_file_s_probe_databases(throwaway):
    """The scoping cuts both ways: PROBE names are invisible to the default sweep."""
    probe = throwaway(_aged(86400))
    assert scratch_db._SCRATCH_NAME.match(probe) is None
    assert probe not in scratch_db.sweep(max_age=3600, keep=(_session_db(),))
    assert _exists(probe)


def test_sweep_spares_a_database_that_is_in_use(throwaway):
    """Age alone cannot tell an abandoned database from a live long-running suite's."""
    name = throwaway(_aged(86400))
    live = psycopg.connect(scratch_db.with_database(scratch_db.base_url(), name))
    try:
        assert name not in _sweep(3600)
        assert _exists(name)
    finally:
        live.close()
    assert name in _sweep(3600)  # once nothing is connected, it is an orphan again


def test_sweep_disabled_by_a_zero_threshold(throwaway):
    old = throwaway(_aged(86400))
    assert _sweep(0) == []
    assert _exists(old)


def test_cli_new_ignores_a_pinned_name(monkeypatch, capsys):
    """deploy_checks.sh drops whatever `new` hands back, so it must never hand back the
    operator's pinned database."""
    monkeypatch.setenv(scratch_db.NAME_ENV, "my_own_test_db")
    assert scratch_db.main(["new"]) == 0
    name = scratch_db.database_of(capsys.readouterr().out.strip())
    try:
        assert name != "my_own_test_db"
        assert scratch_db._SCRATCH_NAME.match(name)
    finally:
        scratch_db.drop(name)


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
