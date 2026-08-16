"""The comparison across bundles: the table the order asks for, and the claims it forbids.

One record is one bundle's five cells. This reads several — the incumbent plus every candidate —
and renders the matrix. Most of the work here is not arithmetic; it is refusing to say things
five tasks cannot support.

**Twenty points is the resolution.** Five tasks means one task is 20 percentage points. Any
percentage finer than that is false precision dressed as measurement, so percentages are
rendered from the count and never carried to a decimal.

**A red cell and an unreachable bundle are different facts.** A model that produced a wrong
patch and a bundle whose credentials never worked are both "not green", and averaging them
together produces a number that describes neither. Unreachable cells stay in the denominator
and are labelled, never dropped — dropping them is how a bundle that could not run acquires a
respectable pass rate.

**A repaired cell is not a clean first shot.** First-shot and final verdicts are both carried
everywhere, because a model that routinely needs rescue must not read as a clean generator.

**The adequacy heuristic is a screen, not a gate.** It is applied visibly, with every exception
named, and it produces a recommendation the operator may ignore. Nothing here qualifies
anything: the M1c designation is an operator act in a committed disposition.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

#: The v0 screen, precommitted in the order. Each parameter is its own named constant rather
#: than a row in an untyped dict, so that a reader can see exactly what was fixed in advance —
#: and so that nothing downstream can quietly widen one of them.
MIN_FINAL_GREEN = 4
MUST_INCLUDE = "ptc-revalidate"
MAX_BEHIND_INCUMBENT = 1
INCUMBENT_FINAL_GREEN = 5

#: Kept as a mapping for the record, built from the constants above so the two cannot drift.
ADEQUACY: dict[str, Any] = {
    "min_final_green": MIN_FINAL_GREEN,
    "must_include": MUST_INCLUDE,
    "max_behind_incumbent": MAX_BEHIND_INCUMBENT,
    "incumbent_final_green": INCUMBENT_FINAL_GREEN,
    "no_stop_line_or_safety_failure": True,
}

#: One task out of five. Every percentage in this module is a multiple of it.
RESOLUTION_POINTS = 20


@dataclass
class CellSummary:
    """One task under one bundle, with both verdicts and the facts behind them."""

    task_id: str
    cell_id: str
    first_passed: bool | None
    final_passed: bool | None
    remediated: bool
    inconclusive: bool
    carried_forward: bool
    project: str = ""
    work_shape: str = ""
    wall_ms: int | None = None
    resolved_model: str | None = None
    spend: dict[str, Any] = field(default_factory=dict)
    usage: dict[str, Any] = field(default_factory=dict)
    gateway: dict[str, Any] = field(default_factory=dict)
    pulse: dict[str, Any] | None = None
    actionable_red: dict[str, str] | None = None
    stop_line_violations: list[str] = field(default_factory=list)

    @property
    def state(self) -> str:
        """One word, and `inconclusive` is one of them on purpose."""
        if self.inconclusive:
            return "inconclusive"
        if self.final_passed is None:
            return "missing"
        return "green" if self.final_passed else "red"


@dataclass
class BundleSummary:
    """One record, read as a bundle: what ran, what passed, and what it cost."""

    label: str
    record: str
    vendor: str
    model: str
    harness: str
    cells: list[CellSummary]
    gateway_totals: dict[str, Any] = field(default_factory=dict)
    unreachable_reason: str | None = None

    @property
    def reachable(self) -> bool:
        return self.unreachable_reason is None

    @property
    def served_model(self) -> str:
        """What the gateway says served this bundle, falling back to what was requested.

        `model` is the launch label, and for a gateway arm that is an alias with a preset
        suffix — a route, not a model. Two bundles could carry different aliases pointing at
        the same weights, or the same alias at different ones, and a comparison table printing
        only the request cannot show either. Where receipts exist they are the answer; where
        they do not, the request is marked as a request rather than promoted into a fact.
        """
        served = self.gateway_totals.get("resolved_models") or []
        if served:
            return ", ".join(str(m) for m in served)
        return f"{self.model} *(requested)*"

    def count(self, which: str) -> int:
        if which == "first":
            return sum(1 for c in self.cells if c.first_passed and not c.inconclusive)
        return sum(1 for c in self.cells if c.final_passed and not c.inconclusive)

    @property
    def denominator(self) -> int:
        """Every cell the bundle was asked to do, including the ones that went wrong.

        Not "cells that produced a clean verdict": a denominator that shrinks when a cell fails
        is how an unreachable bundle ends up with a respectable pass rate.
        """
        return len(self.cells)

    @property
    def repairs(self) -> int:
        return sum(1 for c in self.cells if c.remediated)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_bundle(
    record_root: str | Path, *, label: str, unreachable_reason: str | None = None
) -> BundleSummary:
    """Read one record into a bundle summary. Absent facts stay absent."""
    root = Path(record_root)
    config = _read_json(root / "config.json")
    labels = config.get("agent_labels") or {}
    rows = []
    try:
        rows = json.loads((root / "cells.json").read_text())
    except (OSError, json.JSONDecodeError):
        rows = []

    cells: list[CellSummary] = []
    for row in rows:
        cell_dir = root / "cells" / str(row.get("cell_id", ""))
        verdict = _read_json(cell_dir / "verdict.json")
        cycle = _read_json(cell_dir / "cycle.json")
        run = _read_json(cell_dir / "run.json")
        gateway = {}
        for leg_file in sorted(cell_dir.glob("gateway-receipts-*.json")):
            gateway[leg_file.stem.replace("gateway-receipts-", "")] = _read_json(leg_file)

        first = row.get("first_passed")
        if first is None:
            first = (cycle.get("first_pass") or {}).get("passed")
        cells.append(
            CellSummary(
                task_id=str(row.get("task_id", "")),
                cell_id=str(row.get("cell_id", "")),
                first_passed=first,
                final_passed=row.get("passed"),
                remediated=bool(row.get("remediated") or cycle.get("remediated")),
                inconclusive=bool(verdict.get("inconclusive")),
                carried_forward=bool(row.get("carried_forward")),
                wall_ms=row.get("wall_ms"),
                resolved_model=row.get("resolved_model"),
                spend=_read_json(cell_dir / "spend.json"),
                usage=row.get("usage") or {},
                gateway=gateway,
                pulse=run.get("pulse"),
                stop_line_violations=list(
                    (verdict.get("deterministic") or {}).get("stop_line_violations") or []
                ),
            )
        )
    return BundleSummary(
        label=label,
        record=str(root),
        vendor=str(labels.get("vendor", "unknown")),
        model=str(labels.get("model", "unknown")),
        harness=str(labels.get("harness", "unknown")),
        cells=cells,
        gateway_totals=_read_json(root / "gateway-totals.json"),
        unreachable_reason=unreachable_reason,
    )


def percent(count: int, total: int) -> str:
    """A percentage at the resolution five tasks actually give, and no finer."""
    if not total:
        return "n/a"
    return f"{round(count * 100 / total)}%"


def adequacy(bundle: BundleSummary, *, incumbent: BundleSummary | None = None) -> dict[str, Any]:
    """Apply the v0 screen, showing every clause and every exception.

    Returns the verdict *and* the working, because a screen whose reasoning is invisible becomes
    a threshold — which is exactly what the order says this must not become.
    """
    if not bundle.reachable:
        return {
            "verdict": "unreachable",
            "clauses": [],
            "note": (
                f"{bundle.label} never reached its model: {bundle.unreachable_reason}. That is a "
                "setup boundary, not a model result, and it is not scored as a task failure."
            ),
        }

    final = bundle.count("final")
    incumbent_final = (
        incumbent.count("final") if incumbent else INCUMBENT_FINAL_GREEN
    )
    sentinel = next(
        (c for c in bundle.cells if c.task_id == MUST_INCLUDE), None
    )
    stop_lines = [c.task_id for c in bundle.cells if c.stop_line_violations]

    clauses = [
        {
            "clause": f"at least {MIN_FINAL_GREEN}/5 final pipeline verdicts green",
            "met": final >= MIN_FINAL_GREEN,
            "observed": f"{final}/{bundle.denominator}",
        },
        {
            "clause": f"{MUST_INCLUDE} among them",
            "met": bool(sentinel and sentinel.final_passed and not sentinel.inconclusive),
            "observed": sentinel.state if sentinel else "the cell is absent",
        },
        {
            "clause": "no stop-line or would-have-shipped safety failure",
            "met": not stop_lines,
            "observed": "none" if not stop_lines else f"stop-line violations in {stop_lines}",
        },
        {
            "clause": (
                f"no more than {MAX_BEHIND_INCUMBENT} task behind the incumbent's "
                f"{incumbent_final}/5"
            ),
            "met": (incumbent_final - final) <= MAX_BEHIND_INCUMBENT,
            "observed": f"{incumbent_final - final} behind",
        },
    ]
    failed = [c for c in clauses if not c["met"]]
    return {
        "verdict": "clears the v0 screen" if not failed else "does not clear the v0 screen",
        "clauses": clauses,
        "exceptions": [c["clause"] for c in failed],
        "note": (
            "This is a lossy screen over a five-task matrix, applied to the FINAL pipeline "
            "verdict. It is not routing doctrine, it does not amend HIP-1, and it qualifies "
            "nothing on its own: the M1c designation is the operator's, in a committed "
            "disposition."
        ),
    }


def disagreements(bundles: list[BundleSummary]) -> list[dict[str, Any]]:
    """Tasks where the bundles did not agree — the only place a five-task matrix has signal.

    A task everything passes and a task everything fails both say something about the *task*.
    A task that splits the field says something about the bundles.
    """
    by_task: dict[str, dict[str, str]] = {}
    for bundle in bundles:
        for cell in bundle.cells:
            by_task.setdefault(cell.task_id, {})[bundle.label] = cell.state
    out = []
    for task_id, states in sorted(by_task.items()):
        distinct = set(states.values())
        if len(distinct) > 1:
            out.append({"task_id": task_id, "states": states, "distinct": sorted(distinct)})
    return out


def rank(bundles: list[BundleSummary]) -> list[dict[str, Any]]:
    """Order by final green count, then first-shot. **Ties are left tied.**

    Breaking a tie on a five-task corpus with a spend tiebreaker would be inventing a
    distinction the measurement cannot support.
    """
    reachable = [b for b in bundles if b.reachable]
    keyed = sorted(
        reachable, key=lambda b: (-b.count("final"), -b.count("first"), b.label)
    )
    out: list[dict[str, Any]] = []
    place = 0
    previous: tuple[int, int] | None = None
    for index, bundle in enumerate(keyed, start=1):
        key = (bundle.count("final"), bundle.count("first"))
        if key != previous:
            place = index
            previous = key
        out.append(
            {
                "place": place,
                "label": bundle.label,
                "final": f"{bundle.count('final')}/{bundle.denominator}",
                "first_shot": f"{bundle.count('first')}/{bundle.denominator}",
                "repairs": bundle.repairs,
                "tied": False,
            }
        )
    places = [row["place"] for row in out]
    for row in out:
        row["tied"] = places.count(row["place"]) > 1
    for bundle in bundles:
        if not bundle.reachable:
            out.append(
                {
                    "place": None,
                    "label": bundle.label,
                    "final": "unreachable",
                    "first_shot": "unreachable",
                    "repairs": 0,
                    "tied": False,
                    "why": bundle.unreachable_reason,
                }
            )
    return out


def pulse_coverage(bundle: BundleSummary) -> dict[str, Any]:
    """How much the five-minute snapshot actually saw, reported separately from accuracy.

    Early escalation value matters even when two bundles end at the same pass count, and it is
    only meaningful if the coverage is stated: a bundle whose cells all finished inside five
    minutes has no pulse data, which is not the same as a pulse that saw nothing.
    """
    states: dict[str, int] = {}
    no_pulse = 0
    for cell in bundle.cells:
        if not cell.pulse:
            no_pulse += 1
            continue
        states[str(cell.pulse.get("state"))] = states.get(str(cell.pulse.get("state")), 0) + 1
    return {
        "cells": bundle.denominator,
        "with_pulse": bundle.denominator - no_pulse,
        "without_pulse": no_pulse,
        "states": states,
        "note": (
            "A cell with no pulse finished inside the snapshot window; that is an absence of "
            "data, not a snapshot that observed nothing."
        ),
    }


def matched_pair(arm_a: BundleSummary, arm_b: BundleSummary) -> dict[str, Any]:
    """The DeepSeek table: same model, same route, one variable — per-task and aggregate deltas.

    Deltas only where BOTH arms have the figure. A delta computed against a missing number is a
    statement about the gap in the record, not about the harnesses.
    """
    rows: list[dict[str, Any]] = []
    by_task_b = {c.task_id: c for c in arm_b.cells}
    fields = (
        "native_tokens_prompt", "native_tokens_cached", "native_tokens_completion",
        "gateway_cost_usd", "calls_observed",
    )
    for cell_a in arm_a.cells:
        cell_b = by_task_b.get(cell_a.task_id)
        row: dict[str, Any] = {
            "task_id": cell_a.task_id,
            f"{arm_a.label}_verdict": cell_a.state,
            f"{arm_b.label}_verdict": cell_b.state if cell_b else "missing",
        }
        totals_a = _pair_totals(cell_a)
        totals_b = _pair_totals(cell_b) if cell_b else {}
        for key in fields:
            a_val, b_val = totals_a.get(key), totals_b.get(key)
            row[f"{arm_a.label}_{key}"] = a_val
            row[f"{arm_b.label}_{key}"] = b_val
            row[f"delta_{key}"] = (
                b_val - a_val
                if isinstance(a_val, (int, float)) and isinstance(b_val, (int, float))
                else None
            )
        if cell_a.wall_ms and cell_b and cell_b.wall_ms:
            row["delta_wall_ms"] = cell_b.wall_ms - cell_a.wall_ms
        rows.append(row)

    return {
        "arm_a": arm_a.label,
        "arm_b": arm_b.label,
        "rows": rows,
        "identity_held": _identity_held(arm_a, arm_b),
        "claim_boundary": (
            "Model, provider, gateway, preset, upstream, task and kickoff are held fixed; only "
            "the harness differs. This says whether extra consumption coincided with better "
            "outcomes ON THIS FIVE-TASK CORPUS. It does not attribute any delta to prompt size, "
            "caching, tool loop, compaction or any other internal mechanism — these records "
            "cannot isolate those — and it is not a universal harness verdict."
        ),
    }


def _pair_totals(cell: CellSummary) -> dict[str, Any]:
    """Sum a cell's gateway legs. Absent stays absent; a missing leg is not a zero."""
    out: dict[str, Any] = {}
    for leg in cell.gateway.values():
        for key, value in (leg.get("totals") or {}).items():
            if isinstance(value, (int, float)):
                out[key] = out.get(key, 0) + value
    return out


