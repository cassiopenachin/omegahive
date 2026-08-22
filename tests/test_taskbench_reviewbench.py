"""The reviewer instrument: gold that cannot be asserted, packets that cannot leak, and a
scorer that separates answering correctly from answering for the right reason.

The expensive half — every witness driven at both ends against the real repositories — is
`taskbench validate-review-corpus`. What is here runs on every commit.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from taskbench import CORPUS_ROOT
from taskbench.review_cell import HomeError, ReviewerCellSpec, build_fresh_home, run_probe
from taskbench.review_packet import build_packet
from taskbench.review_score import (
    MIN_DISPOSITIONS_CORRECT,
    PacketScore,
    reviewer_fidelity,
    score_packet,
)
from taskbench.reviewbench import MustFind, PacketGold, load_review_corpus

REVIEW = CORPUS_ROOT / "review-v1"
WORKSPACE = Path(__file__).resolve().parents[2] / "hive"


@pytest.fixture(scope="module")
def corpus():
    return load_review_corpus(REVIEW)


# --- the corpus -----------------------------------------------------------------------------

def test_five_packets_one_of_which_shipped_unchanged(corpus):
    assert len(corpus.catalog.packets) == 5
    clean = [
        p for p in corpus.catalog.packets
        if corpus.gold(p).expected_disposition == "no_required_change"
    ]
    assert len(clean) == 1, (
        "without a packet that needed no change there is no measurement of false positives, "
        "and a reviewer that flags everything scores well on the rest"
    )


def test_every_required_change_packet_says_what_must_be_found(corpus):
    for pid in corpus.catalog.packets:
        gold = corpus.gold(pid)
        if gold.expected_disposition == "required_change":
            assert gold.must_find, pid
        else:
            assert not gold.must_find, pid


def test_every_blocking_defect_can_be_demonstrated(corpus):
    """A defect that decides fidelity must have a check, not an argument."""
    for pid in corpus.catalog.packets:
        for m in corpus.gold(pid).blocking:
            assert m.witness.argv, f"{pid}/{m.id} is {m.severity} and only documentary"


def test_gold_refuses_a_must_find_whose_only_basis_is_someone_saying_so():
    with pytest.raises(ValueError, match="not gold"):
        MustFind.model_validate(
            {
                "id": "x", "severity": "high", "summary": "s",
                "files": ["a.py"], "patterns": ["s"],
                "witness": {"argv": [], "documentary": "d"},
                "basis": ["contemporaneous-review"],
            }
        )


def test_a_witness_must_distinguish_its_two_ends():
    """A witness expecting the same exit at both states proves nothing about either.
    Caught by the cross-vendor review of this branch."""
    from taskbench.reviewbench import Witness

    with pytest.raises(ValueError, match="distinguishes nothing"):
        Witness.model_validate({"argv": ["true"], "bad_end_exit": 0, "accepted_end_exit": 0})
    with pytest.raises(ValueError, match="synthetic -1"):
        Witness.model_validate({"argv": ["true"], "bad_end_exit": -1, "accepted_end_exit": 0})
    with pytest.raises(ValueError, match="relative subpath"):
        Witness.model_validate({"argv": ["true"], "cwd": "/etc"})


def test_a_pattern_that_does_not_compile_fails_the_corpus_not_a_grading_run():
    with pytest.raises(ValueError, match="does not compile"):
        MustFind.model_validate(
            {
                "id": "x", "severity": "high", "summary": "s",
                "files": ["a.py"], "patterns": ["(unclosed"],
                "witness": {"argv": [], "documentary": "d"},
                "basis": ["diff"],
            }
        )


def test_a_finding_matches_on_the_file_field_the_schema_asks_for():
    """The verdict schema has a `file` field. A reviewer that fills it in correctly and does
    not also repeat the path in its sentence was scored as a miss — the instrument
    penalising a reviewer for using the form it handed out."""
    m = MustFind.model_validate(
        {
            "id": "x", "severity": "high", "summary": "s",
            "files": ["src/thing.py"], "patterns": ["idempot"],
            "witness": {"argv": [], "documentary": "d"}, "basis": ["diff"],
        }
    )
    assert m.matches(
        {"file": "src/thing.py", "summary": "the idempotency check runs too late",
         "why_blocking": "a replay is skipped", "evidence": "line 40"}
    )
    assert not m.matches(
        {"file": "src/other.py", "summary": "the idempotency check runs too late",
         "why_blocking": "", "evidence": ""}
    )


def test_gold_refuses_an_incoherent_disposition():
    base = {
        "packet_id": "p", "accepted_sha": "a" * 40, "adjudication": "x", "source_refs": [],
    }
    with pytest.raises(ValueError, match="must say what has to be found"):
        PacketGold.model_validate({**base, "expected_disposition": "required_change"})
    with pytest.raises(ValueError, match="cannot have must-find"):
        PacketGold.model_validate(
            {
                **base, "expected_disposition": "no_required_change",
                "must_find": [
                    {
                        "id": "x", "severity": "high", "summary": "s",
                        "files": ["a"], "patterns": ["s"],
                        "witness": {"argv": [], "documentary": "d"},
                        "basis": ["diff"],
                    }
                ],
            }
        )


def test_no_packet_manifest_names_its_own_answer(corpus):
    """The packet is built from this file; the gold is a separate file the packet never sees."""
    for pid in corpus.catalog.packets:
        text = json.dumps(corpus.packets[pid].model_dump(), default=str)
        gold = corpus.gold(pid)
        assert gold.expected_disposition not in text, pid
        for m in gold.must_find:
            assert m.summary[:40] not in text, f"{pid}: quotes {m.id}"


def test_the_frozen_hashes_match_the_tree(corpus):
    frozen = json.loads((REVIEW / "HASHES").read_text())
    assert frozen["corpus_content_hash"] == corpus.content_hash
    assert frozen["files"] == corpus.file_hashes


# --- building a packet ------------------------------------------------------------------------

@pytest.mark.skipif(not (WORKSPACE / ".git").exists(), reason="needs the workspace clone")
def test_a_built_packet_is_blind_and_carries_no_answer(corpus, tmp_path):
    built = build_packet(
        corpus, "run-registration-pre-review", dest=tmp_path / "p",
        workspace_repo_path=str(WORKSPACE), run_checks=False,
    )
    assert not built.violations, built.violations
    assert built.blind_id.startswith("packet-") and built.packet_id not in built.blind_id

    blob = "\n".join(
        p.read_text(errors="replace")
        for p in built.root.rglob("*")
        if p.is_file() and p.suffix in (".md", ".json", ".patch")
    )
    gold = corpus.gold("run-registration-pre-review")
    assert "run-registration-pre-review" not in (built.root / "README.md").read_text()
    for m in gold.must_find:
        assert m.summary[:50] not in blob, "the packet states the defect it is asking about"
    # The brief names BOTH dispositions — that is the vocabulary the reviewer answers in.
    # What must be absent is which one this packet expects, and the reasoning behind it.
    assert gold.adjudication[:60] not in blob
    assert "must_find" not in blob and "acceptable_optional" not in blob
    assert (built.root / "order.md").is_file()
    assert (built.root / "change.patch").is_file()


@pytest.mark.skipif(not (WORKSPACE / ".git").exists(), reason="needs the workspace clone")
def test_a_packet_shows_exactly_base_to_head_and_nothing_after(corpus, tmp_path):
    """The reviewer sees the work as it stood, not the work plus what main did meanwhile,
    and never a byte of the repair — the diff is `base..head` by construction."""
    pid = "run-registration-pre-review"
    built = build_packet(
        corpus, pid, dest=tmp_path / "p",
        workspace_repo_path=str(WORKSPACE), run_checks=False,
    )
    packet = corpus.packets[pid]
    repo = Path(packet.code.local_path).expanduser()
    expected = subprocess.run(
        ["git", "-C", str(repo), "diff", packet.code.base_sha, packet.code.head_sha],
        capture_output=True, text=True, check=False,
    ).stdout
    assert (built.root / "change.patch").read_text() == expected

    repair_message = subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s",
         corpus.gold(pid).accepted_sha],
        capture_output=True, text=True, check=False,
    ).stdout.strip()
    blob = "\n".join(
        p.read_text(errors="replace") for p in built.root.rglob("*") if p.is_file()
    )
    assert repair_message and repair_message not in blob


@pytest.mark.skipif(not (WORKSPACE / ".git").exists(), reason="needs the workspace clone")
def test_the_scan_reads_contents_and_not_only_filenames(corpus, tmp_path):
    """A filename check alone would miss the answer arriving inside an innocuous file.
    Caught by the cross-vendor review of this branch."""
    from taskbench.review_packet import scan_packet

    pid = "run-registration-pre-review"
    built = build_packet(
        corpus, pid, dest=tmp_path / "p",
        workspace_repo_path=str(WORKSPACE), run_checks=False,
    )
    gold = corpus.gold(pid)
    assert not scan_packet(built, corpus.packets[pid], gold=gold)

    (built.root / "verification" / "innocuous.log").write_text(
        "check output\n" + gold.must_find[0].summary + "\n"
    )
    violations = scan_packet(built, corpus.packets[pid], gold=gold)
    assert any("answer key" in v for v in violations), violations


def test_a_packet_refuses_a_directory_that_is_not_empty(corpus, tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "stray").write_text("x")
    with pytest.raises(Exception, match="not empty"):
        build_packet(corpus, "docs-triage-accepted", dest=tmp_path / "p", run_checks=False)


# --- scoring ---------------------------------------------------------------------------------

def _gold(**over) -> PacketGold:
    data = {
        "packet_id": "p",
        "expected_disposition": "required_change",
        "accepted_sha": "a" * 40,
        "adjudication": "x",
        "must_find": [
            {
                "id": "the-defect", "severity": "critical", "summary": "s",
                "files": ["src/thing.py"], "patterns": ["idempot", "early.return"],
                "witness": {"argv": ["true"]},
                "basis": ["order", "diff"],
            }
        ],
        "acceptable_optional": [
            {"id": "style", "summary": "s", "files": ["docs/"], "patterns": ["wording"]}
        ],
    }
    data.update(over)
    return PacketGold.model_validate(data)


def _finding(**over) -> dict:
    f = {
        "summary": "registration sits below the idempotency early-return",
        "severity": "critical",
        "file": "src/thing.py",
        "why_blocking": "a replayed emit returns success without registering",
        "evidence": "src/thing.py:40",
    }
    f.update(over)
    return f


def test_a_finding_counts_when_it_names_the_file_and_the_mechanism():
    s = score_packet(_gold(), {"disposition": "required_change", "findings": [_finding()]})
    assert s.disposition_correct and s.blocking_found
    assert s.must_find[0].found


def test_gesturing_at_the_right_file_is_not_finding_the_defect():
    s = score_packet(
        _gold(),
        {
            "disposition": "required_change",
            "findings": [_finding(summary="src/thing.py could use a comment",
                                  why_blocking="readability", evidence="src/thing.py")],
        },
    )
    assert s.disposition_correct
    assert not s.blocking_found, "a finding with no mechanism must not satisfy a must-find"


def test_naming_the_mechanism_against_the_wrong_file_is_not_either():
    s = score_packet(
        _gold(),
        {
            "disposition": "required_change",
            "findings": [_finding(file="other/place.py", evidence="other/place.py:1",
                                  summary="idempotency early-return problem in other/place.py",
                                  why_blocking="the early-return is wrong")],
        },
    )
    assert not s.blocking_found


def test_an_extra_finding_never_counts_against_a_reviewer():
    s = score_packet(
        _gold(),
        {
            "disposition": "required_change",
            "findings": [
                _finding(),
                _finding(summary="a different real thing", file="src/other.py",
                         evidence="src/other.py:9", severity="medium"),
            ],
        },
    )
    assert s.blocking_found and s.disposition_correct
    assert len(s.unmatched_findings) == 1
    assert not s.unsupported_high_severity, "extra findings only bite on the clean packet"


def test_on_the_clean_packet_an_unsupported_high_severity_finding_is_the_failure():
    gold = _gold(expected_disposition="no_required_change", must_find=[])
    s = score_packet(
        gold,
        {
            "disposition": "required_change",
            "findings": [_finding(summary="invented", severity="high", file="src/x.py",
                                  evidence="src/x.py:1")],
        },
    )
    assert not s.disposition_correct
    assert len(s.unsupported_high_severity) == 1

    fair = score_packet(
        gold,
        {
            "disposition": "required_change",
            "findings": [_finding(summary="wording in docs/", severity="high",
                                  file="docs/INDEX.md", evidence="docs/INDEX.md:2")],
        },
    )
    assert not fair.unsupported_high_severity, "an acceptable-optional reading is not invented"


def test_one_finding_cannot_satisfy_two_must_finds():
    """A broad finding that brushes two gold defects credits a reviewer with noticing two
    things when it noticed one. Caught by the cross-vendor review of this branch."""
    gold = _gold(
        must_find=[
            {
                "id": "first", "severity": "critical", "summary": "s",
                "files": ["src/thing.py"], "patterns": ["idempot"],
                "witness": {"argv": ["true"]}, "basis": ["order", "diff"],
            },
            {
                "id": "second", "severity": "high", "summary": "s",
                "files": ["src/thing.py"], "patterns": ["early.return"],
                "witness": {"argv": ["true"]}, "basis": ["order", "diff"],
            },
        ]
    )
    broad = _finding(summary="the idempotency early-return in src/thing.py is misplaced")
    s = score_packet(gold, {"disposition": "required_change", "findings": [broad]})
    assert [m.found for m in s.must_find] == [True, False]

    s2 = score_packet(
        gold,
        {
            "disposition": "required_change",
            "findings": [
                broad,
                _finding(summary="src/thing.py returns early before registering",
                         why_blocking="the early-return skips it"),
            ],
        },
    )
    assert all(m.found for m in s2.must_find)


def test_a_finding_that_is_not_an_object_makes_the_verdict_unreadable():
    s = score_packet(
        _gold(), {"disposition": "required_change", "findings": [_finding(), "oops"]}
    )
    assert s.inconclusive and "not objects" in s.because


def test_a_lower_severity_invention_on_the_clean_packet_is_reported_not_scored():
    """The order's rule makes only critical/high/approach decide green. A medium invention
    against work that shipped unchanged is still a signal, so it is carried in its own field
    rather than dropped."""
    gold = _gold(expected_disposition="no_required_change", must_find=[])
    s = score_packet(
        gold,
        {
            "disposition": "required_change",
            "findings": [_finding(summary="invented", severity="medium", file="src/x.py",
                                  evidence="src/x.py:1")],
        },
    )
    assert not s.unsupported_high_severity
    assert len(s.unsupported_lower_severity) == 1


def test_a_self_inconsistent_verdict_is_inconclusive_not_red():
    s = score_packet(_gold(), {"disposition": "no_required_change", "findings": [_finding()]})
    assert s.inconclusive and "self-inconsistent" in s.because


def test_no_verdict_at_all_is_inconclusive():
    s = score_packet(_gold(), None)
    assert s.inconclusive and not s.disposition_correct


# --- the fidelity rule -------------------------------------------------------------------------

def _score(**over) -> PacketScore:
    base: dict[str, Any] = {
        "packet_id": "p", "blind_id": "b", "expected_disposition": "required_change",
        "reported_disposition": "required_change", "disposition_correct": True,
    }
    base.update(over)
    return PacketScore(**base)


def test_fidelity_is_green_only_under_all_three_conditions():
    good = [_score(packet_id=f"p{i}") for i in range(5)]
    assert reviewer_fidelity(good).green

    one_wrong = [*good[:4], _score(packet_id="p4", disposition_correct=False)]
    assert reviewer_fidelity(one_wrong).green, (
        f"the rule allows {5 - MIN_DISPOSITIONS_CORRECT} wrong disposition"
    )

    two_wrong = [
        *good[:3],
        _score(packet_id="p3", disposition_correct=False),
        _score(packet_id="p4", disposition_correct=False),
    ]
    assert not reviewer_fidelity(two_wrong).green

    from taskbench.review_score import MustFindResult

    missed = [
        *good[:4],
        _score(packet_id="p4", must_find=[MustFindResult("d", "critical", False)]),
    ]
    assert not reviewer_fidelity(missed).green

    invented = [*good[:4], _score(packet_id="p4", unsupported_high_severity=[{"summary": "x"}])]
    assert not reviewer_fidelity(invented).green


def test_a_short_or_duplicated_score_set_is_never_green():
    """Four correct cells out of an intended five is not four of five — the fifth packet,
    and every blocking defect in it, simply never appeared. Caught by the cross-vendor
    review of this branch."""
    four = [_score(packet_id=f"p{i}") for i in range(4)]
    assert reviewer_fidelity(four).green, "with no expectation stated, four is all there is"
    short = reviewer_fidelity(four, expected_packets=5)
    assert not short.green and "never run" in short.because

    duped = [*[_score(packet_id=f"p{i}") for i in range(4)], _score(packet_id="p0")]
    assert not reviewer_fidelity(duped, expected_packets=5).green


def test_a_cell_that_produced_nothing_is_never_green():
    scores = [_score(packet_id=f"p{i}") for i in range(4)]
    scores.append(_score(packet_id="p4", inconclusive=True))
    f = reviewer_fidelity(scores)
    assert not f.green and f.inconclusive == ["p4"]


# --- isolation ------------------------------------------------------------------------------------

def test_a_fresh_home_is_fresh(tmp_path):
    spec = ReviewerCellSpec(argv=["true"], labels={}, home_seed=[])
    home = build_fresh_home(spec, tmp_path / "home")
    assert list(home.iterdir()) == []
    with pytest.raises(HomeError, match="not empty"):
        (home / "x").write_text("x")
        build_fresh_home(spec, home)


@pytest.mark.parametrize("seed", ["/etc/passwd", "../.ssh/id_rsa", "a/../../x"])
def test_a_home_seed_cannot_escape_the_fresh_home(tmp_path, seed):
    """An absolute or parent-bearing seed makes the source and the destination the same
    place outside the home, so the copy reads and writes operator state. Caught by the
    cross-vendor review of this branch."""
    spec = ReviewerCellSpec(argv=["true"], labels={}, home_seed=[seed])
    with pytest.raises(HomeError, match="escapes|relative"):
        build_fresh_home(spec, tmp_path / "home")


def test_a_directory_home_seed_is_refused(tmp_path, monkeypatch):
    """An agent CLI's state directory holds its prompt history and its per-project
    transcripts beside its credential. Seeding it hands a reviewer the record of the task it
    is grading — the contamination corpus v0.1 shipped with. Caught by the cross-vendor
    review of this branch."""
    fake_home = tmp_path / "operator"
    (fake_home / ".claude").mkdir(parents=True)
    (fake_home / ".claude" / ".credentials.json").write_text('{"token": "x"}')
    (fake_home / ".claude" / "history.jsonl").write_text("a transcript\n")
    monkeypatch.setenv("HOME", str(fake_home))

    spec = ReviewerCellSpec(argv=["true"], labels={}, home_seed=[".claude"])
    with pytest.raises(HomeError, match="is a directory"):
        build_fresh_home(spec, tmp_path / "h1")


def test_a_seeded_config_is_stripped_of_its_own_history(tmp_path, monkeypatch):
    fake_home = tmp_path / "operator"
    fake_home.mkdir()
    (fake_home / ".claude.json").write_text(
        json.dumps({"projects": {"/some/task": {"history": ["what I asked"]}}, "theme": "dark"})
    )
    monkeypatch.setenv("HOME", str(fake_home))

    spec = ReviewerCellSpec(
        argv=["true"], labels={}, home_seed=[".claude.json"],
        home_seed_strip_json_keys=["projects"],
    )
    home = build_fresh_home(spec, tmp_path / "h2")
    seeded = json.loads((home / ".claude.json").read_text())
    assert "projects" not in seeded and seeded["theme"] == "dark"
    assert "projects" in (home / "TASKBENCH-SEED.txt").read_text()


def test_a_probe_with_nothing_declared_does_not_pass_vacuously(tmp_path):
    """Reading every member of an empty set is true and proves nothing."""
    packet = tmp_path / "packet"
    packet.mkdir()
    spec = ReviewerCellSpec(argv=["true"], labels={}, sandbox_argv=[], home_seed=[])
    home = build_fresh_home(spec, tmp_path / "home")
    probe = run_probe(spec, packet_dir=packet, home=home, deny={}, declared_inputs=[])
    assert not probe.ok and "proves nothing" in probe.detail["probe_failed"]


def test_a_missing_home_seed_refuses_rather_than_producing_an_empty_cell(tmp_path):
    spec = ReviewerCellSpec(argv=["true"], labels={}, home_seed=[".no-such-config-xyz"])
    with pytest.raises(HomeError, match="does not exist"):
        build_fresh_home(spec, tmp_path / "home")


def test_the_probe_catches_no_isolation_at_all(tmp_path):
    """The probe has to be able to fail, or it proves nothing when it passes."""
    packet = tmp_path / "packet"
    packet.mkdir()
    (packet / "order.md").write_text("o\n")
    secret = tmp_path / "gold.yaml"
    secret.write_text("the answer\n")
    spec = ReviewerCellSpec(argv=["true"], labels={}, sandbox_argv=[], home_seed=[])
    home = build_fresh_home(spec, tmp_path / "home")
    probe = run_probe(
        spec, packet_dir=packet, home=home,
        deny={"gold": str(secret)}, declared_inputs=["order.md"],
    )
    assert probe.inputs_readable
    assert not probe.ok and probe.denied == {"gold": False}
