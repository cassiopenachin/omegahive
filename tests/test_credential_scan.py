"""The credential-scope scan's judgement (src/omegahive/deploy/credential_scan.py).

The shell half collects container env-var names; everything that DECIDES lives in the
module, so the asymmetry, the failure modes, and the never-a-value constraint are testable
without a container. The last test in this file is the one that keeps the manifest honest
over time: every compose service must be declared by exactly one row.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from omegahive.deploy.credential_scan import (
    MISSING,
    OK,
    OVER,
    UNMAPPED,
    ManifestError,
    Observation,
    load_manifest,
    main,
    scan,
)

REPO = Path(__file__).resolve().parents[1]


class _Stdin:
    """Minimal stdin stand-in — main() only ever reads."""

    def __init__(self, text: str) -> None:
        self._text = text

    def read(self) -> str:
        return self._text


_MANIFEST = """
groups:
  runtime: [PATH, HOME]
services:
  reader:
    compose_services: [ui, board-view]
    includes: [runtime]
    allowed: [OMEGAHIVE_DATABASE_URL, OMEGAHIVE_UI_BASE_PATH]
  writer:
    compose_services: [cli]
    allowed: [OMEGAHIVE_GATEWAY_DATABASE_URL]
"""


@pytest.fixture
def manifest(tmp_path: Path) -> Path:
    path = tmp_path / "secrets-manifest.yaml"
    path.write_text(_MANIFEST)
    return path


def _obs(service: str, *keys: str, container: str = "c1") -> Observation:
    return Observation(container=container, service=service, env_keys=tuple(keys))


def test_declared_keys_are_clean(manifest):
    report = scan([_obs("ui", "PATH", "HOME", "OMEGAHIVE_DATABASE_URL",
                        "OMEGAHIVE_UI_BASE_PATH")], manifest)
    assert [f.status for f in report.findings] == [OK]
    assert report.over_scope == []


def test_an_undeclared_key_is_over_scope_and_fatal(manifest):
    report = scan([_obs("ui", "PATH", "TELEGRAM_BOT_TOKEN")], manifest)
    (finding,) = report.findings
    assert finding.status == OVER
    assert finding.fatal
    # It must name the service AND the key, or the operator cannot act on it.
    assert "ui" in finding.render() and "TELEGRAM_BOT_TOKEN" in finding.render()


def test_a_declared_but_absent_key_is_reported_and_not_fatal(manifest):
    """The asymmetry: over-scope fails, under-declared informs. Failing on absent keys
    would make an unset optional variable a deploy blocker and train the operator to
    ignore the scan."""
    report = scan([_obs("ui", "PATH", "HOME", "OMEGAHIVE_DATABASE_URL")], manifest)
    (finding,) = report.findings
    assert finding.status == MISSING
    assert not finding.fatal
    assert "OMEGAHIVE_UI_BASE_PATH" in finding.render()
    assert report.over_scope == []


def test_a_service_with_no_row_is_fatal(manifest):
    """An unmapped service is an undeclared one, not an exempt one — otherwise adding a
    service would be the way to escape the scan."""
    report = scan([_obs("notifier", "TELEGRAM_BOT_TOKEN")], manifest)
    (finding,) = report.findings
    assert finding.status == UNMAPPED
    assert finding.fatal


def test_groups_apply_only_where_they_are_included(manifest):
    """`writer` includes no groups, so the runtime names it never declared are over-scope
    there even though `reader` may carry them. A group is a declaration, not a global."""
    report = scan([_obs("cli", "PATH", "OMEGAHIVE_GATEWAY_DATABASE_URL")], manifest)
    (finding,) = report.findings
    assert finding.status == OVER
    assert finding.keys == ("PATH",)


def test_two_rows_claiming_one_service_is_refused(tmp_path: Path):
    """Ambiguity is not resolved silently: with two candidate whitelists there is no
    single answer to what the service may carry."""
    path = tmp_path / "m.yaml"
    path.write_text(
        "services:\n"
        "  a: {compose_services: [ui], allowed: [X]}\n"
        "  b: {compose_services: [ui], allowed: [Y]}\n"
    )
    with pytest.raises(ManifestError, match="claimed by two rows"):
        load_manifest(path)


def test_a_row_with_no_compose_services_is_refused(tmp_path: Path):
    path = tmp_path / "m.yaml"
    path.write_text("services:\n  a: {allowed: [X]}\n")
    with pytest.raises(ManifestError, match="declares no `compose_services:`"):
        load_manifest(path)


def test_an_unknown_group_is_refused(tmp_path: Path):
    """A typo in `includes:` must not silently narrow a whitelist into false over-scope."""
    path = tmp_path / "m.yaml"
    path.write_text("services:\n  a: {compose_services: [ui], includes: [nope]}\n")
    with pytest.raises(ManifestError, match="not defined"):
        load_manifest(path)


def test_the_observation_record_has_nowhere_to_put_a_value():
    """The never-print-a-value constraint, asserted rather than trusted: the type the scan
    consumes carries names only, so a value cannot reach the renderer even by mistake."""
    assert set(Observation.__dataclass_fields__) == {"container", "service", "env_keys"}
    obs = Observation.from_json(
        {"container": "c", "service": "ui", "env_keys": ["A"], "values": {"A": "s3cret"}}
    )
    assert obs.env_keys == ("A",)
    assert "s3cret" not in repr(obs)


def test_main_reads_the_manifest_carried_on_stdin(manifest, monkeypatch, capsys):
    """The host collector sends the checkout's manifest text with the observations, so an
    edited-but-not-rebuilt manifest can never be scanned against silently."""
    payload = {
        "manifest": manifest.read_text(),
        "observations": [{"container": "c1", "service": "ui", "env_keys": ["NOPE"]}],
    }
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(payload)))
    assert main([]) == 1
    assert "NOPE" in capsys.readouterr().out


def test_main_refuses_an_empty_scan(monkeypatch):
    """Nothing scanned is not the same as nothing wrong."""
    monkeypatch.setattr("sys.stdin", _Stdin("   "))
    assert main([]) == 1


def test_every_compose_service_is_declared_exactly_once():
    """The regression that keeps the manifest from going decorative again: add a service
    to docker-compose.yml without a row here and this fails, rather than the scan quietly
    reporting UNMAPPED on a host months later."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    declared = load_manifest(REPO / "secrets-manifest.yaml")
    undeclared = sorted(set(compose["services"]) - set(declared))
    assert not undeclared, f"compose services with no secrets-manifest.yaml row: {undeclared}"
    unknown = sorted(set(declared) - set(compose["services"]))
    assert not unknown, f"manifest rows naming services that do not exist: {unknown}"
