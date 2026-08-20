"""The adapter contract: a route becomes an argv vector, never a shell string.

`hive-launch` used to execute one deployment string, `HIVE_WORKER_CMD`, interpolated
into a tmux command line. That is a shell-string composition boundary, and the named
risk this module answers is "an argv-safe interface hiding a shell-string escape one
layer down". So the contract is a VECTOR, end to end: the catalog's `runner` block
supplies the executable and the static arguments, the adapter appends the dynamic ones,
the CLI hands the result to the shell as NUL-separated fields, and the supervisor execs
it directly. At no point is a prompt, a path, or a model id concatenated into a string a
shell will parse.

**Adapters improve observation; they are not a harness allowlist.** `generic` runs the
operator's argv vector, appends the kickoff in its documented position, and records
route identity as `declared` and usage as `unavailable`. Any harness, wrapper, container
or VM can therefore launch from catalog configuration alone. A specialized adapter
(`claude-code`, `codex`) is earned later, when knowing the harness buys better version,
model or usage observation than `declared`/`unavailable`. What still fails closed is an
adapter NAME this build does not know: that is malformed configuration, and it never
re-enters as a shell string.

What an adapter returns, and why each piece is here:

  argv              the exact vector to exec
  env               a DELIBERATELY CONSTRUCTED environment — an allowlist, not the
                    parent's, so nothing reaches the child by accident
  version_argv      a no-model probe for the installed harness version
  model_requested   what the adapter actually put on the command line, recorded so a
                    later mismatch against the resolved model is visible
  usage_extractor   which parser can read this harness's consumption surface
  proves_model      whether this harness exposes a RESOLVED model identity at all
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field

from omegahive.harness.records import RefusalError, RouteEntry, is_hive_authority_env

# The only environment variables a worker process inherits, unless its route names more
# by name. Everything else is dropped.
#
# This is an allowlist rather than a denylist because the failure modes are asymmetric:
# a missing variable makes a harness complain loudly, while an unexpected one — a stray
# `ANTHROPIC_*` override, a proxy, a credential — changes who gets billed and what model
# answers, silently. The parent environment is never inherited wholesale.
BASE_ENV_ALLOWLIST = frozenset(
    {
        "HOME", "LANG", "LC_ALL", "LC_CTYPE", "LOGNAME", "PATH", "SHELL", "TERM",
        "TMPDIR", "TZ", "USER", "XDG_CACHE_HOME", "XDG_CONFIG_HOME", "XDG_DATA_HOME",
        "XDG_RUNTIME_DIR",
    }
)


@dataclass(frozen=True)
class LaunchContext:
    """Everything the adapter needs that is not a property of the route."""

    kickoff: str                 # the worker's first message, passed as argv, never shell
    cwd: str                     # the worker's workspace clone — where the session starts
    task_root: str               # the worker's ONE writable root: both clones + run/
    execution_id: str
    session_id: str              # pinned by the supervisor so usage is findable later
    parent_env: Mapping[str, str] = field(default_factory=dict)
    # The worker's code clone, inside the task root. Named rather than discovered,
    # because a harness that scopes file access needs it in the scope it is given.
    code_root: str = ""
    # The run-local interface directory (`<task_root>/run`) holding the issued wrappers,
    # the request spool and the receipts. Inside the task root by construction.
    run_dir: str = ""


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

    @property
    def model_identity_evidence(self) -> str:
        """The doctrine's status vocabulary for who says which model ran.

        `observed` — the harness reports a resolved model id we can read back.
        `declared` — all we have is what the catalog said and the adapter put on the
        command line. A generic runner is `declared` and says so; it is never silently
        upgraded to `observed` because the two facts have different weight.
        """
        return "observed" if self.proves_model else "declared"

    @property
    def usage_evidence(self) -> str:
        return "observed" if self.proves_usage else "unavailable"


def _clean_env(
    parent: Mapping[str, str], route: RouteEntry, additions: dict[str, str]
) -> dict[str, str]:
    """Build the child environment from the base allowlist plus the route's own names."""
    allowed = BASE_ENV_ALLOWLIST | set(route.runner.inherit_env)
    env = {k: v for k, v in parent.items() if k in allowed and not is_hive_authority_env(k)}
    for k, v in additions.items():
        if is_hive_authority_env(k):
            raise RefusalError(
                "ADAPTER_REFUSED_CREDENTIAL",
                f"adapter tried to place {k!r} in a worker environment; Hive authority "
                "credentials are never inheritable, under any runner configuration",
            )
        env[k] = v
    return env


