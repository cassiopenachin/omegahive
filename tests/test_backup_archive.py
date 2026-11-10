"""Directories that no Git bundle can see are archived beside the bundles.

The council room keeps uncommitted drafts in a shared workspace clone and its own
configuration in a private directory. A bundle of the bare hub holds neither: drafts exist
only in the clone's working tree until a seat commits them on Cassio's word, and the room
config is deliberately not in Git. So `deploy/git_bundle.sh` archives the paths named
in `OMEGAHIVE_ARCHIVE_SOURCES` into the same backup directory, on the same schedule and
retention, and these tests hold it to what makes that archive worth having: it captures
what Git does not, it never exists world-readable, a missing source fails loudly, and a
host that names no sources is unchanged.
"""

from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BUNDLE = REPO / "deploy" / "git_bundle.sh"
GIT_ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@e.invalid",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@e.invalid", "PATH": "/usr/bin:/bin"}


def _hub(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    subprocess.run(["git", "init", "--quiet", str(seed)], check=True, timeout=30)
    (seed / "a").write_text("x")
    subprocess.run(["git", "-C", str(seed), "add", "-A"], check=True, timeout=30)
    subprocess.run(["git", "-C", str(seed), "commit", "--quiet", "-m", "seed"],
                   check=True, timeout=30, env=GIT_ENV)
    hub = tmp_path / "hub.git"
    subprocess.run(["git", "clone", "--quiet", "--bare", str(seed), str(hub)],
                   check=True, timeout=60)
    return hub


def _run(tmp_path: Path, hub: Path, sources: str | None, keep: int = 14,
         check: bool = True) -> subprocess.CompletedProcess[str]:
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
           "OMEGAHIVE_HUB_REPO": str(hub),
           "OMEGAHIVE_BACKUP_DIR": str(tmp_path / "backups"),
           "OMEGAHIVE_BACKUP_KEEP": str(keep),
           "HIVE_ROUTE_CATALOG": str(tmp_path / "no-catalog.json")}
    if sources is not None:
        env["OMEGAHIVE_ARCHIVE_SOURCES"] = sources
    out = subprocess.run(["sh", str(BUNDLE)], capture_output=True, text=True,
                         timeout=120, env=env)
    if check:
        assert out.returncode == 0, out.stdout + out.stderr
    return out


def _clone_with_draft(tmp_path: Path, hub: Path) -> Path:
    clone = tmp_path / "council-clone"
    subprocess.run(["git", "clone", "--quiet", str(hub), str(clone)], check=True, timeout=60)
    (clone / "draft.md").write_text("uncommitted draft\n")
    return clone


def _archives(tmp_path: Path) -> list[Path]:
    return sorted((tmp_path / "backups").glob("files-*.tar.gz"))


def test_an_uncommitted_draft_and_a_config_file_are_archived_private(tmp_path):
    hub = _hub(tmp_path)
    clone = _clone_with_draft(tmp_path, hub)
    config = tmp_path / "room-config"
    config.mkdir()
    (config / "openclaw.json").write_text("{}\n")
    _run(tmp_path, hub, f"{clone}:{config}")

    archives = _archives(tmp_path)
    assert len(archives) == 1
    # Config may hold credentials, so the archive is 0600 from the instant it exists.
    assert oct(archives[0].stat().st_mode)[-3:] == "600"
    with tarfile.open(archives[0]) as tar:
        names = tar.getnames()
    assert "council-clone/draft.md" in names
    assert "room-config/openclaw.json" in names
    assert list((tmp_path / "backups").glob("*.bundle")), "the bundle is still written"


def test_a_single_file_source_leaves_its_secret_siblings_out(tmp_path):
    """The room's config directory also holds its secrets file. Naming the config file
    itself archives that file under its own name and nothing beside it."""
    hub = _hub(tmp_path)
    config = tmp_path / "room-config"
    config.mkdir()
    (config / "openclaw.json").write_text("{}\n")
    (config / "secrets.json").write_text('{"token": "x"}\n')
    _run(tmp_path, hub, str(config / "openclaw.json"))

    with tarfile.open(_archives(tmp_path)[0]) as tar:
        names = tar.getnames()
    assert names == ["openclaw.json"]


def test_a_host_that_names_no_sources_is_unchanged(tmp_path):
    hub = _hub(tmp_path)
    out = _run(tmp_path, hub, None)
    assert "no archive sources" in out.stdout
    assert _archives(tmp_path) == []
    assert list((tmp_path / "backups").glob("*.bundle"))


def test_a_missing_source_fails_loudly_after_the_bundle(tmp_path):
    """A configured directory that is gone is a misconfiguration; archiving the rest and
    exiting 0 would report a backup that silently lost the one store it was added for."""
    hub = _hub(tmp_path)
    out = _run(tmp_path, hub, str(tmp_path / "gone"), check=False)
    assert out.returncode != 0
    assert "archive source not found" in out.stderr
    assert list((tmp_path / "backups").glob("*.bundle")), "the bundle runs before the archive"
    assert _archives(tmp_path) == []


def test_archives_rotate_on_the_bundle_retention(tmp_path):
    hub = _hub(tmp_path)
    clone = _clone_with_draft(tmp_path, hub)
    backups = tmp_path / "backups"
    backups.mkdir()
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        (backups / f"files-{stamp}.tar.gz").write_bytes(b"old")
    _run(tmp_path, hub, str(clone), keep=2)

    archives = _archives(tmp_path)
    assert len(archives) == 2
    assert archives[0].name == "files-20200103T000000Z.tar.gz"
