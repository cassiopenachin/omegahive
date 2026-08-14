"""The four candidate launchers, held to the same contract the incumbent launcher earned.

An operator runs one of these and it spends money. So the contract is: it takes no arguments,
never asks a second time, assembles no shell command, names its own model and route as
literals, and cannot be re-pointed by an environment variable at the thing that decides what
the experiment IS. Everything checked here is a way a batch could quietly measure something
nobody approved.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from taskbench import CORPUS_ROOT
from taskbench.manifest import load_corpus
from taskbench.openrouter import DEEPSEEK_PIN, MUSE_PIN

LAUNCH = Path(__file__).resolve().parents[1] / "taskbench/launch"

WAVES = {
    "wave-1-haiku-claude-code.sh": "claude-haiku-4-5",
    "wave-2-luna-codex.sh": "gpt-5.6-luna",
    "wave-3-deepseek-paired.sh": DEEPSEEK_PIN.request_string,
    "wave-4-muse-claude-code.sh": MUSE_PIN.request_string,
}
ALL_SCRIPTS = [*WAVES, "qualify-setup.sh", "cell-codex.sh", "cell-reasonix.sh",
               "cell-claude-openrouter.sh", "lib.sh"]


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_every_script_exists_and_is_executable(name):
    path = LAUNCH / name
    assert path.is_file(), path
    assert path.stat().st_mode & 0o111, f"{name} is not executable"


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_every_script_is_shellcheck_clean(name):
    if subprocess.run(["which", "shellcheck"], capture_output=True, check=False).returncode:
        pytest.skip("shellcheck not on PATH")
    out = subprocess.run(
        ["shellcheck", "-x", str(LAUNCH / name)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stdout


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_every_script_parses(name):
    out = subprocess.run(
        ["bash", "-n", str(LAUNCH / name)], capture_output=True, text=True, check=False
    )
    assert out.returncode == 0, out.stderr


@pytest.mark.parametrize("name", WAVES)
def test_a_wave_takes_no_arguments_and_never_asks_twice(name):
    """Running it IS the approval. A second prompt turns a signed batch into a habit."""
    body = (LAUNCH / name).read_text()
    assert "eval " not in body, "model commands are argv arrays, never shell-evaluated"
    for parser in ("getopts", 'case "$1"', "case $1"):
        assert parser not in body, f"{name} takes no arguments"
    for prompt in ("read -p", "read -r -p", "read -n"):
        assert prompt not in body, "invoking the script is the approval; it must not ask again"


@pytest.mark.parametrize("name", WAVES)
def test_a_wave_pins_the_corpus_it_was_written_against(name):
    """Reached through lib.sh, which every wave sources — the hash is stated in exactly one
    place so four launchers cannot drift apart."""
    frozen = load_corpus(CORPUS_ROOT / "v0.1").content_hash
    assert frozen in (LAUNCH / "lib.sh").read_text()
    assert "lib.sh" in (LAUNCH / name).read_text()


@pytest.mark.parametrize(("name", "model"), WAVES.items())
def test_a_wave_names_its_exact_model_as_a_literal(name, model):
    """Not a variable somebody could re-point: the model string IS the experiment's identity."""
    assert model in (LAUNCH / name).read_text(), f"{name} must name {model} literally"


@pytest.mark.parametrize("name", WAVES)
def test_no_wave_writes_to_the_canonical_checkout(name):
    body = (LAUNCH / name).read_text()
    assert "git checkout" not in body and "git switch" not in body
    assert "src/SNET/omegahive" not in body, (
        "the launcher resolves its own repo root; naming the canonical checkout invites it"
    )


@pytest.mark.parametrize("name", ALL_SCRIPTS)
def test_no_script_reads_a_secrets_file_or_prints_a_key(name):
    """The key reaches these processes through the environment and nowhere else."""
    body = (LAUNCH / name).read_text()
    for forbidden in (". ~/.secrets", "source ~/.secrets", "cat ~/.secrets"):
        assert forbidden not in body, f"{name} must never read a secrets file"
    # Two scripts expand the key, both because the order says they may, and both under a
    # stated condition: `cell-reasonix.sh` writes the one mandated per-cell `.env` (0600,
    # removed on every exit path), and `cell-claude-openrouter.sh` derives the
    # harness-compatibility name process-locally without persisting a duplicate. Every other
    # script must never touch the value at all.
    if name not in ("cell-reasonix.sh", "cell-claude-openrouter.sh"):
        assert "$OPENROUTER_API_KEY" not in body.replace(
            '"${OPENROUTER_API_KEY:-}"', ""
        ).replace("${OPENROUTER_API_KEY:-}", ""), f"{name} must not expand the key"


