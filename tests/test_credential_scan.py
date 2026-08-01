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
    load_manifest_file,
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
def manifest() -> str:
    return _MANIFEST


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
    with pytest.raises(ManifestError, match="claimed by two rows"):
        load_manifest(
            "services:\n"
            "  a: {compose_services: [ui], allowed: [X]}\n"
            "  b: {compose_services: [ui], allowed: [Y]}\n"
        )


def test_a_row_with_no_compose_services_is_refused():
    with pytest.raises(ManifestError, match="declares no `compose_services:`"):
        load_manifest("services:\n  a: {allowed: [X]}\n")


def test_a_scalar_compose_services_is_refused():
    """`compose_services: cli` iterates as CHARACTERS, registering rows for 'c', 'l', 'i'
    — after which the real service reports UNMAPPED and the message says no row declares
    it, while one plainly does. Refuse the shape instead."""
    with pytest.raises(ManifestError, match="must be a list"):
        load_manifest("services:\n  a: {compose_services: cli, allowed: [X]}\n")


def test_malformed_yaml_is_a_manifest_error_not_a_traceback():
    with pytest.raises(ManifestError, match="not parseable as YAML"):
        load_manifest("services:\n  a: {compose_services: [ui]\n   oops\n")


def test_an_unknown_group_is_refused():
    """A typo in `includes:` must not silently narrow a whitelist into false over-scope."""
    with pytest.raises(ManifestError, match="not defined"):
        load_manifest("services:\n  a: {compose_services: [ui], includes: [nope]}\n")


def test_the_observation_record_has_nowhere_to_put_a_value():
    """The never-print-a-value constraint, asserted rather than trusted: the type the scan
    consumes carries names only, so a value cannot reach the renderer even by mistake."""
    assert set(Observation.__dataclass_fields__) == {"container", "service", "env_keys"}
    obs = Observation.from_json(
        {"container": "c", "service": "ui", "env_keys": ["A"], "values": {"A": "s3cret"}}
    )
    assert obs.env_keys == ("A",)
    assert "s3cret" not in repr(obs)


def test_main_uses_the_manifest_carried_on_stdin_and_not_the_packaged_one(
    monkeypatch, capsys
):
    """The host collector sends the checkout's manifest text with the observations, so an
    edited-but-not-rebuilt manifest can never be scanned against silently.

    The observation is chosen so the two manifests DISAGREE: `probe-svc` exists only in the
    stdin manifest, so a clean verdict is reachable only if that manifest was honoured — the
    packaged one would report UNMAPPED and exit 1. An observation both manifests judge the
    same way would pass whether or not the stdin text was read at all.
    """
    payload = {
        "manifest": "services:\n  probe: {compose_services: [probe-svc], allowed: [ONLY_HERE]}\n",
        "observations": [
            {"container": "c1", "service": "probe-svc", "env_keys": ["ONLY_HERE"]}
        ],
    }
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps(payload)))
    assert main([]) == 0
    assert "probe-svc" in capsys.readouterr().out


def test_main_refuses_an_empty_scan(monkeypatch):
    """Nothing scanned is not the same as nothing wrong."""
    monkeypatch.setattr("sys.stdin", _Stdin("   "))
    assert main([]) == 1


def test_main_refuses_an_empty_observation_list(monkeypatch):
    """An empty LIST makes the same claim as empty stdin, and `clean` is the one answer
    that must not come back from a scan that looked at nothing."""
    monkeypatch.setattr("sys.stdin", _Stdin(json.dumps({"manifest": _MANIFEST,
                                                        "observations": []})))
    assert main([]) == 1


def test_every_compose_service_is_declared_exactly_once():
    """The regression that keeps the manifest from going decorative again: add a service
    to docker-compose.yml without a row here and this fails, rather than the scan quietly
    reporting UNMAPPED on a host months later."""
    compose = yaml.safe_load((REPO / "docker-compose.yml").read_text())
    declared = load_manifest_file(REPO / "secrets-manifest.yaml")
    undeclared = sorted(set(compose["services"]) - set(declared))
    assert not undeclared, f"compose services with no secrets-manifest.yaml row: {undeclared}"
    unknown = sorted(set(declared) - set(compose["services"]))
    assert not unknown, f"manifest rows naming services that do not exist: {unknown}"
