"""V4 Phase 1: the KB/persona loader (hash-verified) and R1 system-prompt composition. Pure."""

from __future__ import annotations

import pytest
from ladder.knowledge import _verified_text, load_kb, persona_blocks, sha256_of
from ladder.llm import LLMResponse, Usage
from ladder.vanilla import VanillaCoordinator
from qual.loader import QUAL_ROOT, load_catalog

CATALOG = load_catalog(QUAL_ROOT / "catalogs" / "board-ops-v2.yaml")


class _FakeLLM:
    def complete(self, system: str, user: str) -> LLMResponse:
        return LLMResponse(text="", usage=Usage(0, 0, "fake", 0.0))


# --- loader: SHA-256 verification against the pinned HASHES manifest ---

def test_load_kb_returns_pinned_text():
    kb = load_kb("coordination-kb-v1")
    assert "Coordination knowledge base" in kb and len(kb) > 500


def test_persona_blocks_are_role_only_no_fork_mechanics():
    p = persona_blocks()
    assert "[IDENTITY]" in p and "[OBJECTIVE]" in p and "[FEEDBACK]" in p
    assert "[MECHANICS]" not in p                 # the fork op-reference tail is dropped
    assert 'board "assign' not in p               # ...and its fork-format op shape with it


def test_verified_text_accepts_matching_hash(tmp_path):
    f = tmp_path / "kb.md"
    f.write_text("hello knowledge")
    (tmp_path / "HASHES").write_text(f"{sha256_of(f)}  kb.md\n")
    assert _verified_text(f) == "hello knowledge"


def test_verified_text_rejects_tampered_file(tmp_path):
    f = tmp_path / "kb.md"
    f.write_text("hello knowledge")
    (tmp_path / "HASHES").write_text("deadbeef  kb.md\n")   # wrong hash
    with pytest.raises(ValueError, match="SHA-256"):
        _verified_text(f)


# --- composition: persona + op-sheet base (identical across cells); KB only for L3 ---

def _system(*, knowledge: str | None) -> str:
    coord = VanillaCoordinator(llm=_FakeLLM(), catalog=CATALOG,
                               persona=persona_blocks(), knowledge=knowledge)
    return coord._system


def test_l2_prompt_is_persona_plus_opsheet_without_kb():
    s = _system(knowledge=None)
    assert "[IDENTITY]" in s                        # persona role blocks
    assert "assign" in s.lower()                    # op-reference sheet
    assert "Coordination knowledge base" not in s   # no KB


def test_l3_prompt_appends_the_kb_onto_the_identical_base():
    kb = load_kb("coordination-kb-v1")
    l3 = _system(knowledge=kb)
    assert kb in l3 and "[IDENTITY]" in l3
    # the base (persona + op-sheet) is byte-identical to L2's; the KB is strictly appended,
    # so the L3-vs-L2 contrast isolates exactly the KB.
    assert l3.startswith(_system(knowledge=None))
