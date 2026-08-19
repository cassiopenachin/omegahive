"""The adapter contract: a route becomes an argv vector, never a shell string.

`hive-launch` used to execute one deployment string, `HIVE_WORKER_CMD`, interpolated
into a tmux command line. That is a shell-string composition boundary, and the named
risk this module answers is "an argv-safe interface hiding a shell-string escape one
layer down". So the contract is a VECTOR, end to end: the adapter returns `argv` as a
list, the CLI hands it to the shell as NUL-separated fields, and the supervisor execs
it directly. At no point is a prompt, a path, or a model id concatenated into a string
a shell will parse.

What an adapter returns, and why each piece is here:

  argv              the exact vector to exec
  env               a DELIBERATELY CONSTRUCTED environment — an allowlist, not the
                    parent's, so nothing reaches the child by accident
  version_argv      a no-model probe for the installed harness version
  model_requested   what the adapter actually put on the command line, recorded so a
                    later mismatch against the resolved model is visible
  usage_extractor   which parser can read this harness's consumption surface
  proves_model      whether this harness exposes a RESOLVED model identity at all

Unknown harnesses fail closed here, behind the interface. There is no path by which an
unrecognized harness name re-enters as an arbitrary shell string.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field

from omegahive.harness.bindings import HarnessBinding
from omegahive.harness.records import RefusalError, RouteEntry

# The only environment variables a worker process inherits, unless its adapter adds
# more by name. Everything else is dropped.
#
# This is an allowlist rather than a denylist because the failure modes are asymmetric:
# a missing variable makes a harness complain loudly, while an unexpected one — an API
# key, a proxy, a stray `ANTHROPIC_*` override — changes who gets billed and what model
# answers, silently. The order's stop-line ("no API key in a worker process") is
# enforced structurally here, not by remembering to unset things.
BASE_ENV_ALLOWLIST = frozenset(
    {
        "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TERM",
        "TMPDIR", "TZ", "USER", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
    }
)

# Anything matching these is never forwarded, even if an adapter names it. Belt and
# braces over the allowlist: an adapter author adding a variable by name should not be
# able to reintroduce a credential by accident.
_CREDENTIAL_MARKERS = ("API_KEY", "SECRET", "TOKEN", "PASSWORD", "CREDENTIAL")

# `2.1.232`, `0.147.0`, `9.9.9` — a digit, a dot, a digit. Deliberately narrower than
# "starts with a digit", because the point is to pick the version out of a line that
# also carries a product name.
_VERSION_SHAPED = re.compile(r"^[0-9]+\.[0-9]")


@dataclass(frozen=True)
class LaunchContext:
    """Everything the adapter needs that is not a property of the route."""

    kickoff: str                 # the worker's first message, passed as argv, never shell
    cwd: str                     # the worker's workspace clone — the worker ROOT
    execution_id: str
    session_id: str              # pinned by the supervisor so usage is findable later
    parent_env: Mapping[str, str] = field(default_factory=dict)
    # The worker's code clone. It is outside the worker root, so a harness that scopes
    # file access to the cwd needs it named explicitly in the materialized config —
    # which is why it travels here rather than being discovered.
    code_root: str = ""
    # The supervisor's private run-dir. A harness whose boundary IS its state directory
    # gets that directory here: it must live outside every writable root (it holds the
    # ephemeral credential copy and the plan the boundary check is anchored to) and it
    # must be removable on every terminal path without touching the worker's work.
    run_dir: str = ""


def boundary_root(binding: HarnessBinding, ctx: LaunchContext) -> str:
    """The ABSOLUTE path the descriptor's `config_path` hangs off, for this execution.

    Resolved in one place because three consumers need the same answer and would
    otherwise each recompute it: the adapter (which puts it in the child's
    environment), the launcher (which writes the files) and the supervisor (which
    re-checks their bytes). `config_root` selects the base; an empty base yields an
    empty answer, which is how a `--check` with no run-dir stays free of side effects
    rather than inventing a path.
    """
    if binding.config_path is None:
        return ""
    base = ctx.run_dir if binding.config_root == "run" else ctx.cwd
    if not base:
        return ""
    return f"{base.rstrip('/')}/{binding.config_path}"


@dataclass(frozen=True)
class LaunchPlan:
    """The resolved, executable form of one route. Pure data — no side effects."""

    argv: list[str]
    env: dict[str, str]
    version_argv: list[str]
    model_requested: str
    usage_extractor: str
    # Where the usage evidence will be found, as opaque hints for the extractor.
    usage_hint: dict[str, str]
    proves_model: bool
    proves_usage: bool
    # Named reason when a capability is absent — this becomes the `unavailable` reason
    # on the finished fact, so it must read as an explanation to an operator.
    unproven_reason: str | None = None


def _clean_env(
    parent: Mapping[str, str], extra_names: frozenset[str], additions: dict[str, str]
) -> dict[str, str]:
    """Build the child environment from an allowlist plus explicit additions."""
    allowed = BASE_ENV_ALLOWLIST | extra_names
    env = {
        k: v
        for k, v in parent.items()
        if k in allowed and not any(m in k.upper() for m in _CREDENTIAL_MARKERS)
    }
    for k, v in additions.items():
        if any(m in k.upper() for m in _CREDENTIAL_MARKERS):
            raise RefusalError(
                "ADAPTER_REFUSED_CREDENTIAL",
                f"adapter tried to place {k!r} in a worker environment; credential "
                "delivery is not this order's surface and never a plain env var",
            )
        env[k] = v
    return env


class Adapter:
    """Base contract. Subclasses are data + one `build`; keep them dumb and testable.

    `build` takes the harness BINDING DESCRIPTOR alongside the route, and the boundary
    flags come out of that descriptor verbatim rather than being spelled a second time
    in adapter code. Two spellings of one boundary drift; one spelling with a checker
    over it does not — `check_argv` verifies the argv this method actually produced
    against the same descriptor, so a future edit that drops a flag is caught at
    preflight rather than discovered in a worker that was never bound.
    """

    name: str = ""
    executable: str = ""

    def build(  # pragma: no cover
        self, route: RouteEntry, ctx: LaunchContext, binding: HarnessBinding
    ) -> LaunchPlan:
        raise NotImplementedError

    @staticmethod
    def boundary_flags(binding: HarnessBinding) -> list[str]:
        """The descriptor's subcommand and required flags, in argv order.

        The subcommand belongs here rather than in adapter code because a flag can be
        valid only under one — `--ignore-user-config` exits 2 on bare `codex` and works
        under `codex exec` — so a descriptor that names the flag and not the subcommand
        describes an argv that cannot start the harness.
        """
        return binding.argv_prefix

    def version_argv(self) -> list[str]:
        return [self.executable, "--version"]

    def parse_version(self, output: str) -> str:
        """The first version-SHAPED token anywhere, else a digit-leading one, else the
        first token at all.

        Two harnesses put it in two places. `claude --version` prints
        `2.1.231 (Claude Code)` — the version is field one. `codex --version` prints
        `codex-cli 0.147.0` — the version is field two, and a rule that only looked at
        field one recorded `codex-cli` as the harness version. Measured 2026-08-19, and
        it is not cosmetic: that value is what `status: proven` is tied to and what the
        supervisor compares series against before the child exists, so a product name
        recorded there makes every launch look like a series change.

        The digit preference is not cosmetic either. The probe merges stderr so that a
        harness which fails to start can say why — which means an unrelated warning
        (`bash: warning: setlocale: ...`) can land on the first line, and the naive rule
        records that warning's first word as the version. Observed 2026-08-14 on a real
        preflight, which reported `harness: sh:`. A version fact naming a shell is worse
        than no fact. The shell twin is `harness_version_from` in hive-common.sh, and a
        test holds the two implementations together.
        """
        first = ""
        digit_first = ""
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            tokens = line.split()
            for token in tokens:
                if _VERSION_SHAPED.match(token):
                    return token
            if not digit_first and tokens[0][:1].isdigit():
                digit_first = tokens[0]
            if not first:
                first = tokens[0]
        return digit_first or first


class ClaudeCodeAdapter(Adapter):
    """Claude Code.

    Two flags carry the weight. `--model` takes an exact id (`claude-opus-5`), not only
    a friendly alias, so the catalog's pinned model goes on the command line verbatim.
    `--session-id` lets the supervisor PIN the session uuid, which is what makes usage
    extraction deterministic: the harness writes its own transcript to
    `<config>/projects/<cwd-slug>/<session-id>.jsonl`, and pinning the uuid means the
    supervisor knows which file to read without guessing or globbing by mtime.

    That transcript is both surfaces at once — it carries the RESOLVED model id per
    assistant message and the provider-reported token counts — so this harness proves
    model and usage with no extra model call and no paid probe.
    """

    name = "claude-code"
    executable = "claude"

    def build(
        self, route: RouteEntry, ctx: LaunchContext, binding: HarnessBinding
    ) -> LaunchPlan:
        argv = [
            self.executable,
            *self.boundary_flags(binding),
            "--model", route.model,
            "--session-id", ctx.session_id,
            ctx.kickoff,
        ]
        env = _clean_env(ctx.parent_env, frozenset({"CLAUDE_CONFIG_DIR"}), {})
        hint = {"session_id": ctx.session_id, "cwd": ctx.cwd}
        if "CLAUDE_CONFIG_DIR" in env:
            hint["config_dir"] = env["CLAUDE_CONFIG_DIR"]
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv(),
            model_requested=route.model,
            usage_extractor="claude-code-transcript",
            usage_hint=hint,
            proves_model=True,
            proves_usage=True,
        )


class CodexAdapter(Adapter):
    """OpenAI Codex CLI.

    The whole boundary travels in ONE environment variable. `CODEX_HOME` names a
    generated per-execution directory holding the permission profile, the execpolicy
    rules, and an opaque copy of the operator's existing `auth.json` — so the
    operator's own harness state (prior threads, memory, plugins, personal
    configuration) is absent by construction rather than disabled by a flag. The
    launcher renders that directory; the supervisor seeds the credential into it,
    verifies its bytes, and removes it on every terminal path.

    `CODEX_HOME` is therefore set by this adapter and never inherited: an inherited
    value would silently point the child at the operator's real home, which is the one
    outcome that reads as a bound launch and is not one.

    What the argv deliberately does NOT carry, both measured on codex-cli 0.147.0:
    `--sandbox <mode>`, because it OVERRIDES the permission profile (with the profile
    denying a planted secret and `-s workspace-write` on the command line, the agent
    read the secret); and `--ignore-user-config`, because it suppresses exactly the
    `config.toml` this launcher just wrote. Both are forbidden argv tokens in the
    descriptor rather than comments here.

    Evidence surfaces are now established. Codex writes a session ROLLOUT under the
    home it is given, and that rollout carries both the configured model id and the
    per-turn token counts — so `proves_model` and `proves_usage` are True, with the
    standing caveat (carried by the extractor, not hidden here) that Codex reports the
    model it was CONFIGURED with rather than a server-resolved identity.
    """

    name = "codex"
    executable = "codex"

    def build(
        self, route: RouteEntry, ctx: LaunchContext, binding: HarnessBinding
    ) -> LaunchPlan:
        argv = [
            self.executable,
            *self.boundary_flags(binding),
            "--model", route.model,
            ctx.kickoff,
        ]
        home = boundary_root(binding, ctx)
        if not home:
            raise RefusalError(
                "HARNESS_BINDING_UNRENDERABLE",
                "the codex adapter needs the generated harness home the descriptor "
                "declares (config_path under config_root); without it the child would "
                "read the operator's own Codex state and no boundary would apply",
            )
        # NOT inherited: whatever the parent had is dropped and this execution's own
        # directory is placed. `_clean_env` is still given the name so the allowlist
        # documents that this variable is expected, but the addition wins.
        env = _clean_env(ctx.parent_env, frozenset(), {"CODEX_HOME": home})
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv(),
            model_requested=route.model,
            usage_extractor="codex-rollout",
            usage_hint={"codex_home": home, "run_dir": ctx.run_dir},
            proves_model=True,
            proves_usage=True,
        )


class FakeAdapter(Adapter):
    """A deterministic adapter for tests. Runs a fixture script, never a model.

    It exists so the full supervisor path — version probe, started, child, usage
    extraction, finished — can be exercised end to end with no paid call and no
    network. The route's `model` names the fixture behaviour, which is what lets one
    test drive success, failure, and interruption through the real code path.
    """

    name = "fake"
    executable = "hive-fake-harness"

    def build(
        self, route: RouteEntry, ctx: LaunchContext, binding: HarnessBinding
    ) -> LaunchPlan:
        # The fixture executable is supplied by the test through the parent env; there
        # is no default, so a fake route can never accidentally resolve to something
        # real on an operator's PATH.
        exe = ctx.parent_env.get("HIVE_FAKE_HARNESS")
        if not exe:
            raise RefusalError(
                "ADAPTER_UNCONFIGURED",
                "the fake adapter requires HIVE_FAKE_HARNESS to name a fixture "
                "executable; it has no default and never resolves from PATH",
            )
        argv = [
            exe,
            *self.boundary_flags(binding),
            "--model", route.model,
            "--session-id", ctx.session_id,
            ctx.kickoff,
        ]
        env = _clean_env(
            ctx.parent_env,
            frozenset({"HIVE_FAKE_HARNESS", "HIVE_FAKE_BEHAVIOUR", "HIVE_FAKE_USAGE_FILE"}),
            {},
        )
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=[exe, "--version"],
            model_requested=route.model,
            usage_extractor="fake-usage-file",
            usage_hint={"usage_file": ctx.parent_env.get("HIVE_FAKE_USAGE_FILE", "")},
            proves_model=True,
            proves_usage=True,
        )


_ADAPTERS: dict[str, Adapter] = {
    a.name: a for a in (ClaudeCodeAdapter(), CodexAdapter(), FakeAdapter())
}


def get_adapter(name: str) -> Adapter:
    """Resolve an adapter name, or fail closed.

    The refusal is the containment: an unknown harness stops here, at a typed boundary,
    rather than falling through to "run whatever string the catalog said".
    """
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise RefusalError(
            "ADAPTER_UNKNOWN",
            f"no adapter named {name!r}; known adapters: {known}. An unknown harness "
            "fails closed — it never re-enters as a shell command string",
        )
    return adapter