def _identity_held(arm_a: BundleSummary, arm_b: BundleSummary) -> dict[str, Any]:
    """Did the pair actually run the same model on the same silicon? The claim rests on this."""
    def upstreams(bundle: BundleSummary) -> set[str]:
        return set(bundle.gateway_totals.get("resolved_upstreams") or [])

    def models(bundle: BundleSummary) -> set[str]:
        return set(bundle.gateway_totals.get("resolved_models") or [])

    a_up, b_up = upstreams(arm_a), upstreams(arm_b)
    a_mo, b_mo = models(arm_a), models(arm_b)
    problems = []
    if not a_up or not b_up:
        problems.append(
            "at least one arm has no resolved upstream in its gateway totals, so a fallback "
            "cannot be ruled out and the matched claim is unproven"
        )
    elif a_up != b_up:
        problems.append(f"the arms resolved different upstreams: {sorted(a_up)} vs {sorted(b_up)}")
    if a_mo and b_mo and a_mo != b_mo:
        problems.append(f"the arms resolved different models: {sorted(a_mo)} vs {sorted(b_mo)}")
    if arm_a.model != arm_b.model:
        problems.append(
            f"the arms requested different model strings: {arm_a.model!r} vs {arm_b.model!r}"
        )
    return {
        "held": not problems,
        "problems": problems,
        "arm_a_upstreams": sorted(a_up),
        "arm_b_upstreams": sorted(b_up),
        "arm_a_models": sorted(a_mo),
        "arm_b_models": sorted(b_mo),
    }


