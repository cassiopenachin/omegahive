"""Corpus v1's own graders, driven against a tree that should fail and one that should pass.

A grader that passes everything measures nothing; a grader that fails the outcome the
operator accepted misreports a grader defect as a model result. The heavy graders — the
loop-script fixture, the privilege property, the read-surface contract — need a database,
a container toolchain or two source repositories, and are exercised by
`taskbench endpoint-witness` against the real historical endpoints instead. These are the
ones cheap enough to keep honest on every commit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from taskbench import CORPUS_ROOT

VERIFIERS = CORPUS_ROOT / "v1" / "verifiers"


def run(script: str, tree: Path) -> tuple[int, str]:
    out = subprocess.run(
        [sys.executable, str(VERIFIERS / script), str(tree)],
        capture_output=True, text=True, check=False,
    )
    return out.returncode, out.stdout + out.stderr


def git(tree: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(tree), *args], check=True, capture_output=True)


def baseline_repo(tmp: Path, files: dict[str, str]) -> Path:
    tree = tmp / "tree"
    for rel, text in files.items():
        p = tree / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git(tree, "init", "--quiet", "--initial-branch=main")
    git(tree, "config", "user.name", "t")
    git(tree, "config", "user.email", "t@t")
    git(tree, "add", "-A")
    git(tree, "commit", "--quiet", "-m", "baseline")
    return tree


# --- pw-writeup: the writing standard's three mechanical tests ---------------------------

REPORT = "benchmarks/proofwriter/REPORT.html"

INTERNAL_DOC = """\
<html><body>
<h1>ProofWriter results</h1>
<p>The hive's worker report records that starvation dominates the miss class, and the
deriver's over-derivation on the frozen slice is discussed in known-issues #6. The
budget-invariance recheck is in the harness. PLN scored 0.707.</p>
<table><tr><td>0.707</td></tr></table>
</body></html>
"""

CLEAN_DOC = """\
<html><body>
<h1>ProofWriter results</h1>
<p>We evaluate Probabilistic Logic Networks (PLN) on ProofWriter. Of the 176 questions we
got wrong, 125 were cases where the inference engine drew a conclusion the benchmark does
not count as proven. PLN scored 0.707 on the 600-question subset.</p>
<p>Published comparisons are quoted as of 2026-07-30 and were not re-checked here.</p>
<h2>Sources</h2>
<dl>
<dd><code>projects/pln-benchmarks/reports/2026-07-28-pw-libpln-slice-result.md</code>@9d87407fac9fc20c862285aef8b439043d21e69b</dd>
<dd><code>projects/pln-benchmarks/reports/2026-07-30-pw-d5-comparable-result.md</code>@eb3895a28423fb06e8bf210f9afa9bb4b727367f</dd>
<dd><code>benchmarks/proofwriter/results/summary-adequate-budget.md</code>@1134811aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa</dd>
</dl>
<table><tr><td>0.707</td></tr></table>
</body></html>
"""


def test_the_writing_standard_fails_a_document_written_for_us(tmp_path):
    tree = baseline_repo(tmp_path, {REPORT: INTERNAL_DOC})
    code, out = run("pw_writeup_legibility.py", tree)
    assert code == 1
    assert "internal-vocabulary" in out
    assert "coinage-budget" in out
    assert "abbreviation" in out
    assert "sources-manifest" in out


def test_the_writing_standard_passes_a_document_written_for_a_reader(tmp_path):
    tree = baseline_repo(tmp_path, {REPORT: CLEAN_DOC})
    code, out = run("pw_writeup_legibility.py", tree)
    assert code == 0, out


def test_a_source_citation_is_not_counted_as_internal_vocabulary(tmp_path):
    """The definition of done asks for a manifest of workspace refs, and those paths carry
    our task ids. Reading them as dialect would refuse a document for obeying its order."""
    doc = CLEAN_DOC.replace(
        "<h2>Sources</h2>",
        "<p>Numbers come from "
        "<code>projects/pln-benchmarks/reports/2026-07-28-pw-libpln-slice-result.md</code>.</p>"
        "<h2>Sources</h2>",
    )
    tree = baseline_repo(tmp_path, {REPORT: doc})
    code, out = run("pw_writeup_legibility.py", tree)
    assert code == 0, out


def test_three_coinages_are_within_budget_and_four_are_not(tmp_path):
    three = CLEAN_DOC.replace(
        "<h1>", "<p>starvation, over-derivation, frozen slice</p><h1>"
    )
    tree = baseline_repo(tmp_path, {REPORT: three})
    assert run("pw_writeup_legibility.py", tree)[0] == 0

    four = CLEAN_DOC.replace(
        "<h1>", "<p>starvation, over-derivation, frozen slice, the deriver</p><h1>"
    )
    tree2 = baseline_repo(tmp_path / "b", {REPORT: four})
    code, out = run("pw_writeup_legibility.py", tree2)
    assert code == 1 and "coinage-budget" in out


def test_a_missing_document_is_a_failure_and_not_a_pass(tmp_path):
    tree = baseline_repo(tmp_path, {"README.md": "nothing here\n"})
    code, out = run("pw_writeup_legibility.py", tree)
    assert code == 1 and "missing" in out


# --- fol-pln-mapping: the holes a converter order would fall into --------------------------

NOTE = "docs/fol-pln-mapping.md"
REPRO = "tests/repros/fol/01-quantifiers.metta"
FOLIO_REPORT = "benchmarks/folio/REPORT.html"

FULL_NOTE = """\
# FOL to lib_pln mapping

