"""Render a task's blinded-review rubric from its manifest.

The rubric is the ONLY corpus file the blinded reviewer ever sees, and everything in it
already exists in the manifest: the checklist the order could not mechanise, the
deterministic checks that were run, the stop-lines, and the legs an offline instrument
cannot execute. Corpus v0/v0.1 wrote both by hand and kept them in step by care. This
renders one from the other instead, so a manifest edit cannot leave a rubric describing a
task that no longer exists — and so the reviewer's world is provably a projection of the
frozen manifest rather than a second, independently drifting document.

What it must never render: anything from `grading/<id>.yaml`. That file states what the
closed outcome established and is grader-only; a rubric carrying it would tell the
reviewer the answer. Nothing here reads it, and `validate-corpus` checks the rendered
result for the historical solution sha regardless.

v0 and v0.1 rubrics are NOT regenerated — their content hashes are pinned by records that
must stay reproducible. This is the mechanism corpus v1 onward uses.
"""

from __future__ import annotations

from .manifest import TaskManifest

HEADER = """\
# Grading rubric — {title}

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.
"""

NOT_DEFECTS = """\
## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.
"""

DETERMINISTIC = """\
## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.
"""

WHAT_TO_WRITE = """\
## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
"""


def render_rubric(manifest: TaskManifest) -> str:
    """The sanitized rubric for one task. Reads the manifest and nothing else."""
    out = [HEADER.format(title=manifest.title), "## What must be true", ""]
    for item in manifest.checklist:
        suffix = " *(this leg is the operator's, not yours — see below)*" if (
            item.applicable == "operator"
        ) else ""
        out.append(f"- **{item.id}** — {item.text}{suffix}")
    for v in manifest.offline_verifiers():
        out.append(f"- **{v.id}** (checked mechanically) — {v.description}")
    out += ["", NOT_DEFECTS, "## Stop-lines — crossing one is always a defect", ""]
    for sl in manifest.stop_lines:
        out.append(f"- **{sl.id}** — {sl.text}")
    out.append("")
    if manifest.non_replayable_legs:
        out += [
            "## Out of scope here",
            "",
            "These legs of the order cannot be executed by the process that produced this "
            "attempt, and",
            "their absence is **not** a defect. Do not mark the attempt down for them, and do "
            "not credit",
            "an attempt that claims to have done them.",
            "",
        ]
        for leg in manifest.non_replayable_legs:
            out.append(f"- {leg.leg} ({leg.reason})")
        out.append("")
    if manifest.withheld_inputs:
        out += [
            "## Documents the attempt could not open",
            "",
            "The order cites these and this replay did not supply them. An attempt that works "
            "around the",
            "gap honestly — naming what it could not check — is doing the right thing; an "
            "attempt that",
            "cites one as if it had read it is not.",
            "",
        ]
        for w in manifest.withheld_inputs:
            out.append(f"- `{w.path}` — {w.reason}")
        out.append("")
    out += [DETERMINISTIC, WHAT_TO_WRITE]
    return "\n".join(out).rstrip() + "\n"