class Adapter:
    """Base contract. Subclasses are data + one `build`; keep them dumb and testable.

    The executable and the static arguments come from the route's `runner` block, never
    from adapter code: which binary this deployment runs is the operator's fact, and an
    adapter that hardcoded one would silently outrank the catalog.
    """

    name: str = ""

    def build(  # pragma: no cover
        self, route: RouteEntry, ctx: LaunchContext
    ) -> LaunchPlan:
        raise NotImplementedError

    @staticmethod
    def version_argv_for(route: RouteEntry) -> list[str]:
        return [route.runner.executable, "--version"]

    def parse_version(self, output: str) -> str:
        """The first token that looks like a version, else the first token at all.

        `claude --version` prints `2.1.231 (Claude Code)`; taking the first token keeps
        the recorded value a version rather than a product banner.

        The digit preference is not cosmetic. The probe merges stderr so that a harness
        which fails to start can say why — which means an unrelated warning
        (`bash: warning: setlocale: ...`) can land on the first line, and the naive rule
        records that warning's first word as the harness version. Observed 2026-08-14 on
        a real preflight, which reported `harness: sh:`. A version fact naming a shell is
        worse than no fact. The shell twin is `harness_version_from` in hive-common.sh.
        """
        first = ""
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            token = line.split()[0]
            if token[:1].isdigit():
                return token
            if not first:
                first = token
        return first


class ClaudeCodeAdapter(Adapter):
    """Claude Code.

    Two dynamic flags carry the weight. `--model` takes an exact id (`claude-opus-5`),
    not only a friendly alias, so the catalog's pinned model goes on the command line
    verbatim. `--session-id` lets the supervisor PIN the session uuid, which is what
    makes usage extraction deterministic: the harness writes its own transcript to
    `<config>/projects/<cwd-slug>/<session-id>.jsonl`, and pinning the uuid means the
    supervisor knows which file to read without guessing or globbing by mtime.

    That transcript is both surfaces at once — it carries the RESOLVED model id per
    assistant message and the provider-reported token counts — so this harness observes
    model and usage with no extra model call and no paid probe.

    Everything else — the permission mode, any deny settings, a wrapper in front of the
    binary — is the route's static argument vector. It is a deployment example, not a
    launch invariant, and nothing here checks it.
    """

    name = "claude-code"

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--session-id", ctx.session_id,
            ctx.kickoff,
        ]
        env = _clean_env(ctx.parent_env, route, {})
        hint = {"session_id": ctx.session_id, "cwd": ctx.cwd}
        if "CLAUDE_CONFIG_DIR" in env:
            hint["config_dir"] = env["CLAUDE_CONFIG_DIR"]
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv_for(route),
            model_requested=route.model,
            usage_extractor="claude-code-transcript",
            usage_hint=hint,
            proves_model=True,
            proves_usage=True,
        )


# The config key a Codex permissions profile uses for its filesystem table, matched on
# the route's own static arguments. See `CodexAdapter` for why this is a merge and not
# an append.
_CODEX_FS_PREFIX = "permissions."
_CODEX_FS_SUFFIX = ".filesystem="


