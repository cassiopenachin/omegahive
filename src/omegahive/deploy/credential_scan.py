"""Credential-scope scan: what each container actually carries, against what
`secrets-manifest.yaml` says it may (deployment spec §4/§7 step 7; OPERATIONS pain 10).

The manifest has declared per-service env-var names since 2026-07-13 and nothing checked
them, so a wave could add rows — or add a variable and not the row — with no signal at
all. This is the check. `scripts/credential_scope_scan.sh` collects the observations on
the host (no container can see another's environment) and pipes them here as JSON; this
module owns the manifest, the diff, and the verdict, so the part with the judgement in it
is testable without a container.

**Key NAMES only.** Nothing in this module reads, receives, prints, logs or compares a
value: the observation record carries `env_keys`, a list of names, and there is no field
for a value to arrive in. That is the whole lesson of the incident behind pain 10 — a
`compose config` that printed a bot token in plaintext into a transcript — so the output
here is safe to paste into a shared terminal, and the constraint is asserted by
tests/test_credential_scan.py rather than trusted.

Asymmetric by design:

  * a key in the container and NOT in its manifest row is **over-scope** — the container
    can see something nobody declared it may. Hard fail.
  * a key in the row and NOT in the container is **under-declared** — reported, never
    fatal. Most are simply optional (an unset `OMEGAHIVE_UI_BASE_URL` never materializes),
    and failing on them would train the operator to ignore the scan.
  * a running service with NO row at all is a hard fail. An unmapped service is not an
    unchecked service; it is an undeclared one, which is the thing being caught.

There is deliberately no exception list, and adding one is not the fix for a false
positive. A legitimately-present platform variable is fixed by declaring it in the row —
which is a diffable, reviewable line — never by teaching this scanner to look away.
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

OK, OVER, MISSING, UNMAPPED = "OK", "OVER", "MISSING", "UNMAPPED"


class ManifestError(Exception):
    """The manifest cannot be used as a whitelist — malformed, or ambiguous about a
    service. Distinct from a scan finding: nothing was checked, so nothing passed."""


@dataclass(frozen=True)
class Observation:
    """One running container's env-var NAMES. No value field exists, by construction."""

    container: str
    service: str
    env_keys: tuple[str, ...]

    @staticmethod
    def from_json(obj: dict[str, Any]) -> Observation:
        missing = {"container", "service", "env_keys"} - set(obj)
        if missing:
            raise ManifestError(f"observation is missing {sorted(missing)}: {obj!r}")
        return Observation(
            container=str(obj["container"]),
            service=str(obj["service"]),
            env_keys=tuple(sorted(str(k) for k in obj["env_keys"])),
        )


@dataclass(frozen=True)
class Finding:
    status: str
    container: str
    service: str
    row: str | None
    keys: tuple[str, ...] = ()

    @property
    def fatal(self) -> bool:
        return self.status in (OVER, UNMAPPED)

    def render(self) -> str:
        where = f"{self.service} ({self.container})"
        if self.status == UNMAPPED:
            return (
                f"[{UNMAPPED}] {where}: no row in secrets-manifest.yaml declares this "
                "service — add one (`compose_services:` lists the compose services a row "
                "covers). An undeclared service is not an exempt one."
            )
        if self.status == OVER:
            return (
                f"[{OVER}] {where} -> row '{self.row}': undeclared key names present: "
                f"{', '.join(self.keys)}"
            )
        if self.status == MISSING:
            return (
                f"[{MISSING}] {where} -> row '{self.row}': declared but absent (not a "
                f"failure): {', '.join(self.keys)}"
            )
        return f"[{OK}] {where} -> row '{self.row}': all key names declared"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)

    @property
    def over_scope(self) -> list[Finding]:
        return [f for f in self.findings if f.fatal]

    def render(self) -> str:
        n = len(self.findings)
        lines = [
            f"== credential scope: {n} running container(s) vs secrets-manifest.yaml ==",
            *(f.render() for f in self.findings),
            "",
        ]
        bad = len(self.over_scope)
        under = sum(1 for f in self.findings if f.status == MISSING)
        lines.append(
            f"== {bad} over-scope finding(s), {under} under-declared note(s) =="
            if bad
            else f"== clean: no container carries an undeclared key name "
            f"({under} under-declared note(s)) =="
        )
        lines.append(
            "key NAMES only — this scan never reads, prints or compares a value, so the "
            "output above is safe to share."
        )
        return "\n".join(lines)


