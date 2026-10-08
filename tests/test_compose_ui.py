"""The ui container can reach the operation service's socket on an SELinux host.

SELinux checks a Unix-socket connection against the listening process's domain, not only
the socket file's label: `container_t` may not connect to the service's `unconfined_t`
however the file is labelled (measured on Beastie, Fedora enforcing). So the ui service
alone runs with label separation disabled, and no other service does.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]


def _services() -> dict:
    return yaml.safe_load((REPO / "docker-compose.yml").read_text())["services"]


def test_the_ui_runs_without_selinux_label_separation():
    assert "label=disable" in _services()["ui"].get("security_opt", [])


def test_no_other_service_drops_label_separation():
    others = [name for name, service in _services().items()
              if name != "ui" and "label=disable" in (service.get("security_opt") or [])]
    assert others == []