def render(
    bundles: list[BundleSummary], *, incumbent_label: str = "incumbent"
) -> str:
    """The matrix as markdown: every table the order asks for, with its caveats attached.

    Caveats are rendered beside the numbers rather than collected in a footer, because a reader
    who quotes one row will quote the caveat with it.
    """
    incumbent = next((b for b in bundles if b.label == incumbent_label), None)
    tasks = sorted({c.task_id for b in bundles for c in b.cells})
    out: list[str] = []

    out += [
        "# Candidate matrix — HIP-1 M1b qualification",
        "",
        f"{len(bundles)} bundle(s) over {len(tasks)} held-in task(s). **One task is "
        f"{RESOLUTION_POINTS} percentage points**: no figure below is quoted finer than that, "
        "and no population or general-coding claim is made from it.",
        "",
        "## Per-task verdicts — first shot / final pipeline",
        "",
        "| bundle | vendor | model | harness | " + " | ".join(tasks) + " | first | final |",
        "|" + "---|" * (len(tasks) + 6),
    ]
    for bundle in bundles:
        by_task = {c.task_id: c for c in bundle.cells}
        cells = []
        for task in tasks:
            cell = by_task.get(task)
            if cell is None:
                cells.append("—")
            elif cell.inconclusive:
                cells.append("inconclusive")
            else:
                first = "green" if cell.first_passed else "RED"
                final = "green" if cell.final_passed else "RED"
                cells.append(f"{first} / {final}" + (" *(repaired)*" if cell.remediated else ""))
        if not bundle.reachable:
            cells = ["unreachable"] * len(tasks)
        out.append(
            f"| {bundle.label} | {bundle.vendor} | {bundle.served_model} | {bundle.harness} | "
            + " | ".join(cells)
            + f" | {bundle.count('first')}/{bundle.denominator}"
            + f" | {bundle.count('final')}/{bundle.denominator} |"
        )
    out += [
        "",
        "A cell reads `first shot / final pipeline`. **A repaired cell is never narrated as a "
        "clean first shot**, and an `inconclusive` cell — one the environment killed — stays in "
        "the denominator rather than being dropped from it.",
        "",
        "## Repair use, and what it does not license",
        "",
        "| bundle | repairs used | first shot | final |",
        "|---|---|---|---|",
    ]
    for bundle in bundles:
        out.append(
            f"| {bundle.label} | {bundle.repairs}/{bundle.denominator} | "
            f"{percent(bundle.count('first'), bundle.denominator)} | "
            f"{percent(bundle.count('final'), bundle.denominator)} |"
        )
    out += [
        "",
        "There is no repair-count gate. A route may clear the quality screen and still lose the "
        "economic case because its review leg is too frequent or too expensive — those are "
        "separate findings and neither overrides the other.",
        "",
        "## The v0 adequacy screen, applied visibly",
        "",
    ]
    for bundle in bundles:
        verdict = adequacy(bundle, incumbent=incumbent)
        out += [f"**{bundle.label} — {verdict['verdict']}**", ""]
        for clause in verdict["clauses"]:
            mark = "met" if clause["met"] else "**NOT MET**"
            out.append(f"- {clause['clause']} — {mark} ({clause['observed']})")
        out += ["", f"> {verdict['note']}", ""]

    out += ["## Rank, with ties left tied", "", "| place | bundle | final | first shot | repairs |",
            "|---|---|---|---|---|"]
    for row in rank(bundles):
        place = "—" if row["place"] is None else str(row["place"])
        tie = " *(tied)*" if row["tied"] else ""
        out.append(
            f"| {place}{tie} | {row['label']} | {row['final']} | {row['first_shot']} | "
            f"{row['repairs']} |"
        )
    out += [
        "",
        "A one-task lead is not a stable lead and a tied result is not a demonstration of "
        "equivalence. This corpus ranks this sample.",
        "",
        "## Where the bundles disagreed",
        "",
    ]
    splits = disagreements(bundles)
    if not splits:
        out.append("No task split the field. On five tasks that is a weak result, not a strong "
                   "one: it says the corpus did not discriminate here.")
    else:
        out += ["| task | " + " | ".join(b.label for b in bundles) + " |",
                "|" + "---|" * (len(bundles) + 1)]
        for split in splits:
            out.append(
                f"| {split['task_id']} | "
                + " | ".join(split["states"].get(b.label, "—") for b in bundles)
                + " |"
            )
    out += ["", "## Five-minute diagnostic coverage", "",
            "| bundle | cells | with a pulse | states |", "|---|---|---|---|"]
    for bundle in bundles:
        cov = pulse_coverage(bundle)
        out.append(
            f"| {bundle.label} | {cov['cells']} | {cov['with_pulse']} | {cov['states'] or '—'} |"
        )
    out += [
        "",
        "Reported separately from final accuracy: early escalation value matters even when two "
        "bundles reach the same pass count. A cell with no pulse finished inside the window — "
        "an absence of data, not a snapshot that saw nothing.",
        "",
        "## What this table is not",
        "",
        "Five hive-heavy tasks with one PLN sentinel. It cannot qualify a bundle for applied "
        "tenant research, for less-bounded work, or for the future middle tier, and the single "
        "cross-project task is a sentinel that the instrument reaches a second repository — not "
        "evidence about PLN research. **No bundle is benchmark-qualified by this document:** the "
        "M1c designation is the operator's, in a committed disposition.",
        "",
    ]
    return "\n".join(out) + "\n"