def test_the_reasonix_wrapper_removes_its_env_on_every_exit_path():
    """The one bounded exception the order grants, and the condition attached to it."""
    body = (LAUNCH / "cell-reasonix.sh").read_text()
    assert "trap cleanup EXIT INT TERM HUP" in body
    assert "rm -rf" in body
    assert "umask 077" in body, "the file must never be briefly world-readable"
    assert "chmod 600" in body


def test_the_codex_wrapper_carries_auth_and_nothing_else():
    """Fresh state by absence, not by a set of disable flags whose meaning can change."""
    body = (LAUNCH / "cell-codex.sh").read_text()
    assert "auth.json" in body
    assert "--ignore-user-config" in body
    assert "trap cleanup EXIT INT TERM" in body
    for carried in ("sessions", "memories", "skills", "plugins"):
        assert carried not in body.split("# Everything after")[-1], (
            f"the wrapper must not copy {carried} into the cell home"
        )


def test_the_paired_wave_freezes_its_schedule_before_any_result_exists():
    body = (LAUNCH / "wave-3-deepseek-paired.sh").read_text()
    assert "TASK_ORDER=(" in body and "LEAD_ORDER=(" in body
    assert "frozen-schedule.txt" in body
    # Alternating lead: neither harness is systematically first.
    leads = body.split("LEAD_ORDER=(")[1].split(")")[0].split()
    assert leads.count("reasonix") >= 2 and leads.count("claude-code") >= 2
    assert leads[0] != leads[1], "the lead must alternate, not run in blocks"


def test_the_paired_wave_signs_both_arms_in_one_command():
    """Signing them separately would let one arm run under conditions the other did not."""
    body = (LAUNCH / "wave-3-deepseek-paired.sh").read_text()
    assert "wave-3a-deepseek-reasonix" in body and "wave-3b-deepseek-claude-code" in body
    assert body.count("run_arm_task") >= 3


def test_both_deepseek_arms_use_the_same_preset_and_upstream():
    """The pair's entire claim. Two presets would make it two unrelated runs."""
    body = (LAUNCH / "wave-3-deepseek-paired.sh").read_text()
    assert body.count(f'PRESET="{DEEPSEEK_PIN.slug}"') == 1
    assert "gmicloud/fp8" in body
    assert "--preset \"$PRESET\"" in body


def test_the_muse_wave_records_its_substitution_and_its_context_bound():
    """Two facts the report must carry, written where they cannot be quietly dropped."""
    body = (LAUNCH / "wave-4-muse-claude-code.sh").read_text()
    assert "unreachable" in body, "the Muse Code arm's disposition must be stated"
    assert "provider-openrouter" in body, "and the reason it is unreachable"
    assert "200k" in body and "1,048,576" in body, (
        "the harness-effective context must be reported against the advertised one"
    )
    assert "contributor" in body, "the prohibited SKU must be named, not merely omitted"


@pytest.mark.parametrize("name", WAVES)
def test_every_wave_runs_the_pause_point_task_first(name):
    """One fixed, precommitted cheapest/high-signal task leads every bundle, and its completion
    is a pause point rather than permission to score a partial bundle.

    Stated once in `lib.sh` and referenced by every wave, so the four launchers cannot drift
    into leading with different tasks — which would quietly break the comparison.
    """
    body = (LAUNCH / name).read_text()
    assert 'FIRST_TASK="docs-triage"' in (LAUNCH / "lib.sh").read_text()
    assert "FIRST_TASK" in body or "docs-triage" in body
    assert "pause point" in body.lower()


def test_the_pause_point_task_is_actually_in_the_held_in_set():
    """A lead task that is not in the corpus would refuse every batch at preflight."""
    lib = (LAUNCH / "lib.sh").read_text()
    first = lib.split('FIRST_TASK="')[1].split('"')[0]
    assert first in load_corpus(CORPUS_ROOT / "v0.1").catalog.held_in


