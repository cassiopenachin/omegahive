"""The ephemeral credential: seeded opaquely, denied to the model, gone on every exit.

Codex reads its subscription credential out of the same directory that carries the
permission profile, so a generated home that excludes the operator's prior threads,
memory, plugins and configuration also excludes the login. The pattern this file holds
in place is the narrowest thing that works: copy THOSE BYTES and nothing else, mode
0600 inside a 0700 directory, never read them, and remove the directory on every
terminal path — clean exit, failure, and signal.

These tests drive the REAL shell functions from `scripts/hive-common.sh` rather than a
Python reimplementation of them, because the thing that has to be true is a property of
the code that actually runs at launch. The credential here is a fixture string, never a
real one.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
COMMON = REPO / "scripts" / "hive-common.sh"
CANARY = "FIXTURE-NOT-A-REAL-CREDENTIAL-7f3a"


def run_shell(script: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["bash", "-c", f'set -euo pipefail; source "{COMMON}"; {script}'],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        timeout=60,
        env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"), **(env or {})},
    )


@pytest.fixture
def auth_source(tmp_path: Path) -> Path:
    src = tmp_path / "operator-codex" / "auth.json"
    src.parent.mkdir(parents=True)
    src.write_text(json.dumps({"auth_mode": "chatgpt", "token": CANARY}))
    src.chmod(0o600)
    return src


# --- seeding ------------------------------------------------------------------------


@pytest.mark.parametrize("source_mode", [0o600, 0o644])
def test_the_credential_is_copied_with_restrictive_permissions(
    tmp_path, auth_source, source_mode
):
    """0600 WHATEVER the source was.

    The loose-source case is the one that matters: `cp` carries the source's mode
    through, so with a 0600 original the copy is 0600 whether or not anything tightened
    it — and a test that only ever saw that case would pass over a launcher that had
    quietly stopped tightening. An operator whose `auth.json` is group-readable is not
    a reason to write a group-readable copy.
    """
    auth_source.chmod(source_mode)
    home = tmp_path / "run" / "codex-home"
    home.mkdir(parents=True)
    home.chmod(0o755)   # deliberately loose to prove the seeding tightens it
    r = run_shell(f'seed_codex_auth "{home}"', {"CODEX_AUTH_SOURCE": str(auth_source)})
    assert r.returncode == 0, r.stderr

    seeded = home / "auth.json"
    assert seeded.exists()
    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600, "the credential copy is owner-only"
    assert stat.S_IMODE(home.stat().st_mode) == 0o700, "and so is the directory holding it"
    # Copied, not transformed: a re-serialization would be a read, and the launcher's
    # claim is that it moves bytes it never inspects.
    assert seeded.read_bytes() == auth_source.read_bytes()


def test_the_credential_value_never_reaches_the_launchers_output(tmp_path, auth_source):
    """`set -x` is the adversarial case: if the value were ever captured into a shell
    variable it would appear in a trace, and a trace is what an operator pastes into a
    report. The two recorded secret exposures on this host were both a value reaching a
    terminal."""
    home = tmp_path / "run" / "codex-home"
    home.mkdir(parents=True)
    r = run_shell(
        f'set -x; seed_codex_auth "{home}"; set +x',
        {"CODEX_AUTH_SOURCE": str(auth_source)},
    )
    assert r.returncode == 0, r.stderr
    assert CANARY not in r.stdout + r.stderr


def test_a_missing_credential_refuses_and_says_it_is_an_operator_act(tmp_path):
    """There is deliberately no automated login path."""
    home = tmp_path / "run" / "codex-home"
    home.mkdir(parents=True)
    r = run_shell(
        f'seed_codex_auth "{home}"', {"CODEX_AUTH_SOURCE": str(tmp_path / "nope.json")}
    )
    assert r.returncode != 0
    assert "OPERATOR act" in r.stderr or "operator act" in r.stderr.lower()
    assert "codex login" in r.stderr


# --- cleanup ------------------------------------------------------------------------


def _home_with_rollout(
    tmp_path: Path, extra_records: list[dict] | None = None
) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    home = run_dir / "codex-home"
    sessions = home / "sessions" / "2026" / "08" / "19"
    sessions.mkdir(parents=True)
    (home / "auth.json").write_text(json.dumps({"token": CANARY}))
    records = [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": {"input_tokens": 10, "output_tokens": 2}},
            },
        },
        *(extra_records or []),
    ]
    (sessions / "rollout-2026-08-19T10-00-00-abc.jsonl").write_text(
        "\n".join(json.dumps(r) for r in records) + "\n"
    )
    return run_dir, home


def test_cleanup_removes_the_credential_and_keeps_the_evidence(tmp_path):
    """Cleanup removes the CREDENTIAL, not the record.

    Codex writes its session rollout under the home it is given, and that rollout is
    the only place it states which model it ran and what it consumed. Deleting the home
    wholesale threw that away and left executions unattributable — the defect PR #55
    hit — so the rollout is copied out first.
    """
    run_dir, home = _home_with_rollout(tmp_path)
    r = run_shell(f'clean_codex_home "{home}" "{run_dir}"')
    assert r.returncode == 0, r.stderr

    assert not home.exists(), "the generated home, and the credential in it, are gone"
    preserved = run_dir / "codex-rollout.jsonl"
    assert preserved.exists(), "the non-secret evidence survives"
    assert "gpt-5.6-sol" in preserved.read_text()
    assert CANARY not in preserved.read_text()
    # Usage survives too: the model and the token counts are the whole reason the
    # rollout is preserved at all.
    assert "token_count" in preserved.read_text()


def test_only_the_two_evidence_record_kinds_are_persisted(tmp_path):
    """EXTRACTED, not copied.

    The rollout is a vendor file in a credential-bearing directory and it carries the
    whole session — the prompt, every tool call, every tool OUTPUT. Persisting all of
    that into a durable run-dir would be trusting a format this repository does not
    control to never carry anything credential-adjacent in any field, forever. An
    allowlist is checkable; "we believe it is clean" is not.
    """
    leaky = [
        {
            "type": "response_item",
            "payload": {
                "type": "custom_tool_call_output",
                "output": "cat ~/.netrc\nmachine example login bob password " + CANARY,
            },
        },
        {"type": "event_msg", "payload": {"type": "agent_message", "message": CANARY}},
        {"type": "session_meta", "payload": {"id": "abc", "instructions": CANARY}},
    ]
    run_dir, home = _home_with_rollout(tmp_path, extra_records=leaky)
    r = run_shell(f'clean_codex_home "{home}" "{run_dir}"')
    assert r.returncode == 0, r.stderr

    text = (run_dir / "codex-rollout.jsonl").read_text()
    assert CANARY not in text, "no record kind outside the allowlist reaches the run-dir"
    assert "custom_tool_call_output" not in text
    assert "session_meta" not in text
    # And what the record actually needs is still there.
    assert "gpt-5.6-sol" in text
    assert "token_count" in text


# --- the sweep, for the paths a trap cannot cover -------------------------------------


def test_the_sweep_removes_a_home_whose_supervisor_never_ran_its_trap(tmp_path):
    """An EXIT trap covers a clean exit, a failing exit and a handled signal. It does
    NOT cover SIGKILL, an OOM kill, or the host losing power — and this directory holds
    a copy of a live subscription credential, so "usually removed" is not the standard.
    `hive-supervise --reconcile` is the one thing that runs after those, so the floor
    under the trap lives there."""
    work_root = tmp_path / "work"
    run_dir = work_root / "sess-abandoned" / "execution"
    home = run_dir / "codex-home"
    (home / "sessions").mkdir(parents=True)
    (home / "auth.json").write_text(json.dumps({"token": CANARY}))

    r = run_shell(f'sweep_codex_homes "{work_root}"')
    assert r.returncode == 0, r.stderr
    assert not home.exists(), "the abandoned credential is gone"
    assert run_dir.exists(), "and the run-dir it lived in is not"


def test_the_sweep_is_a_no_op_on_a_root_with_nothing_to_sweep(tmp_path):
    work_root = tmp_path / "work"
    (work_root / "sess-clean" / "execution").mkdir(parents=True)
    r = run_shell(f'sweep_codex_homes "{work_root}"')
    assert r.returncode == 0, r.stderr
    r = run_shell('sweep_codex_homes "/no/such/root"')
    assert r.returncode == 0, r.stderr


def test_cleanup_is_idempotent_and_safe_when_the_home_was_never_created(tmp_path):
    """It runs from an EXIT trap, which fires on paths where nothing was ever seeded."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    for _ in range(2):
        r = run_shell(f'clean_codex_home "{run_dir}/codex-home" "{run_dir}"')
        assert r.returncode == 0, r.stderr