def _toml_inline_table(entries: dict[str, str]) -> str:
    """Serialize a flat string->string map as a TOML inline table.

    `json.dumps` is used for each scalar because a TOML basic string and a JSON string
    agree on every escape this data can contain (paths and the four access words), and
    hand-rolling quoting is how a path with a space becomes a parse error at launch.
    """
    body = ",".join(f"{json.dumps(k)}={json.dumps(v)}" for k, v in entries.items())
    return "{" + body + "}"


def _merge_codex_writable_roots(args: list[str], writable: list[str]) -> list[str]:
    """Fold the task-root write grants into the route's own Codex filesystem table.

    Why a merge rather than another argument, measured on codex-cli 0.147.0 against the
    installed binary:

      * a second `-c permissions.<p>.filesystem={...}` REPLACES the first rather than
        merging into it, so appending our roots that way would silently drop the
        operator's deny entries;
      * the legacy `sandbox_workspace_write.writable_roots` key is ignored entirely
        while a permissions profile is active, so it cannot carry them either;
      * a dotted-path override of one table entry does not parse — the `-c` splitter
        keeps the quotes, and `filesystem."/"` is rejected as a path.

    So the one place a writable root can live is inside that single table value, which
    means the adapter has to open it. This is harness-specific mechanism knowledge,
    which is exactly what a specialized adapter is for; it is not a template language,
    and no element is ever concatenated into a string a shell will parse.

    When the route names no such override — a plain `--sandbox workspace-write` runner,
    say — the roots are appended as `--add-dir`, the harness's own flag for the same
    thing, and the table is left alone.
    """
    out: list[str] = []
    merged = False
    i = 0
    while i < len(args):
        a = args[i]
        nxt = args[i + 1] if i + 1 < len(args) else None
        if (
            a in ("-c", "--config")
            and nxt is not None
            and nxt.startswith(_CODEX_FS_PREFIX)
            and _CODEX_FS_SUFFIX in nxt
        ):
            key, _, value = nxt.partition("=")
            try:
                table = tomllib.loads(f"x = {value}")["x"]
            except (tomllib.TOMLDecodeError, KeyError) as exc:
                raise RefusalError(
                    "RUNNER_ARGS_MALFORMED",
                    f"the codex route's {key!r} override is not a readable TOML table "
                    f"({exc}); the adapter must merge the task-root write grants into "
                    "it and cannot do that blind",
                ) from exc
            if not isinstance(table, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in table.items()
            ):
                raise RefusalError(
                    "RUNNER_ARGS_MALFORMED",
                    f"the codex route's {key!r} override must be a table of "
                    "path -> access strings",
                )
            entries = dict(table)
            for root in writable:
                entries[root] = "write"
            out += [a, f"{key}={_toml_inline_table(entries)}"]
            merged = True
            i += 2
            continue
        out.append(a)
        i += 1
    if not merged:
        for root in writable:
            out += ["--add-dir", root]
    return out


class CodexAdapter(Adapter):
    """OpenAI Codex CLI.

    The route's static arguments carry the operator's measured native-sandbox settings —
    the `exec` subcommand, the permission profile and its deny table, the feature flags.
    Those are deployment facts and they live in the catalog, not here. What this adapter
    adds is the part only the launch knows: the task root and both clones' `.git`
    directories, made writable because Codex marks `.git` READ-ONLY inside a
    workspace-write root by default, and a worker that can edit but not `git commit` is
    not a worker (measured 2026-08-20, boundary report gate 4).

    Usage and resolved-model surfaces are NOT established on this deployment: reading a
    Codex session rollout is unbuilt work, so an execution here records its consumption
    as `unavailable` with a named reason. That is a truthful record and the input the
    next order needs; it is not a placeholder zero, and it does not stop the route from
    launching — the doctrine's launch checks are cheap and deterministic, and "we cannot
    read this harness's token counts" is neither a safety question nor a launch gate.
    """

    name = "codex"

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        writable = [ctx.task_root]
        for clone in (ctx.cwd, ctx.code_root):
            if clone:
                writable.append(f"{clone.rstrip('/')}/.git")
        argv = [
            route.runner.executable,
            *_merge_codex_writable_roots(list(route.runner.args), writable),
            "--model", route.model,
            ctx.kickoff,
        ]
        env = _clean_env(ctx.parent_env, route, {})
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv_for(route),
            model_requested=route.model,
            usage_extractor="none",
            usage_hint={},
            proves_model=False,
            proves_usage=False,
            unproven_reason=(
                "codex usage and resolved-model surfaces are not established on this "
                "deployment; no rollout extractor is built"
            ),
        )