@pytest.mark.parametrize("name", WAVES)
def test_no_wave_can_name_a_held_out_task(name):
    reserved = load_corpus(CORPUS_ROOT / "v0.1").catalog.held_out
    body = (LAUNCH / name).read_text()
    for task_id in reserved:
        assert task_id not in body, (
            f"{name} names held-out task {task_id}; the reservation is the one thing this "
            "instrument cannot get back once spent"
        )


@pytest.mark.parametrize("name", WAVES)
def test_every_wave_uses_the_one_shared_reviewer_block(name):
    """A candidate-specific reviewer would invalidate the whole comparable set, so no wave
    emits its own."""
    body = (LAUNCH / name).read_text()
    assert "emit_reviewer_block" in body
    assert "reviewer:" not in body, f"{name} must not write its own reviewer configuration"


# --- the OpenRouter Claude arms -------------------------------------------------------------


def test_the_openrouter_claude_arms_go_through_the_bare_wrapper():
    """Without `--bare`, Claude Code can satisfy itself from the operator's Anthropic OAuth
    subscription and never reach OpenRouter — a cell that runs a different model from the one
    its record claims. Wave 1 is the deliberate exception: it IS the subscription."""
    wrapper = (LAUNCH / "cell-claude-openrouter.sh").read_text()
    assert "--bare" in wrapper
    assert "--strict-mcp-config" in wrapper
    for name in ("wave-3-deepseek-paired.sh", "wave-4-muse-claude-code.sh"):
        body = (LAUNCH / name).read_text()
        assert "cell-claude-openrouter.sh" in body, f"{name} must use the wrapper"
        assert "$CLAUDE_BIN" not in body, f"{name} must not invoke claude directly"
    wave1 = (LAUNCH / "wave-1-haiku-claude-code.sh").read_text()
    assert "cell-claude-openrouter.sh" not in wave1, (
        "wave 1 runs on the Anthropic subscription and needs OAuth, which --bare disables"
    )


def test_the_wrapper_derives_the_harness_key_without_persisting_a_duplicate():
    """The order allows one operator secret for all three direct-cost arms and permits a
    process-local derivation; it forbids a durably persisted duplicate."""
    body = (LAUNCH / "cell-claude-openrouter.sh").read_text()
    assert 'export ANTHROPIC_API_KEY="$OPENROUTER_API_KEY"' in body
    for persist in (">", "cat >", "tee"):
        assert f"ANTHROPIC_API_KEY{persist}" not in body
    assert "$HOME" not in body, "the derivation must not touch a profile or home file"


# --- settings alignment on the matched pair -------------------------------------------------


def test_the_reasonix_arm_switches_off_the_optional_subsystems_by_name():
    """By name rather than by hoping a default holds: the order requires web, MCP, memory,
    planner and subagent behaviour off for both arms."""
    body = (LAUNCH / "cell-reasonix.sh").read_text()
    # The invocation, not the comment that explains it.
    line = next(ln for ln in body.splitlines() if ln.strip().startswith("--ablate"))
    for subsystem in ("evidence", "planner", "subagent", "retrieval", "compaction"):
        assert subsystem in line, f"{subsystem} must be ablated"
    assert "--preset balanced" in body, (
        "the execution preset must be pinned explicitly, so a changed default cannot move the arm"
    )
    assert "--metrics" in body, "the harness-side token totals must be a recorded fact"


def test_the_paired_wave_records_what_it_could_not_align():
    """An irreducible difference that is not written down reads afterwards as one nobody
    checked."""
    body = (LAUNCH / "wave-3-deepseek-paired.sh").read_text()
    assert "IRREDUCIBLE" in body
    assert "not the same unit" in body
    assert "harness default" in body.lower()
    assert "system prompts" in body


def test_neither_deepseek_arm_overrides_the_output_cap():
    """Aligned by both leaving it to the endpoint, which then decides identically for each.
    An override on one side only would be the confound this pair exists to avoid."""
    for name in ("cell-reasonix.sh", "cell-claude-openrouter.sh"):
        body = (LAUNCH / name).read_text()
        for override in ("--max-tokens", "MAX_OUTPUT_TOKENS", "max_tokens"):
            assert override not in body, f"{name} overrides the output cap on one side only"