@pytest.mark.parametrize(
    ("how", "why"),
    [
        ("exit 0", "a clean exit"),
        ("exit 7", "a failing exit"),
        ("kill -TERM $$", "a signal"),
    ],
)
def test_the_home_is_removed_on_every_terminal_path(tmp_path, how, why):
    """The trap, not a remembered call at each exit. A credential left behind by the
    one path nobody thought about is the whole failure mode."""
    run_dir, home = _home_with_rollout(tmp_path)
    run_shell(
        f'trap \'clean_codex_home "{home}" "{run_dir}"\' EXIT INT TERM; {how}'
    )
    assert not home.exists(), f"the home survived {why}"
    assert not (home / "auth.json").exists()


# --- materialization into the run-dir ------------------------------------------------


def test_materialize_binding_writes_the_codex_home_into_the_run_dir(tmp_path):
    """The boundary's root is the DESCRIPTOR's choice, not the materializer's.

    A boundary that doubles as the harness's state directory holds the credential and
    the session record, so it must not live in the worker's git tree — and the launcher
    must be told, rather than inferring it from a rule written twice.
    """
    worker_root = tmp_path / "hive"
    run_dir = tmp_path / "execution"
    worker_root.mkdir()
    run_dir.mkdir()
    import hashlib

    def d(content: str) -> str:
        return "sha256:" + hashlib.sha256(content.encode()).hexdigest()

    toml, rules = 'default_permissions = "hive-worker"\n', "prefix_rule()\n"
    plan = {
        "binding": {
            "config_path": "codex-home",
            "config_root": "run",
            "config_directory": True,
            "config_digest": "sha256:" + "0" * 64,
            "config_files": [
                {"path": "config.toml", "content": toml, "digest": d(toml)},
                {"path": "rules/hive.rules", "content": rules, "digest": d(rules)},
            ],
        }
    }
    r = run_shell(
        f"materialize_binding '{json.dumps(plan)}' '{worker_root}' '{run_dir}'"
    )
    assert r.returncode == 0, r.stderr
    home = run_dir / "codex-home"
    assert (home / "config.toml").read_text() == toml
    assert (home / "rules" / "hive.rules").read_text() == rules
    assert stat.S_IMODE(home.stat().st_mode) == 0o700
    assert stat.S_IMODE((home / "config.toml").stat().st_mode) == 0o600
    assert not (worker_root / "codex-home").exists(), "never in the worker's tree"


