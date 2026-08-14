"""port-sha-pin-fallback: the resolver's three branches and the validation floor.

Exercises the outcome the order names — the pin is never empty, and empty is still refused
by validation — through the candidate's own code. It calls the qual config builder rather
than any particular helper signature, so an implementation that reaches the outcome
differently passes.

Run from the candidate repo root, e.g.
    uv run --frozen python port_sha_resolver_property.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))
sys.path.insert(0, os.path.abspath("."))

PROBE = r"""
import json, os, sys
sys.path.insert(0, os.path.abspath("src"))
sys.path.insert(0, os.path.abspath("."))
from qual.loader import load_scenario_set
from qual.record import build_config
from pathlib import Path
loaded = load_scenario_set(Path("qual/scenarios"))
cfg = build_config(
    loaded=loaded, image_ref="probe:0", image_id="sha256:deadbeef", image_role="v0a",
    models=["probe"], reps=1, matrix_id="probe", date="2026-01-01",
)
print(json.dumps({"pin": cfg.get("port_library_sha")}))
"""


def _run(cwd: Path, env: dict[str, str]) -> str:
    out = subprocess.run(
        [sys.executable, "-c", PROBE], cwd=cwd, env=env, capture_output=True, text=True, check=False
    )
    if out.returncode != 0:
        print(f"FAIL probe failed in {cwd}: {out.stderr.strip()[-1500:]}")
        raise SystemExit(1)
    return json.loads(out.stdout.strip().splitlines()[-1])["pin"] or ""


def main() -> int:
    root = Path.cwd()
    base_env = {k: v for k, v in os.environ.items() if k != "OMEGAHIVE_PORT_LIBRARY_SHA"}
    failures: list[str] = []

    # Branch 1 — a repository is present.
    pin_repo = _run(root, base_env)
    if not pin_repo:
        failures.append("branch 1: the pin is empty with a repository present")

    with tempfile.TemporaryDirectory() as tmp:
        mirror = Path(tmp) / "tree"
        subprocess.run(
            ["git", "-C", str(root), "worktree", "list"], capture_output=True, check=False
        )
        # A copy with no repository metadata: branches 2 and 3 are what remain.
        subprocess.run(
            ["cp", "-a", str(root), str(mirror)], capture_output=True, check=False
        )
        for junk in (".git", ".venv"):
            subprocess.run(["rm", "-rf", str(mirror / junk)], capture_output=True, check=False)

        # Branch 2 — no repository, but the environment names the sha.
        env2 = dict(base_env, OMEGAHIVE_PORT_LIBRARY_SHA="cafef00d" * 5)
        pin_env = _run(mirror, env2)
        if pin_env != "cafef00d" * 5:
            failures.append(f"branch 2: the environment override was not used (got {pin_env!r})")

        # Branch 3 — neither: an honest sentinel derived from a pin the config already holds.
        pin_sentinel = _run(mirror, base_env)
        if not pin_sentinel:
            failures.append("branch 3: the pin is empty with no repository and no override")
        elif "deadbeef" not in pin_sentinel:
            failures.append(
                f"branch 3: the fallback {pin_sentinel!r} is not derived from the image pin"
            )

    from qual.record import validate_record  # noqa: E402 — after the sys.path setup above

    with tempfile.TemporaryDirectory() as tmp:
        rec = Path(tmp) / "rec"
        rec.mkdir()
        cfg = {
            "matrix_id": "m", "date": "2026-01-01", "image_ref": "r", "image_id": "i",
            "image_role": "v0a", "reps": 1, "model_profiles": ["m"], "scenario_set_sha": "s",
            "persona_hashes": {"p": "h"}, "port_library_sha": "",
        }
        (rec / "config.json").write_text(json.dumps(cfg))
        if not validate_record(rec):
            failures.append("validation floor: an empty pin was accepted as valid")

    for f in failures:
        print(f"FAIL {f}")
    if failures:
        return 1
    print("ok — three resolver branches non-empty, empty still refused by validation")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
