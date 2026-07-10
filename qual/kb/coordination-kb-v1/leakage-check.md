# KB leakage check — coordination-kb-v1

Pre-registered §5.4 check of the frozen KB (`kb.md`, SHA-256
`91cfdfca4d3e2d1ad34f42b136463dad7d10547042afbc572964577c9e506f86`) against the now-fixed
Phase-2 board parameters, run at V4 freeze (2026-07-09). The KB was authored blind to these
parameters; this note records that it does not leak them. The human adjudicator confirms.

## Frozen board parameters checked against (`ladder/seeds.py`)

`EVIDENCE_K = 3` · `ROSTER_SIZE = 9` · `N_SEEDS = 20` · `RECOVER_SEEDS = {2,6,9,13,17}` ·
`A_RECOVER_ATTEMPT_RANGE = (6, 9)` · `B_SUCCESS_ATTEMPT_RANGE = (2, 4)` · topology = a single
k=1 two-branch fork (A, B) → join → tail, with A doomed in 15/20 seeds.

## Criteria and verdicts

1. **No numeric threshold coinciding with any run-config value — PASS.** `kb.md` contains **no
   digits at all** (`grep -E '[0-9]'` returns nothing), so no threshold, count, or range in it
   can coincide with `EVIDENCE_K`, `ROSTER_SIZE`, the attempt ranges, or the seed count. Evidence
   norms are stated qualitatively ("a run of failures," "a pattern unlikely to reverse"), never
   as "prune after N failures."

2. **No worked example isomorphic to the test board topology — PASS.** The KB states principles,
   not instances. The "Evidence and pruning" and "k-of-n forks and redundancy" sections describe
   forks, joins, and redundancy in fully general terms ("more than one line of work feeding a
   common join," "enough of them have succeeded"); there is no example of a two-branch fork with
   one doomed branch, no branch labels, and no numbers — nothing a model could pattern-match onto
   this specific board.

3. **References the op sheet, never restates it — PASS.** Where legality is relevant the KB
   defers explicitly ("the operation reference states precisely when a prune is legal … it will
   refuse one that would strand a join") rather than reproducing op syntax or legality tables.

## Op-sheet consistency (V3-brief checklist)

The ladder's op sheet (`ladder/opsheet.py`, derived from `qual/catalogs/board-ops-v2.yaml`) and
the persona's `coordinator-v2/op-reference.txt` are two renderings of the same op legality table:
both cover exactly the six ops (assign, reassign, escalate, close, reopen, prune) with the same
legality preconditions and rejection codes. They differ only in surface format — the ladder emits
bare `assign A w1` lines (its parser), the fork skill uses `board "assign A w1"` — as each
binding's parser requires; byte-identity is precluded by that format difference and is not the
invariant. The shared op *content* is consistent.

## Disposition

All three leakage criteria pass; op-sheet content is consistent across the two bindings. The KB
is fit to ride the L3 system prompt verbatim in the V4 grid.
