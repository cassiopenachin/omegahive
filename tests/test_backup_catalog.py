"""The route catalog is the one store that had no backup.

The spine is dumped by the `backup` compose service and the workspace is bundled by
`deploy/git_bundle.sh`. The catalog was in neither: it is a host file no container can see,
it is deliberately not in git — committing one host's answer would make another deployment
inherit routes it never approved — and it is not reconstructible, because it records which
models a host may spend money on and under which credential pool.

So it is snapshotted beside the other two, and these tests hold that script to the three
properties that make the backup worth having: it captures, it does not leak, and it does
not spend the retention window on identical copies.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "deploy" / "git_bundle.sh"


def _hub(tmp_path: Path) -> Path:
    """A bare repo with one commit: `git bundle` refuses an empty one, and the script's
    `set -eu` would then exit before reaching the catalog step at all."""
    seed = tmp_path / "seed"
    seed.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid", "PATH": "/usr/bin:/bin"}
    subprocess.run(["git", "init", "--quiet", str(seed)], check=True, timeout=30)
    (seed / "a").write_text("x")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(seed), "commit", "--quiet", "-m", "seed"],
                   check=True, timeout=30, env=env)
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(hub)],
                   check=True, timeout=60)
    return hub


def _run(tmp_path: Path, hub: Path, catalog: Path | None) -> str:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "OMEGAHIVE_HUB_REPO": str(hub),
           "OMEGAHIVE_BACKUP_DIR": str(tmp_path / "backups")}
    if catalog is not None:
        env["HIVE_ROUTE_CATALOG"] = str(catalog)
    out = subprocess.run(["sh", str(BUNDLE)], capture_output=True, text=True,
                         timeout=120, env=env)
    assert out.returncode == 0, out.stdout + out.stderr
    return out.stdout


def test_the_catalog_is_captured_and_never_world_readable(tmp_path):
    hub = _hub(tmp_path)
    catalog = tmp_path / "routes.json"
    catalog.write_text(json.dumps({"schema_version": 2, "routes": [{"name": "r"}]}))
    _run(tmp_path, hub, catalog)

    snaps = list((tmp_path / "backups").glob("routes-*.json"))
    assert len(snaps) == 1
    assert json.loads(snaps[0].read_text())["routes"][0]["name"] == "r"
    # The live file is 0600 and the copy of it must be too, from the instant it exists --
    # which is why the script uses a umask rather than a chmod after the fact.
    assert oct(snaps[0].stat().st_mode)[-3:] == "600"


def test_an_unchanged_catalog_does_not_consume_the_retention_window(tmp_path):
    """It changes rarely. Daily copies would fill the window with identical files and evict
    the older, genuinely different one -- the only copy worth restoring."""
    hub = _hub(tmp_path)
    catalog = tmp_path / "routes.json"
    catalog.write_text('{"schema_version": 2, "routes": []}')
    _run(tmp_path, hub, catalog)
    second = _run(tmp_path, hub, catalog)

    assert "unchanged" in second
    assert len(list((tmp_path / "backups").glob("routes-*.json"))) == 1


def test_a_host_with_no_catalog_still_backs_up_its_workspace(tmp_path):
    """A host that configures no worker routes has no catalog, and the workspace bundle is
    still the point of the run. Absence is a note, not a failure."""
    hub = _hub(tmp_path)
    out = _run(tmp_path, hub, tmp_path / "definitely-absent.json")
    assert "skipping catalog snapshot" in out
    assert list((tmp_path / "backups").glob("*.bundle")), "the bundle must still be written"