def load_manifest(text: str) -> dict[str, tuple[str, frozenset[str]]]:
    """compose service name -> (declaring row name, the env-var names it may carry).

    Takes the manifest TEXT rather than a path: the host collector sends this checkout's
    file along with the observations, so an edited-but-not-rebuilt manifest can never be
    scanned against silently. `load_manifest_file` is the convenience wrapper for tests.

    Every row states the compose services it covers (`compose_services:`) because the two
    namings genuinely differ: one `gateway` row covers several compose services, and a row
    named for a service that had since been renamed would silently cover nothing.

    A row's whitelist is its own `allowed:` plus every `groups:` entry it `includes:`. The
    groups are how the base-image and shared-`.env` names get declared once instead of
    eight times — a group is still a written, diffable declaration, not an exemption: a
    row that does not include a group does not get its names.
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ManifestError(f"not parseable as YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ManifestError("manifest is not a mapping")
    rows = raw.get("services")
    if not isinstance(rows, dict):
        raise ManifestError("manifest has no `services:` mapping")

    groups = raw.get("groups") or {}
    if not isinstance(groups, dict):
        raise ManifestError("`groups:` must be a mapping of name -> list of env names")

    by_service: dict[str, tuple[str, frozenset[str]]] = {}
    for row_name, row in rows.items():
        if not isinstance(row, dict):
            raise ManifestError(f"row '{row_name}' is not a mapping")
        names = {str(k) for k in (row.get("allowed") or [])}
        for group in row.get("includes") or []:
            if str(group) not in groups:
                raise ManifestError(
                    f"row '{row_name}' includes group '{group}', which is not defined "
                    "under `groups:`"
                )
            names |= {str(k) for k in (groups[str(group)] or [])}
        allowed = frozenset(names)
        services = row.get("compose_services")
        if not services:
            raise ManifestError(
                f"row '{row_name}' declares no `compose_services:` — a row that names no "
                "service cannot whitelist one"
            )
        # A bare scalar (`compose_services: cli`) is an easy YAML slip and iterates as
        # CHARACTERS, registering rows for 'c', 'l', 'i' — after which the real service
        # reports UNMAPPED and the message says no row declares it, while one plainly does.
        if not isinstance(services, list):
            raise ManifestError(
                f"row '{row_name}': `compose_services:` must be a list, got "
                f"{type(services).__name__}"
            )
        for svc in services:
            svc = str(svc)
            if svc in by_service:
                raise ManifestError(
                    f"compose service '{svc}' is claimed by two rows "
                    f"('{by_service[svc][0]}' and '{row_name}') — the whitelist would be "
                    "ambiguous"
                )
            by_service[svc] = (str(row_name), allowed)
    return by_service


def load_manifest_file(path: Path) -> dict[str, tuple[str, frozenset[str]]]:
    """load_manifest for a path on disk — the form tests and ad-hoc checks want."""
    return load_manifest(path.read_text())


def scan(observations: list[Observation], manifest_text: str) -> Report:
    """Diff each observation against its row. All I/O is the caller's."""
    by_service = load_manifest(manifest_text)

    report = Report()
    for obs in sorted(observations, key=lambda o: (o.service, o.container)):
        declared = by_service.get(obs.service)
        if declared is None:
            report.findings.append(Finding(UNMAPPED, obs.container, obs.service, None))
            continue
        row, allowed = declared
        over = tuple(sorted(set(obs.env_keys) - allowed))
        if over:
            report.findings.append(Finding(OVER, obs.container, obs.service, row, over))
            continue
        absent = tuple(sorted(allowed - set(obs.env_keys)))
        status = MISSING if absent else OK
        report.findings.append(Finding(status, obs.container, obs.service, row, absent))
    return report


def main(argv: list[str] | None = None) -> int:
    """Read `{"manifest": <yaml text>, "observations": [...]}` on stdin; print the report.

    Exit 0 clean, 1 findings or unusable input. The collector
    (scripts/credential_scope_scan.sh) reserves exit 2 for "the scan could not run at all",
    which it decides before ever reaching this process.
    """
    if argv:
        print(f"unexpected arguments: {argv} (this reads stdin only)", file=sys.stderr)
        return 1

    payload = sys.stdin.read().strip()
    if not payload:
        print(
            "no observations on stdin — nothing was scanned, which is not the same as "
            "nothing being wrong",
            file=sys.stderr,
        )
        return 1
    try:
        data = json.loads(payload)
        manifest_text = data["manifest"]
        observations = [Observation.from_json(o) for o in data["observations"]]
    except (json.JSONDecodeError, ManifestError, KeyError, TypeError) as exc:
        print(f"could not read the observations: {exc}", file=sys.stderr)
        return 1
    if not isinstance(manifest_text, str):
        print("the `manifest` field must be the manifest's text", file=sys.stderr)
        return 1
    if not observations:
        # An empty LIST is the same claim as empty stdin: the collector found nothing to
        # look at. It refuses that case itself, so reaching here means something is wrong
        # with the handoff — and "clean" is the one answer that must not be printed.
        print("the observation list is empty — nothing was scanned", file=sys.stderr)
        return 1

    try:
        report = scan(observations, manifest_text)
    except ManifestError as exc:
        print(f"secrets-manifest.yaml is unusable as a whitelist: {exc}", file=sys.stderr)
        return 1

    print(report.render())
    return 1 if report.over_scope else 0


if __name__ == "__main__":
    sys.exit(main())
