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

from collections.abc import Mapping
from dataclasses import dataclass, field

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


@dataclass(frozen=True)
class LaunchContext:
    """Everything the adapter needs that is not a property of the route."""

    kickoff: str                 # the worker's first message, passed as argv, never shell
    cwd: str                     # the worker's workspace clone
    execution_id: str
    session_id: str              # pinned by the supervisor so usage is findable later
    parent_env: Mapping[str, str] = field(default_factory=dict)


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
    """Base contract. Subclasses are data + one `build`; keep them dumb and testable."""

    name: str = ""
    executable: str = ""

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:  # pragma: no cover
        raise NotImplementedError

    def version_argv(self) -> list[str]:
        return [self.executable, "--version"]

    def parse_version(self, output: str) -> str:
        """First whitespace-delimited token of the first non-empty line.

        `claude --version` prints `2.1.231 (Claude Code)`; taking the first token keeps
        the recorded value a version rather than a product banner. A harness whose
        output does not fit overrides this.
        """
        for line in output.splitlines():
            line = line.strip()
            if line:
                return line.split()[0]
        return ""


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

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [
            self.executable,
            "--permission-mode", "auto",
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

    UNVERIFIED ON THIS DEPLOYMENT. The Codex binary was not installed on the host when
    this adapter was written (2026-08-13), so its argv shape is implemented from the
    documented interface and its evidence surfaces are recorded as UNKNOWN rather than
    assumed. That distinction is the point: an adapter that claimed a usage surface it
    had never read would put invented numbers on the spine, which is precisely the
    failure the `unavailable`-plus-reason path exists to prevent.

    Consequently `proves_model` and `proves_usage` are False. A Codex execution records
    its consumption as `unavailable` with a named reason until someone runs the harness
    and establishes the surface. That is a truthful record, and it is the input the next
    order needs; it is not a placeholder zero.
    """

    name = "codex"
    executable = "codex"

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [self.executable, "--model", route.model, ctx.kickoff]
        env = _clean_env(ctx.parent_env, frozenset({"CODEX_HOME"}), {})
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv(),
            model_requested=route.model,
            usage_extractor="none",
            usage_hint={},
            proves_model=False,
            proves_usage=False,
            unproven_reason=(
                "codex usage and resolved-model surfaces are not established on this "
                "deployment (harness not installed when the adapter was written)"
            ),
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

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
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
        argv = [exe, "--model", route.model, "--session-id", ctx.session_id, ctx.kickoff]
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
