"""The experiment record (battery spec §8): a dated, config-pinned directory.

`qual/records/<date>-<matrix-id>/`:
  config.json   — the pins (scenario-set SHA, image ref/id, port-library SHA, persona
                  hashes, model profiles, reps, image role). A record is *valid* only if
                  all pins are present (`validate_record`) — the check shared by Mode A
                  (CI) and Mode B (the review instrument).
  <scenario>/<model>/rep-<n>/{metrics.json, llm_raw.txt, events.json}
  aggregate.md  — model × metric distributions (applicable subset per image role)
  cost.json     — per-rep + total tokens/USD/wall

Hashing reuses the canonical-JSON + sha256 idiom from `omegahive.port.keys`; writes reuse
its temp-file + fsync + os.replace atomicity so a crash never leaves a torn record.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from omegahive.port.keys import canonical_payload

from .aggregate import MetricsDistribution
from .capture import CaptureResult
from .loader import QUAL_ROOT, LoadedScenario
from .metrics import MetricsRow, serialize

# --- applicable metric sets (spec §7 two-image split) ------------------------

# Emission-discipline metrics — the v0a half. `total_tokens` is real whenever the run image
# carries the usage-logging patch (the hive image does; a pre-patch base image would report 0).
V0A_METRICS = [
    "pre_repair_parse_rate",
    "post_repair_parse_rate",
    "repair_dependency",
    "command_recognition",
    "silent_unknown_count",
    "pin_discipline_ok",
    "idle_ok",
    "idle_junk_op_count",
    "total_tokens",
    "total_wall_ms",
]
# Board-op metrics — need the hive board channel driven against a real board (v0b).
V0B_EXTRA = [
    "legal_op_rate",
    "rejection_recovered",
    "rejection_identical_retries",
    "total_usd",
]
# batch_order_ok is intentionally omitted from both: N/A for the as-shipped
# one-call-one-emit binding (it only bites bindings using the port batch envelope).

_LABELS = {
    "pre_repair_parse_rate": "pre-parse",
    "post_repair_parse_rate": "post-parse",
    "repair_dependency": "repair-dep",
    "command_recognition": "cmd-recog",
    "silent_unknown_count": "silent-unk",
    "pin_discipline_ok": "pin-ok",
    "idle_ok": "idle-ok",
    "idle_junk_op_count": "junk-ops",
    "total_wall_ms": "wall-ms",
    "legal_op_rate": "legal-op",
    "rejection_recovered": "recovered",
    "rejection_identical_retries": "retries",
    "total_tokens": "tokens",
    "total_usd": "usd",
}

REQUIRED_PINS = [
    "scenario_set_sha",
    "image_ref",
    "image_id",
    "port_library_sha",
    "persona_hashes",
    "model_profiles",
    "reps",
    "image_role",
    "matrix_id",
    "date",
]


@dataclass
class RepRecord:
    """One graded rep plus the raw artifacts to retain."""

    row: MetricsRow
    hard_fail_flags: list[str]
    capture: CaptureResult


# --- config pins -------------------------------------------------------------

def _resolve(ref: str) -> Path:
    p = Path(ref)
    return p if p.is_absolute() else QUAL_ROOT / ref


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def scenario_set_sha(loaded: list[LoadedScenario]) -> str:
    dumps = sorted((ls.scenario.model_dump(mode="json") for ls in loaded), key=lambda d: d["id"])
    return _sha256_bytes(canonical_payload({"scenarios": dumps}).encode("utf-8"))


def persona_hashes(loaded: list[LoadedScenario]) -> dict[str, str]:
    out: dict[str, str] = {}
    for ls in loaded:
        ref = ls.scenario.persona
        if ref not in out:
            path = _resolve(ref)
            out[ref] = _sha256_bytes(path.read_bytes()) if path.exists() else ""
    return out


def port_library_sha(image_id: str) -> str:
    """Provenance of the port client build (config pin) — a resolver chain, never empty.

    Most-to-least precise, first non-empty wins:
      1. ``git rev-parse HEAD`` — a repo is present (dev/CI on host).
      2. ``OMEGAHIVE_PORT_LIBRARY_SHA`` — injected by a deploy script at build/run time.
      3. ``image:<image_id>`` sentinel — in-container there is no ``.git`` and no env
         override, but the image digest already uniquely identifies the code, so it is
         honest provenance for that environment.
    """
    try:
        out = subprocess.run(
            ["git", "-C", str(QUAL_ROOT), "rev-parse", "HEAD"],
            capture_output=True, text=True, check=True,
        )
        sha = out.stdout.strip()
        if sha:
            return sha
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        pass
    env = os.environ.get("OMEGAHIVE_PORT_LIBRARY_SHA", "").strip()
    if env:
        return env
    return f"image:{image_id}"


def build_config(
    *,
    loaded: list[LoadedScenario],
    image_ref: str,
    image_id: str,
    image_role: str,
    models: list[str],
    reps: int,
    matrix_id: str,
    date: str,
) -> dict:
    return {
        "matrix_id": matrix_id,
        "date": date,
        "image_ref": image_ref,
        "image_id": image_id,
        "image_role": image_role,
        "reps": reps,
        "model_profiles": list(models),
        "scenario_set_sha": scenario_set_sha(loaded),
        "port_library_sha": port_library_sha(image_id),
        "persona_hashes": persona_hashes(loaded),
    }


# --- writing -----------------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _metrics_applicable(image_role: str) -> list[str]:
    return V0A_METRICS if image_role == "v0a" else V0A_METRICS + V0B_EXTRA


def _cell(dist: MetricsDistribution, metric: str) -> str:
    if metric in dist.numeric:
        s = dist.numeric[metric]
        if s.min == s.max:
            return f"{s.p50:.2f}"
        return f"{s.p50:.2f} [{s.min:.2f}–{s.max:.2f}]"
    if metric in dist.incidence:
        return f"{dist.incidence[metric] * 100:.0f}%"
    return "—"


def render_aggregate_md(distributions: list[MetricsDistribution], config: dict) -> str:
    role = config["image_role"]
    metrics = _metrics_applicable(role)
    lines = [
        f"# Qualification aggregate — {config['matrix_id']} ({config['date']})",
        "",
        f"Image `{config['image_ref']}` · role **{role}** · R={config['reps']} reps.",
        "",
    ]
    if role == "v0a":
        lines += [
            "Emission-discipline subset. Board-op metrics need the hive board channel driven "
            "against a real board (v0b) and are omitted here.",
            "",
        ]
    by_scenario: dict[str, list[MetricsDistribution]] = {}
    for d in distributions:
        by_scenario.setdefault(d.scenario_id, []).append(d)

    header = "| model | " + " | ".join(_LABELS.get(m, m) for m in metrics) + " |"
    sep = "|" + "---|" * (len(metrics) + 1)
    for scenario_id in sorted(by_scenario):
        lines += [f"## {scenario_id}", "", header, sep]
        for d in sorted(by_scenario[scenario_id], key=lambda x: x.model):
            lines.append("| " + d.model + " | " + " | ".join(_cell(d, m) for m in metrics) + " |")
        lines.append("")
    lines += ["_Batch-order sanity: N/A for the as-shipped one-call-one-emit binding._", ""]
    return "\n".join(lines) + "\n"


def _cost_summary(reps: list[RepRecord]) -> dict:
    per_rep = [
        {
            "scenario": r.row.scenario_id,
            "model": r.row.model,
            "rep": r.row.rep,
            "tokens": r.row.total_tokens,
            "usd": r.row.total_usd,
            "wall_ms": r.row.total_wall_ms,
        }
        for r in reps
    ]
    totals = {
        "tokens": sum(r.row.total_tokens for r in reps),
        "usd": sum(r.row.total_usd for r in reps),
        "wall_ms": sum(r.row.total_wall_ms for r in reps),
    }
    summary: dict = {"per_rep": per_rep, "totals": totals}
    if totals["tokens"] == 0:
        summary["note"] = (
            "tokens are 0 — the run image has no usage-logging patch (pre-patch base image); "
            "wall-clock only."
        )
    if totals["usd"] == 0:
        summary["note_usd"] = (
            "USD is 0 — token→USD pricing not yet applied (needs the price table)."
        )
    return summary


def write_record(
    out_dir: str | Path,
    config: dict,
    reps: list[RepRecord],
    distributions: list[MetricsDistribution],
) -> Path:
    root = Path(out_dir) / f"{config['date']}-{config['matrix_id']}"
    _atomic_write_text(root / "config.json", json.dumps(config, indent=2, sort_keys=True) + "\n")

    for rr in reps:
        rep_dir = root / rr.row.scenario_id / rr.row.model / f"rep-{rr.row.rep}"
        payload = {"metrics": serialize(rr.row), "hard_fail_flags": rr.hard_fail_flags}
        _atomic_write_text(
            rep_dir / "metrics.json", json.dumps(payload, indent=2, sort_keys=True) + "\n"
        )
        _atomic_write_text(rep_dir / "llm_raw.txt", rr.capture.raw_llm)
        _atomic_write_text(
            rep_dir / "events.json", json.dumps(rr.capture.event_log_slice, indent=2) + "\n"
        )

    _atomic_write_text(root / "aggregate.md", render_aggregate_md(distributions, config))
    _atomic_write_text(
        root / "cost.json",
        json.dumps(_cost_summary(reps), indent=2, sort_keys=True) + "\n",
    )
    return root


# --- validation (shared Mode A CI / Mode B review) ---------------------------

def validate_record(path: str | Path) -> list[str]:
    """Return the list of missing/empty config pins. Empty list ⇒ the record is valid.
    Accepts either the record directory or its config.json path."""
    p = Path(path)
    cfg_path = p / "config.json" if p.is_dir() else p
    try:
        config = json.loads(cfg_path.read_text())
    except FileNotFoundError:
        return [f"{cfg_path}: missing"]
    except json.JSONDecodeError as exc:
        return [f"{cfg_path}: not valid JSON ({exc})"]

    missing = [pin for pin in REQUIRED_PINS if not config.get(pin)]
    persona = config.get("persona_hashes")
    if isinstance(persona, dict) and any(not h for h in persona.values()):
        missing.append("persona_hashes:incomplete")
    return missing
