# INVALIDATED — the bundle could not write the files it was graded on

This record is preserved as evidence and must not be read as a result for GPT-5.6 Luna, nor
resumed from. `taskbench.record.resumable_cells` returns nothing for a record carrying this file.

## The defect

The runner launches every candidate with `cwd=<cell>/code`. Codex's `workspace-write` sandbox
permits writes under the cwd only, so the documents this corpus grades — which live in the
sibling `<cell>/workspace` tree — were unreachable to this bundle. Every Claude Code arm in the
study ran with no filesystem confinement and wrote them freely.

The signature is unambiguous. This is the only bundle in the study with
`workspace_changed_files: []` in all five cells; every other bundle wrote one or two per cell.
The blinded reviewer then refused three of the five cells for precisely the missing documents:

- *"No result report was filed — the order's named deliverable does not exist anywhere in the
  attempt."*
- *"Only one of the two required runbook caveats was updated — the workspace
  `projects/omegahive/RUNBOOK.md` still carries the inert-bump caveat."*

Those read as model results. They are artefacts of the launcher.

## What the record still supports

Its **deterministic** leg is untouched by the defect and remains evidence: this bundle passed
every machine-checkable gate in all five cells, on work confined to `code/`. Its per-leg review
cost is real. Its first-shot/final verdicts are not.

## Disposition

Found by independent audit on 2026-08-16, after all four candidate batches had run. Fixed in
`taskbench/launch/cell-codex.sh` by granting `--add-dir <cell>/workspace`, which widens the
sandbox to the two trees the task spans and no further; what the candidate is *asked* to change
is still bounded by the corpus's own `writable_workspace_paths`. Superseded by a fresh record
run under the corrected grant.