class GenericAdapter(Adapter):
    """Any harness the operator can name, launched from configuration alone.

    This is what makes the catalog the whole authorization: a new CLI, an operator's own
    wrapper script, a container invocation or a VM entry point becomes launchable by
    writing a `runner` block, with no code change and no descriptor. The cost of that
    freedom is stated rather than hidden — nothing here can read the harness's version
    beyond a `--version` probe, nothing can read a resolved model id, and nothing can
    read token counts. So identity is `declared` and usage is `unavailable` with a named
    reason, which is the honest record and the input that justifies writing a
    specialized adapter later.

    The kickoff is appended as the FINAL argument. That position is the contract: an
    operator whose harness wants the prompt somewhere else writes a wrapper that moves
    it, because the alternative is a placeholder syntax, and a placeholder syntax is a
    template language one escape away from a shell.
    """

    name = "generic"

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [route.runner.executable, *route.runner.args, ctx.kickoff]
        env = _clean_env(ctx.parent_env, route, {})
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv_for(route),
            model_requested=route.model,
            usage_extractor="none",
            usage_hint={},
            proves_model=False,
            proves_usage=False,
            unproven_reason=(
                "the generic adapter runs the operator's argv and exposes no model or "
                "usage surface to read; route identity is declared, not observed"
            ),
        )


class FakeAdapter(Adapter):
    """A deterministic adapter for tests and the operator drill. Never a model.

    It exists so the full path — version probe, started, supervised emits, sync,
    publication, usage extraction, finished — can be exercised end to end with no paid
    call and no network. The executable comes from the route like every other adapter's,
    so a fake route can only ever run what a catalog explicitly names.
    """

    name = "fake"

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--session-id", ctx.session_id,
            ctx.kickoff,
        ]
        env = _clean_env(
            ctx.parent_env,
            route,
            {
                "HIVE_TASK_ROOT": ctx.task_root,
                "HIVE_RUN_DIR": ctx.run_dir,
                "HIVE_CODE_ROOT": ctx.code_root,
            },
        )
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv_for(route),
            model_requested=route.model,
            usage_extractor="fake-usage-file",
            usage_hint={"usage_file": ctx.parent_env.get("HIVE_FAKE_USAGE_FILE", "")},
            proves_model=True,
            proves_usage=True,
        )


_ADAPTERS: dict[str, Adapter] = {
    a.name: a
    for a in (ClaudeCodeAdapter(), CodexAdapter(), GenericAdapter(), FakeAdapter())
}


def get_adapter(name: str) -> Adapter:
    """Resolve an adapter name, or fail closed.

    An unknown NAME is malformed configuration and refuses here, at a typed boundary. An
    unknown HARNESS is not: it launches on `generic`. The distinction is the whole
    difference between a typo and a new tool.
    """
    adapter = _ADAPTERS.get(name)
    if adapter is None:
        known = ", ".join(sorted(_ADAPTERS))
        raise RefusalError(
            "ADAPTER_UNKNOWN",
            f"no adapter named {name!r}; known adapters: {known}. A harness this build "
            "has never heard of launches on 'generic' from configuration alone; an "
            "adapter name it does not know is a typo, and a typo never re-enters as a "
            "shell command string",
        )
    return adapter