## Inventory

| construct | count | share |
|---|---|---|
| universal quantification (forall) | 1204 | 61.2% |
| existential quantification (exists) | 233 | 11.8% |
| disjunction | 180 | 9.1% |
| biconditional | 96 | 4.9% |
| negation | 410 | 20.8% |
| nested quantifiers | 44 | 2.2% |
| n-ary predicate arity | 512 | 26.0% |
| function symbols | 0 | 0.0% |
| implication | 900 | 45.7% |
| conjunction | 620 | 31.5% |

Every row is verified by a repro under `tests/repros/fol/`; see repro `01`, repro `02` and
repro `03`.

## Reporting arms

A deductive-subset arm, realized through the base-rate lever rather than by editing
`vendor/`, and a full arm reported with confidence calibration curves. A derived rule file
with provenance and a regeneration script is the fallback.

## Run protocol

Slice versus full: the full 1,430 conclusions cost roughly 3 CPU-hours at the measured rate.
Budget policy: adequate budget plus a budget-invariance recheck. Per-question timeout set
off the tail. Liveness canaries are known-derivable probes, each an existing repro assertion.
"""

GOOD_REPORT = """\
<html><body>
<h2>1. What FOLIO tests</h2><p>A worked example follows.</p>
<h2>2. Known results</h2><p>GPT-4 few-shot 64.2%, chain-of-thought with self-consistency
69.5%. Source: https://arxiv.org/abs/2209.00840</p>
</body></html>
"""


def test_the_note_check_fails_when_the_deliverable_is_absent(tmp_path):
    tree = baseline_repo(tmp_path, {"README.md": "x\n"})
    code, out = run("fol_pln_note_shape.py", tree)
    assert code == 1 and NOTE in out


def test_the_note_check_passes_a_note_that_answers_everything(tmp_path):
    tree = baseline_repo(
        tmp_path,
        {
            NOTE: FULL_NOTE,
            REPRO: "; a repro\n!(assertEqual (foo) (bar))\n",
            "tests/repros/fol/02-biconditional.metta": "!(assertEqual (a) (b))\n",
            "tests/repros/fol/03-disjunction.metta": "!(assertEqual (c) (d))\n",
            FOLIO_REPORT: GOOD_REPORT,
        },
    )
    code, out = run("fol_pln_note_shape.py", tree)
    assert code == 0, out


def test_a_micro_example_that_asserts_nothing_cannot_verify_an_encoding(tmp_path):
    tree = baseline_repo(
        tmp_path,
        {
            NOTE: FULL_NOTE,
            REPRO: "; runs and proves nothing\n!(foo)\n",
            "tests/repros/fol/02-biconditional.metta": "!(assertEqual (a) (b))\n",
            "tests/repros/fol/03-disjunction.metta": "!(assertEqual (c) (d))\n",
            FOLIO_REPORT: GOOD_REPORT,
        },
    )
    code, out = run("fol_pln_note_shape.py", tree)
    assert code == 1 and "assert nothing" in out


def test_a_construct_the_note_never_mentions_is_a_hole(tmp_path):
    note = FULL_NOTE.replace("| existential quantification (exists) | 233 | 11.8% |", "")
    note = note.replace("Skolem", "")
    tree = baseline_repo(
        tmp_path,
        {
            NOTE: note,
            REPRO: "!(assertEqual (a) (b))\n",
            "tests/repros/fol/02-x.metta": "!(assertEqual (a) (b))\n",
            "tests/repros/fol/03-y.metta": "!(assertEqual (a) (b))\n",
            FOLIO_REPORT: GOOD_REPORT,
        },
    )
    code, out = run("fol_pln_note_shape.py", tree)
    assert code == 1 and "existential" in out


def test_editing_the_vendored_runtime_is_caught_as_a_stop_line(tmp_path):
    files = {
        NOTE: FULL_NOTE,
        REPRO: "!(assertEqual (a) (b))\n",
        "tests/repros/fol/02-x.metta": "!(assertEqual (a) (b))\n",
        "tests/repros/fol/03-y.metta": "!(assertEqual (a) (b))\n",
        FOLIO_REPORT: GOOD_REPORT,
        "vendor/PLN/rules.metta": "original\n",
    }
    tree = baseline_repo(tmp_path, files)
    (tree / "vendor" / "PLN" / "rules.metta").write_text("edited\n")
    code, out = run("fol_pln_note_shape.py", tree)
    assert code == 1 and "vendored runtime" in out


# --- every grader is at least loadable ------------------------------------------------------

@pytest.mark.parametrize(
    "script",
    [
        "pw_writeup_legibility.py",
        "fol_pln_note_shape.py",
        "cli_qol2_loop_behaviour.py",
        "hive_mcp_contract.py",
        "sole_write_path_property.py",
    ],
)
def test_every_grader_compiles(script):
    out = subprocess.run(
        [sys.executable, "-m", "py_compile", str(VERIFIERS / script)],
        capture_output=True, text=True, check=False,
    )
    assert out.returncode == 0, out.stderr
