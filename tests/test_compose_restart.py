"""The spine database comes back after a host reboot, with the services that need it.

On Beastie, `podman-restart.service` starts at boot exactly the containers whose restart
policy makes them should-start-on-boot. The ui and notifier were `unless-stopped`, but the
spine's postgres had no policy, so after a reboot the notifier looped on "failed to resolve
host 'postgres'" until someone started the database by hand (2026-11-13). The database must
carry the same policy as the long-running services that read it.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _services() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]


def test_the_spine_database_restarts_like_the_services_that_read_it():
    services = _services()
    assert services["postgres"].get("restart") == "unless-stopped"
    for reader in ("ui", "notifier"):
        assert services[reader].get("restart") == "unless-stopped"
