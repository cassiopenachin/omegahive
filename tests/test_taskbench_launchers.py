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


def code(name: str) -> str:
    """The script with its comments stripped.

    These scripts explain themselves at length, and several of the explanations quote the very
    flags and constructs the checks below forbid. Searching the raw text finds the warning
    against a thing and calls it the thing.
    """
    lines = []
    for line in (LAUNCH / name).read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)

WAVES = {
    "wave-1-haiku-claude-code.sh": "claude-haiku-4-5",
    "wave-2-luna-codex.sh": "gpt-5.6-luna",
    "wave-3-deepseek.sh": DEEPSEEK_PIN.request_string,
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


def test_the_codex_wrapper_seeds_the_cell_home_with_auth_and_nothing_else():
    """Fresh state by absence, not by a set of disable flags whose meaning can change.

    Direction matters, and the first version of this test got it wrong by forbidding a word
    rather than a behaviour: copying prior state *into* the fresh home is what must never
    happen, while copying evidence *out* of it before deletion is required — the rollout is the
    only place Codex records which model it ran.
    """
    body = code("cell-codex.sh")
    assert "--ignore-user-config" in body
    assert "trap cleanup EXIT INT TERM" in body

    # The behaviour, not a word: every copy whose DESTINATION is inside the cell home must be
    # the credential. Anything else would be prior state seeded into a home whose whole purpose
    # is to have none.
    into_home = [
        ln.strip() for ln in body.splitlines()
        if ln.strip().startswith("cp ") and "$CELL_HOME" in ln.split("cp ", 1)[1].split()[-1]
    ]
    assert into_home == ['cp "$SOURCE_AUTH" "$CELL_HOME/auth.json"'], into_home

    # And the reverse direction is required: the rollout leaves before the home is deleted.
    assert body.index('cp {} "$BENCH_CELL_ROOT') < body.index('rm -rf "$CELL_HOME"\n}')






def test_both_deepseek_arms_use_the_same_preset_and_upstream():
    """The pair's entire claim. Two presets would make it two unrelated runs."""
    body = (LAUNCH / "wave-3-deepseek.sh").read_text()
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
    for name in ("wave-3-deepseek.sh", "wave-4-muse-claude-code.sh"):
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



def test_neither_deepseek_arm_overrides_the_output_cap():
    """Aligned by both leaving it to the endpoint, which then decides identically for each.
    An override on one side only would be the confound this pair exists to avoid."""
    for name in ("cell-reasonix.sh", "cell-claude-openrouter.sh"):
        body = (LAUNCH / name).read_text()
        for override in ("--max-tokens", "MAX_OUTPUT_TOKENS", "max_tokens"):
            assert override not in body, f"{name} overrides the output cap on one side only"


@pytest.mark.parametrize("name", WAVES)
def test_every_wave_smokes_the_bundle_before_it_spends(name):
    """The order requires a tool loop proved before the matrix, using the bundle's real argv."""
    body = code(name)
    assert "qualify-smoke" in body
    assert '--config "$CONFIG"' in body or "smoke-$arm.yaml" in body, (
        "the smoke must run the config the batch will use, not an approximation of it"
    )
    assert "does NOT run" in body or "NEITHER arm runs" in body, (
        "a failed smoke must stop the batch"
    )



# --- regressions from the independent review -------------------------------------------------


@pytest.mark.parametrize("name", ["cell-reasonix.sh", "cell-codex.sh"])
def test_a_wrapper_with_a_cleanup_trap_never_execs(name):
    """`exec` replaces the shell's process image and DISCARDS the EXIT trap. With `exec`, the
    reasonix wrapper left a 0600 file containing the operator's OpenRouter key in every cell
    root, and the codex wrapper left a copy of the ChatGPT credential — and cell roots are
    retained with the record. Verified: `bash -c 'trap "echo X" EXIT; exec /bin/echo hi'` prints
    only `hi`."""
    body = code(name)
    assert "trap cleanup EXIT" in body
    # The shell builtin, which is `exec` at the start of a command — not `codex exec`, which is
    # a subcommand of a different program and replaces nothing.
    for line in body.splitlines():
        assert not line.strip().startswith("exec "), (
            f"{name} must not exec: it has a cleanup trap that exec would discard"
        )
    assert 'exit "$status"' in body, "the harness's exit code must still reach the runner"


def test_the_claude_wrapper_may_exec_because_it_has_no_cleanup_to_lose():
    body = code("cell-claude-openrouter.sh")
    assert "trap " not in body, "if this ever grows a cleanup trap, the exec below must go"
    assert "exec claude" in body


@pytest.mark.parametrize("name", ["wave-3-deepseek.sh", "wave-4-muse-claude-code.sh"])
def test_the_json_flags_stay_where_preflight_can_see_them(name):
    """`preflight.check_agent_command` refuses a config declaring the claude-code-json envelope
    whose argv never asks for JSON. Hiding those flags inside the wrapper made preflight refuse
    the whole batch before a single cell ran."""
    body = code(name)
    assert '"--output-format", "json"' in body
    assert "result_envelope: claude-code-json" in body
    assert "--output-format" not in code("cell-claude-openrouter.sh"), (
        "the flags must be in the launcher's argv, not swallowed by the wrapper"
    )


def test_the_openrouter_claude_argv_actually_passes_preflight():
    """Not asserted from the text but executed: build the spec the launcher writes and run the
    real check against it."""
    from taskbench.preflight import check_agent_command
    from taskbench.runner import AgentSpec

    spec = AgentSpec(
        argv=[
            str(LAUNCH / "cell-claude-openrouter.sh"), "--model", "m",
            "--print", "--output-format", "json", "--permission-mode", "auto",
        ],
        labels={"vendor": "v", "model": "m", "harness": "h"},
        result_envelope="claude-code-json",
    )
    assert check_agent_command(spec, label="agent") == []


def test_codex_is_not_given_a_working_directory_the_runner_already_set():
    """`run_cell` launches with cwd=<cell>/code; a `-C code` on top resolves to code/code."""
    body = code("wave-2-luna-codex.sh")
    assert '"-C", "code"' not in body
    assert "cwd: code" in body


@pytest.mark.parametrize("name", [*WAVES, "qualify-setup.sh"])
def test_every_launcher_stops_the_harness_updating_under_it(name):
    """A batch runs for hours and Claude Code updates itself. Between two cells that is a
    recorded dimension changing silently; between the two arms of the matched pair it breaks the
    only claim that pair makes."""
    body = code(name) if name == "qualify-setup.sh" else code(name) + code("lib.sh")
    assert "DISABLE_AUTOUPDATER=1" in body, f"{name} must disable the auto-updater"


# --- the pause point, and why it is only advisory ---------------------------------------------


@pytest.mark.parametrize("name", ["wave-1-haiku-claude-code.sh", "wave-2-luna-codex.sh",
                                  "wave-4-muse-claude-code.sh"])
def test_a_wave_declares_the_whole_held_in_set(name):
    """`preflight.check_corpus` receives the launch's task list as `expect_held_in` and refuses
    unless it equals the corpus's held-in set. A launch that silently narrows the study is what
    that guard exists to catch — and it is also why the pause cannot be staged as one cell per
    invocation, which was tried and refused before a single model call."""
    tasks_line = next(ln for ln in code(name).splitlines() if "--tasks" in ln)
    # The lead task is referenced as $FIRST_TASK, stated once in lib.sh so the four launchers
    # cannot drift into leading with different tasks.
    first = code("lib.sh").split('FIRST_TASK="')[1].split('"')[0]
    declared = tasks_line.replace("$FIRST_TASK", first)
    for task in load_corpus(CORPUS_ROOT / "v0.1").catalog.held_in:
        assert task in declared, (
            f"{name} must declare {task}; a narrowed batch is refused by preflight"
        )


@pytest.mark.parametrize("name", ["wave-1-haiku-claude-code.sh", "wave-2-luna-codex.sh",
                                  "wave-4-muse-claude-code.sh"])
def test_the_advisory_pause_states_its_reason(name):
    """A limitation stated is one a reader can weigh; left implicit it reads as an oversight the
    next time somebody trips over it."""
    body = (LAUNCH / name).read_text()
    assert "ADVISORY" in body
    assert "pipeline.py" in body, "the reason must name what would have to change"
    assert "Ctrl-C" in body, "and what the operator can do instead"


# --- the permission mode, which the harness redefined under us --------------------------------


@pytest.mark.parametrize("name", ["wave-1-haiku-claude-code.sh", "wave-3-deepseek.sh",
                                  "wave-4-muse-claude-code.sh", "lib.sh"])
def test_no_claude_invocation_uses_the_auto_permission_mode(name):
    """On 2.1.231 `auto` let a cell read, edit and run — every incumbent cell invoked
    bin/bench-verify. On 2.1.233 it denies edits outright, so a candidate cannot write a single
    file. A candidate that cannot write is not a weaker candidate, it is a broken measurement."""
    assert '"--permission-mode", "auto"' not in code(name)


@pytest.mark.parametrize("name", ["wave-1-haiku-claude-code.sh", "wave-3-deepseek.sh",
                                  "wave-4-muse-claude-code.sh"])
def test_every_claude_arm_uses_the_shared_tool_grant(name):
    """One helper, so four launchers and the reviewer cannot drift into different capabilities —
    which would be a confound sitting directly on top of the thing being measured."""
    assert "emit_claude_tool_grant" in code(name)


def test_the_tool_grant_permits_editing_and_running():
    grant = code("lib.sh").split("emit_claude_tool_grant()")[1].split("}")[0]
    for tool in ("Bash", "Edit", "Write", "Read"):
        assert f'"{tool}"' in grant, f"{tool} must be granted or the cell cannot work"
    assert '"acceptEdits"' in grant


def test_the_variadic_flag_is_never_last_before_the_kickoff():
    """`--allowedTools` is variadic and the runner appends the kickoff as the final argv element.
    Put it last and it swallows the prompt: 'Input must be provided either through stdin or as a
    prompt argument when using --print'. `--permission-mode` follows it for exactly that reason."""
    grant = code("lib.sh").split("emit_claude_tool_grant()")[1].split("}")[0]
    lines = [ln for ln in grant.splitlines() if "printf" in ln]
    assert "--permission-mode" in lines[-1], (
        "the last thing emitted must be a non-variadic flag and its value"
    )
    assert "--allowedTools" not in lines[-1]


def test_the_codex_wrapper_preserves_the_rollout_before_deleting_the_home():
    """Cleanup removes the CREDENTIAL, not the evidence. Deleting the home wholesale threw away
    the only place Codex records which model it ran, leaving every cell unattributable — which
    the record validator then refused, after all five cells had been spent."""
    body = code("cell-codex.sh")
    assert "codex-rollout.jsonl" in body
    assert body.index("cp {}") < body.index('rm -rf "$CELL_HOME"'), (
        "the rollout must be copied out before the home is removed"
    )
    assert "taskbench.harness_model" in body


def test_the_dropped_reasonix_arm_is_recorded_in_the_launcher_itself():
    """The matched pair was the order's most-discussed arm. A launcher that simply stopped
    mentioning it would let the result be read as though the pair had never been designed."""
    body = (LAUNCH / "wave-3-deepseek.sh").read_text()
    assert "DESIGNED AS A MATCHED PAIR AND IS NOT ONE" in body
    assert "unanswered" in body, "the cost of dropping it must be stated, not implied"
    assert "NOT evidence that Reasonix cannot drive DeepSeek" in body, (
        "the claim must stay bounded to the one run that supports it"
    )
    assert "scoping decision" in body


def test_wave_3_is_a_single_arm_on_the_pinned_deepseek_route():
    body = code("wave-3-deepseek.sh")
    assert "ARM_ORDER=(claude-code)" in body
    assert "cell-reasonix.sh" not in body
    assert "gmicloud/fp8" in body
    assert DEEPSEEK_PIN.request_string in body
