# Replay records

One directory per batch, dated and immutable. A record is never edited: a rerun opens a new
one that names what it supersedes, so a failed run and its diagnosis both survive.

- `2026-08-13-incumbent-fidelity` — corpus **v0**, incumbent Opus. **3/5.** Retained as the
  written diagnosis of two defects: a grader defect (workspace deliverables were exported
  read-only and never captured, so a candidate that wrote them was graded as not having) and
  a method defect (a single shot was scored against outcomes produced by a worker *plus*
  review and repair). v0 itself is retained unmodified so this record's corpus pin stays
  reproducible.
- `2026-08-13-incumbent-fidelity-v0-1` — corpus **v0.1**, both repairs in. Two cells green;
  the account's usage ceiling killed one candidate and every later reviewer. Those cells are
  **inconclusive**, not red — a rate-limited session is not a model result.
- `2026-08-13-incumbent-fidelity-v0-1-2` — the resume. Carries the two conclusive cells
  forward verbatim (each stamped `CARRIED-FORWARD.txt`) and re-runs only the three the
  environment killed. **5/5 green; first-shot generation 4/5.**

Read `review/probe.json` first on any cell you intend to trust: it says whether the
cold-reader boundary held for that cell. Read `cycle.json` to see the first-shot verdict,
which a repair never rewrites.
