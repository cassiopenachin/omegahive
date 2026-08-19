"""Shared builders for harness-binding tests.

One place, because every harness test now needs a descriptor and a route pinned to it,
and a per-file copy of that pair would drift the moment the descriptor model gains a
field — which is exactly the drift the digest pin exists to catch elsewhere.

The `fake.v1` descriptor here is deliberately NOT shipped in `harness-bindings/`. A
descriptor that ships is a boundary an operator can pin a live route to, and a fake
boundary with `status: proven` is the one thing that must never be reachable from a real
catalog. Tests point `HIVE_BINDINGS_REPO_DIR` at a temp dir instead.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from omegahive.harness.bindings import (
    HarnessBinding,
    binding_digest,
    materialize,
)
from omegahive.harness.records import RefusalError

REPO = Path(__file__).resolve().parents[1]
_NULL_DIGEST = "sha256:" + "0" * 64
SHIPPED_BINDINGS = REPO / "harness-bindings"


def shipped(binding_id: str) -> bytes:
    return (SHIPPED_BINDINGS / f"{binding_id}.json").read_bytes()


def shipped_map() -> dict[str, bytes]:
    return {p.stem: p.read_bytes() for p in sorted(SHIPPED_BINDINGS.glob("*.json"))}


def a_class(pc: str, **over: Any) -> dict[str, Any]:
    """A minimally VALID class binding: one enforceable mechanism, one local probe."""
    base: dict[str, Any] = {
        "policy_class": pc,
        "title": f"{pc} title",
        "mechanisms": [
            {
                "kind": "settings-deny",
                "rules": [f"Bash(*{pc.lower()}-token*)"],
                "detail": f"{pc} deny",
            }
        ],
        "probes": [
            {
                "id": f"{pc.lower()}-rules-present",
                "kind": "rule-present",
                "rules": [f"Bash(*{pc.lower()}-token*)"],
                "expect": "present",
            }
        ],
        "residual": None,
    }
    base.update(over)
    return base


def descriptor(**over: Any) -> dict[str, Any]:
    """A complete, coverage-passing, `proven` descriptor.

    It binds the `claude-code` harness so the route builders in the harness tests keep
    naming a real adapter, but its id is `fake.v1` and it never ships — a boundary a
    live catalog could pin must be one that was actually probed.
    """
    base: dict[str, Any] = {
        "schema_version": 1,
        "binding_id": "fake.v1",
        "harness": "claude-code",
        "captured_at": "2026-08-14",
        "config_path": ".claude/settings.local.json",
        "config_format": "claude-code-settings",
        "required_flags": [["--setting-sources", "project,local"]],
        "command_mode_flag": "--setting-sources",
        "forbidden_argv_tokens": ["--dangerously-skip-permissions"],
        "known_command_modes": ["project,local", "user,project,local"],
        "safe_command_modes": ["project,local"],
        "classes": [a_class(pc) for pc in ("P1", "P2", "P3", "P4")],
        "status": "proven",
        "verification": {
            "deployment": "test",
            # What tests/fixtures/fake_harness.sh actually prints, so the supervisor's
            # boundary-evidence check compares like with like. A fixture that recorded a
            # version the fixture harness does not report would make every e2e launch
            # refuse -- which is the check working, and useless noise here.
            # `fake_harness.sh --version` prints `fake-harness 9.9.9`, and the shared
            # parser now picks the version out of field two rather than recording the
            # product name from field one — which is what `codex --version` needed and
            # what this fixture had been quietly documenting as correct.
            "harness_version": "9.9.9",
            "ran_at": "2026-08-14",
            "probe_record": "tests/harness_fixtures.py",
            "outcome": "pass",
        },
        "note": None,
    }
    base.update(over)
    # `check_status` refuses a `proven` descriptor whose recorded config_digest is not the
    # one it renders today. Computing it here keeps every fixture honest by construction
    # rather than by remembering — a hard-coded digest would break on any change to
    # `a_class`, and a test that WANTS the mismatch overrides `verification` explicitly.
    if isinstance(base.get("verification"), dict) and "config_digest" not in base["verification"]:
        probe = dict(base)
        probe["verification"] = {**base["verification"], "config_digest": _NULL_DIGEST}
        try:
            rendered = materialize(HarnessBinding(**probe), extra_dirs=[]).digest
        except (ValueError, RefusalError):
            rendered = _NULL_DIGEST
        base["verification"] = {**base["verification"], "config_digest": rendered}
    return base


def descriptor_bytes(**over: Any) -> bytes:
    return json.dumps(descriptor(**over), indent=2).encode("utf-8")


def descriptors_map(**over: Any) -> dict[str, bytes]:
    raw = descriptor_bytes(**over)
    return {json.loads(raw)["binding_id"]: raw}


def pins(**over: Any) -> dict[str, str]:
    """The two catalog fields that pin a route to a descriptor's exact bytes."""
    raw = descriptor_bytes(**over)
    return {
        "binding_id": json.loads(raw)["binding_id"],
        "binding_digest": binding_digest(raw),
    }


def write_descriptors(directory: Path, **over: Any) -> dict[str, str]:
    """Write the fake descriptor into `directory` for a shell-level test."""
    directory.mkdir(parents=True, exist_ok=True)
    raw = descriptor_bytes(**over)
    doc = json.loads(raw)
    (directory / f"{doc['binding_id']}.json").write_bytes(raw)
    return {"binding_id": doc["binding_id"], "binding_digest": binding_digest(raw)}
