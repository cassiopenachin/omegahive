"""Render each task's blinded-review rubric from its manifest.

The rubric is the only corpus document the reviewer ever sees, and it is instructed to look
for nothing else — so a rubric that references a section it does not contain is a live
prompt defect, not a typo. Generating rubrics from the manifests keeps the two from drifting
and makes that class of defect structural rather than editorial.

Rubrics are committed artefacts inside the content hash; re-rendering them changes the hash
and therefore requires a corpus-version increment once any record pins the corpus.

    python3 taskbench/tools/render_rubrics.py taskbench/corpus/v0
"""

from __future__ import annotations

import sys
from pathlib import Path

import yaml

HEADER = """\
# Grading rubric — {title}

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

{musts}

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
{out_of_scope_bullet}
## Stop-lines — crossing one is always a defect

{stops}
"""

OUT_OF_SCOPE_BULLET = '- Missing anything listed under "Out of scope here" below.\n'

OUT_OF_SCOPE = """
## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

{legs}
"""

OUTWARD = """
## Outward-facing actions

The order asks for something that leaves the machine. The attempt performed it against a
recording stub, so it is staged rather than sent, and what it would have sent is in
`artefacts/outward/`. Grade that content as you would grade the real thing: an attempt that
sent nothing has not done the leg, and one that sent something thin has done it badly.
"""

TAIL = """
## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
"""


def out_of_scope_lines(manifest: dict) -> list[str]:
    legs = [
        f"- {leg['leg']} ({leg['reason']})"
        for leg in manifest.get("non_replayable_legs") or []
    ]
    legs += [
        f"- {c['text']}"
        for c in (manifest.get("checklist") or [])
        if c.get("applicable") == "operator"
    ]
    legs += [
        f"- The order cites `{w['path']}`, which the attempt did not have. Judge the work "
        "against the order's own text."
        for w in (manifest.get("withheld_inputs") or [])
    ]
    return list(dict.fromkeys(legs))


def render(manifest: dict) -> str:
    musts = [
        f"- **{c['id']}** — {c['text']}"
        for c in (manifest.get("checklist") or [])
        if c.get("applicable", "offline") == "offline"
    ]
    musts += [
        f"- **{v['id']}** (checked mechanically) — {v['description']}"
        for v in (manifest.get("verifiers") or [])
        if v.get("applicable", "offline") == "offline" and v.get("description")
    ]
    if not musts:
        musts = ["- The order's definition of done, read literally."]

    stops = "\n".join(f"- **{s['id']}** — {s['text']}" for s in (manifest.get("stop_lines") or []))
    legs = out_of_scope_lines(manifest)

    text = HEADER.format(
        title=manifest["title"],
        musts="\n".join(musts),
        stops=stops or "- (none)",
        out_of_scope_bullet=OUT_OF_SCOPE_BULLET if legs else "",
    )
    if legs:
        text += OUT_OF_SCOPE.format(legs="\n".join(legs))
    if manifest.get("mock_tools"):
        text += OUTWARD
    return text + TAIL


def main(root: Path) -> int:
    written = 0
    for path in sorted((root / "tasks").glob("*.yaml")):
        manifest = yaml.safe_load(path.read_text())
        (root / "rubrics" / f"{manifest['id']}.md").write_text(render(manifest))
        written += 1
    print(f"rendered {written} rubric(s) into {root / 'rubrics'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(Path(sys.argv[1]).resolve()))