def test_materialize_binding_refuses_when_the_bytes_that_landed_are_not_the_approved_ones(
    tmp_path,
):
    """A write that succeeded and a file that says what we meant are different claims,
    and only the second one is a boundary. Per FILE, not only over the manifest."""
    run_dir = tmp_path / "execution"
    run_dir.mkdir()
    plan = {
        "binding": {
            "config_path": "codex-home",
            "config_root": "run",
            "config_directory": True,
            "config_digest": "sha256:" + "0" * 64,
            "config_files": [
                {"path": "config.toml", "content": "x\n", "digest": "sha256:" + "b" * 64},
            ],
        }
    }
    r = run_shell(f"materialize_binding '{json.dumps(plan)}' '{tmp_path}' '{run_dir}'")
    assert r.returncode != 0
    assert "not the ones that were approved" in r.stderr


def test_a_run_rooted_boundary_refuses_when_no_run_dir_is_given(tmp_path):
    """Falling back to the worker root would put the credential in a git tree."""
    plan = {
        "binding": {
            "config_path": "codex-home",
            "config_root": "run",
            "config_directory": True,
            "config_digest": "sha256:" + "0" * 64,
            "config_files": [
                {"path": "config.toml", "content": "x\n", "digest": "sha256:" + "0" * 64}
            ],
        }
    }
    r = run_shell(f"materialize_binding '{json.dumps(plan)}' '{tmp_path}' ''")
    assert r.returncode != 0
    assert "run-dir" in r.stderr
