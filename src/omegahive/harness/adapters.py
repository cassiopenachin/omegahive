"""The adapter contract: a route becomes an argv vector, never a shell string.

`hive-launch` used to execute one deployment string, `HIVE_WORKER_CMD`, interpolated
into a tmux command line. That is a shell-string composition boundary, and the named
risk this module answers is "an argv-safe interface hiding a shell-string escape one
layer down". So the contract is a VECTOR, end to end: the catalog's `runner` block
supplies the executable and the static arguments, the adapter appends the dynamic ones,
the CLI hands the result to the shell as NUL-separated fields, and the turn runner execs
it directly. At no point is a prompt, a path, or a model id concatenated into a string a
shell will parse.

**Adapters improve observation; they are not a harness allowlist.** `generic` runs the
operator's argv vector, appends the prompt in its documented position, and records route
identity as `declared` and usage as `unavailable`. Any harness, wrapper, container or VM
can therefore launch from catalog configuration alone. A specialized adapter
(`claude-code`, `codex`) is earned when knowing the harness buys better version, model,
usage, session or lifecycle observation than `declared`/`unavailable`. What still fails
closed is an adapter NAME this build does not know: that is malformed configuration, and
it never re-enters as a shell string.

**Four operations, one turn contract.** Since the `worker-turns` cutover every shipped
adapter answers the same four questions, because that is what lets one launcher run both
harnesses through one visible exit-and-resume lifecycle:

  build           an INITIAL turn's argv, from the route
  build_resume    a RESUME turn's argv, from the route plus a recorded native session id
  scan            a retained structured stream -> lifecycle, session, model, usage facts
  activity_jq     a jq program the pane uses to render the stream as it arrives

The scan's allowlists are MEASURED against the installed harness, never guessed. Where a
vendor exposes no structured signal for something — Codex has no budget field in its
JSONL on 0.147.0 — the fact is `unknown` and the raw evidence is preserved. A fabricated
budget pass would be worse than no answer, because it would look like one.

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

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from omegahive.harness.records import RefusalError, RouteEntry, is_hive_authority_env
from omegahive.harness.turns import HarnessTerminal, TurnFacts, stream_digest

# What counts as a version token in a `--version` banner: a leading digit (optionally
# after a `v`) followed by at least one dotted component. `0.147.0` and `v2.1.231` match;
# `codex-cli`, `sh:` and a bare `2026` do not.
_VERSION_SHAPE = re.compile(r"^v?\d+(\.\d+)+")

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

    kickoff: str                 # the turn's prompt, passed as argv, never shell
    cwd: str                     # the worker's workspace clone — where the turn starts
    task_root: str               # the worker's ONE writable root: both clones + run/
    execution_id: str
    session_id: str              # pinned where the harness allows it, so usage is findable
    parent_env: Mapping[str, str] = field(default_factory=dict)
    # The worker's code clone, inside the task root. Named rather than discovered,
    # because a harness that scopes file access needs it in the scope it is given.
    code_root: str = ""
    # The run-local turn directory (`<task_root>/run/turns/<n>`) holding this turn's
    # retained stream and evidence. Inside the task root by construction.
    run_dir: str = ""
    # On a resume turn, the native session id recorded by an earlier turn of THIS
    # worker. Empty on an initial turn.
    resume_session_id: str = ""


@dataclass(frozen=True)
class LaunchPlan:
    """The resolved, executable form of one turn. Pure data — no side effects."""

    argv: list[str]
    env: dict[str, str]
    version_argv: list[str]
    model_requested: str
    usage_extractor: str
    # Where the usage evidence will be found, as opaque hints for the extractor.
    usage_hint: dict[str, str]
    proves_model: bool
    proves_usage: bool
    # How the harness writes its lifecycle records on stdout. `jsonl` means one JSON
    # object per line and the turn runner may scan it; `none` means the adapter has no
    # structured surface and every turn on it is evidence-poor by construction.
    structured_format: str = "none"
    # A jq PROGRAM run with `jq -R -r` over each retained line, rendering the pane's
    # live activity. Raw-input mode is deliberate: a malformed line must render as a
    # marker, not kill the renderer that is the operator's only view of a live worker.
    activity_jq: str = "empty"
    # Whether this adapter can build a resume turn at all, and the named reason when not.
    resumable: bool = False
    resume_unsupported_reason: str | None = None
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
    """Base contract. Subclasses are data + `build`/`build_resume`/`scan`.

    The executable and the static arguments come from the route's `runner` block, never
    from adapter code: which binary this deployment runs is the operator's fact, and an
    adapter that hardcoded one would silently outrank the catalog.
    """

    name: str = ""

    def build(  # pragma: no cover
        self, route: RouteEntry, ctx: LaunchContext
    ) -> LaunchPlan:
        raise NotImplementedError

    def build_resume(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        """A resume turn's argv, from the route plus a recorded native session id.

        The default refuses by name. An adapter with no native resume command must not
        fall back to a fresh session wearing the old one's turn number: that would look
        like continuity and be a new context, which is the worst of both.
        """
        raise RefusalError(
            "RESUME_UNSUPPORTED",
            f"the {self.name!r} adapter has no measured native resume command on this "
            "deployment, so this turn's session cannot be woken. Its evidence is "
            "retained; recovery is a fresh task launch, not a silent new session",
        )

    def scan(self, records: Sequence[Mapping[str, Any]], *, raw: str) -> TurnFacts:
        """Normalize a retained structured stream. Never raises, never repairs.

        An adapter with no structured surface returns `missing` with a named reason. The
        one thing no adapter may do is read the assistant's final prose: a lifecycle
        fact derived from model output is not an observation.
        """
        _ = records
        return TurnFacts(
            terminal=HarnessTerminal(
                kind="missing",
                reason="unknown",
                detail=f"the {self.name!r} adapter exposes no structured stream to scan",
            ),
            digest=stream_digest(raw.encode("utf-8")),
            unavailable_reason=(
                f"the {self.name!r} adapter exposes no structured lifecycle surface on "
                "this deployment"
            ),
        )

    @staticmethod
    def version_argv_for(route: RouteEntry) -> list[str]:
        return [route.runner.executable, "--version"]

    def parse_version(self, output: str) -> str:
        """The first token on any line that LOOKS like a version, else the first token.

        Two real banners, two shapes: `claude --version` prints `2.1.238 (Claude Code)`
        and `codex --version` prints `codex-cli 0.147.0`. Taking the first token of the
        first line records `2.1.238` for one and the product name `codex-cli` for the
        other — a harness_version fact naming a product is a false fact on a durable log,
        so the scan looks at every token and takes the first that parses as a version.

        The preference is not cosmetic. The probe merges stderr so that a harness which
        fails to start can say why — which means an unrelated warning
        (`bash: warning: setlocale: ...`) can land on the first line, and "first token of
        the first non-empty line" then records that warning's first word as the harness
        version. Observed 2026-08-14 on a real preflight, which reported `harness: sh:`.
        The shell twin is `harness_version_from` in hive-common.sh; the two are asserted
        to agree in `tests/test_hive_common.py`.
        """
        first = ""
        for raw in output.splitlines():
            line = raw.strip()
            if not line:
                continue
            for token in line.split():
                if _VERSION_SHAPE.match(token):
                    return token
            if not first:
                first = line.split()[0]
        return first


# --- Claude Code -----------------------------------------------------------------------
#
# Everything below is MEASURED against claude 2.1.238 on this deployment (probe,
# 2026-08-21). The two enums are read out of the installed build's own terminal
# vocabulary rather than assumed, because the classifier's `budget` row is only allowed
# to fire on an explicit structured signal, and "explicit" has to mean something.

# `result.terminal_reason` values that mean the process ran out of the budget it was
# given. On 2.1.238 that is exactly one value: `budget_exhausted`, which the harness
# pairs with `subtype: "error_max_budget_usd"`. `blocking_limit` and
# `rapid_refill_breaker` are deliberately NOT here — the installed build maps both to a
# CONTEXT limit, not a spend limit, and folding them into `budget` would tell an operator
# to wait for a window that was never the problem.
CLAUDE_BUDGET_TERMINAL_REASONS = frozenset({"budget_exhausted"})

# Terminal reasons that mean the process ended cleanly. Everything else that appears in
# a terminal record is an error; an unrecognized value stays verbatim on the fact and is
# classified as an error rather than silently accepted as success.
CLAUDE_CLEAN_TERMINAL_REASONS = frozenset({"completed"})


class ClaudeCodeAdapter(Adapter):
    """Claude Code, run through its batch/resume interface — never as an interactive TUI.

    Three dynamic flags carry the turn. `--model` takes an exact id (`claude-opus-5`), so
    the catalog's pinned model goes on the command line verbatim. `-p --output-format
    stream-json --verbose` is the documented non-interactive structured surface, and
    `--verbose` is required by the harness alongside the other two. And the session
    identity: an INITIAL turn pins `--session-id <uuid>`, so the transcript's location is
    known without globbing by mtime; a RESUME turn passes `--resume <id>`, which the
    installed build keeps on the SAME session id (measured 2026-08-21) — the durable
    identity survives the process, which is the whole premise of a turn.

    That structured stream is several surfaces at once: `system/init` carries the
    resolved model and the harness's own version, `result` carries the terminal reason
    and the provider-reported token counts, and `rate_limit_event` carries the
    subscription window's status. So this harness observes model, usage and lifecycle
    with no extra model call and no paid probe.

    Everything else — the permission mode, any deny settings, a wrapper in front of the
    binary — is the route's static argument vector. It is a deployment fact, not a launch
    invariant, and nothing here checks it.
    """

    name = "claude-code"

    # Rendered per line by `jq -R -r`. Quiet by design: a pane that prints every
    # thinking-token delta is as unreadable as one that prints nothing.
    ACTIVITY_JQ = r"""
      (try fromjson catch null) as $r
      | if $r == null then "  ! unparsed harness line (\(length) bytes)"
        elif ($r.type == "system" and $r.subtype == "init") then
          "  · session \($r.session_id // "?")  model \($r.model // "?")"
          + "  claude \($r.claude_code_version // "?")"
        elif $r.type == "assistant" then
          ([$r.message.content[]? | select(.type == "tool_use") | .name] | unique) as $t
          | if ($t | length) > 0 then "  · tool: \($t | join(", "))"
            else ([$r.message.content[]? | select(.type == "text") | .text]
                  | join(" ") | gsub("\\s+"; " ")) as $x
                 | if ($x | length) > 0 then "  · \($x[0:160])" else empty end
            end
        elif $r.type == "rate_limit_event" then
          (if ($r.rate_limit_info.status // "allowed") == "allowed" then empty
           else "  ! rate limit \($r.rate_limit_info.status)"
                + " (\($r.rate_limit_info.rateLimitType // "?"))" end)
        elif $r.type == "result" then
          "  · terminal: \($r.terminal_reason // $r.subtype // "?")  error=\($r.is_error)"
        else empty end
    """

    def _common(self, route: RouteEntry, ctx: LaunchContext, argv: list[str]) -> LaunchPlan:
        env = _clean_env(ctx.parent_env, route, {})
        hint = {"session_id": ctx.resume_session_id or ctx.session_id, "cwd": ctx.cwd}
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
            structured_format="jsonl",
            activity_jq=self.ACTIVITY_JQ,
            resumable=True,
        )

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--session-id", ctx.session_id,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def build_resume(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        if not ctx.resume_session_id:
            raise RefusalError(
                "RESUME_SESSION_MISSING",
                "a claude-code resume turn needs the native session id recorded by an "
                "earlier turn; none was supplied",
            )
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--resume", ctx.resume_session_id,
            "-p",
            "--output-format", "stream-json",
            "--verbose",
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def scan(self, records: Sequence[Mapping[str, Any]], *, raw: str) -> TurnFacts:
        session_id: str | None = None
        model: str | None = None
        version: str | None = None
        usage: dict[str, Any] | None = None
        terminal_record: Mapping[str, Any] | None = None
        rate_limit_rejected = False
        notes: list[str] = []

        for rec in records:
            rtype = rec.get("type")
            if isinstance(rec.get("session_id"), str) and not session_id:
                session_id = rec["session_id"]
            if rtype == "system" and rec.get("subtype") == "init":
                if isinstance(rec.get("model"), str):
                    model = rec["model"]
                if isinstance(rec.get("claude_code_version"), str):
                    version = rec["claude_code_version"]
            elif rtype == "assistant" and model is None:
                msg = rec.get("message")
                if isinstance(msg, Mapping) and isinstance(msg.get("model"), str):
                    model = msg["model"]
            elif rtype == "rate_limit_event":
                info = rec.get("rate_limit_info")
                if isinstance(info, Mapping) and info.get("status") == "rejected":
                    # A measured signal, not a guess: the installed build sets `rejected`
                    # exactly when a five-hour, seven-day or overage limit has actually
                    # blocked the request (2.1.238's own scenario table).
                    rate_limit_rejected = True
                    notes.append(
                        f"rate limit rejected ({info.get('rateLimitType') or 'unknown window'})"
                    )
            elif rtype == "result":
                terminal_record = rec

        if terminal_record is not None:
            if isinstance(terminal_record.get("usage"), Mapping):
                usage = dict(terminal_record["usage"])
            if isinstance(terminal_record.get("session_id"), str):
                session_id = terminal_record["session_id"]
            terminal = self._terminal_of(terminal_record, rate_limit_rejected)
        elif rate_limit_rejected:
            # No terminal record at all AND a measured rejection: the window stopped the
            # process, which is the one case where the absence itself is evidence.
            terminal = HarnessTerminal(
                kind="budget",
                reason="rate_limit_rejected",
                detail="no terminal result record; the subscription window rejected the request",
            )
        else:
            terminal = HarnessTerminal(
                kind="missing",
                reason="unknown",
                detail="the stream carries no `result` record",
            )

        return TurnFacts(
            terminal=terminal,
            session_id=session_id,
            model_resolved=model,
            harness_version=version,
            usage=usage,
            records=len(records),
            digest=stream_digest(raw.encode("utf-8")),
            notes=notes,
        )

    @staticmethod
    def _terminal_of(rec: Mapping[str, Any], rate_limit_rejected: bool) -> HarnessTerminal:
        reason = rec.get("terminal_reason")
        reason = reason if isinstance(reason, str) and reason else None
        subtype = rec.get("subtype") if isinstance(rec.get("subtype"), str) else None
        is_error = bool(rec.get("is_error"))
        if reason in CLAUDE_BUDGET_TERMINAL_REASONS:
            return HarnessTerminal(
                kind="budget", reason=reason or "budget_exhausted", detail=subtype
            )
        if is_error and rate_limit_rejected:
            return HarnessTerminal(
                kind="budget",
                reason="rate_limit_rejected",
                detail=f"terminal_reason={reason or subtype or 'unknown'}",
            )
        if is_error or (subtype is not None and subtype != "success"):
            return HarnessTerminal(
                kind="error", reason=reason or subtype or "unknown", detail=subtype
            )
        if reason is not None and reason not in CLAUDE_CLEAN_TERMINAL_REASONS:
            # A clean-looking result carrying a terminal reason this build has never
            # measured. It is not called a success and it is not called a budget; it is
            # recorded verbatim as an error so that a human reads it.
            return HarnessTerminal(kind="error", reason=reason, detail=subtype)
        return HarnessTerminal(kind="completed", reason=reason or "completed", detail=subtype)


# --- Codex -----------------------------------------------------------------------------
#
# Measured against codex-cli 0.147.0 on this deployment (probe, 2026-08-21).

# The subcommand a codex route's static arguments must begin with. `codex exec` is the
# non-interactive interface and `codex exec resume <id>` is its resume form, so the
# adapter has to know where in the vector to insert `resume`.
_CODEX_EXEC = "exec"

# Options `codex exec` accepts that `codex exec resume` does NOT (measured: the two
# `--help` outputs on 0.147.0). A route carrying one of these can be launched and cannot
# be resumed, so the adapter refuses the resume BY NAME instead of dropping the option —
# silently dropping `-s` would resume the worker under a different sandbox than the
# operator configured, which is exactly the class of change a hive must never make on an
# operator's behalf.
CODEX_RESUME_UNSUPPORTED_OPTS = {
    "-s": '-c sandbox_mode="<mode>"',
    "--sandbox": '-c sandbox_mode="<mode>"',
    "-C": "the turn already runs in the worker's workspace clone; drop it",
    "--cd": "the turn already runs in the worker's workspace clone; drop it",
    "--add-dir": "the route's own -c filesystem/permissions table",
    "--approve-for-me": "no resume equivalent on 0.147.0",
    "-p": "no resume equivalent on 0.147.0",
    "--profile": "no resume equivalent on 0.147.0",
    "--oss": "no resume equivalent on 0.147.0",
    "--local-provider": "no resume equivalent on 0.147.0",
    "--color": "no resume equivalent on 0.147.0",
}


class CodexAdapter(Adapter):
    """OpenAI Codex CLI, run through `codex exec --json` and `codex exec resume`.

    The route's static arguments carry the operator's own runner settings — the `exec`
    subcommand and whatever configuration they chose. Those are deployment facts and they
    live in the catalog. What this adapter adds is the part only the launch knows: the
    structured-output flag, the pinned model, the prompt, and — on a resume — the
    `resume <thread-id>` pair inserted directly after `exec`.

    What it deliberately no longer adds: writable-root grants. Before the `worker-turns`
    cutover this adapter merged the task root and both clones' `.git` directories into
    the route's own Codex filesystem table, widening a sandbox that Hive itself had
    specified. Under the accepted runner doctrine the runner's reach is the operator's to
    configure and Hive's to record; a launcher that quietly widens a sandbox is deciding
    the deployment's posture from inside itself. If a configured runner cannot commit,
    the worker records and blocks honestly, and the operator changes the route.

    Codex's structured stream establishes the durable thread id (`thread.started`), the
    turn's token usage (`turn.completed.usage`) and an explicit failure (`turn.failed`).
    It establishes NO resolved model id and NO budget signal on 0.147.0 — both are
    recorded as unavailable with a named reason rather than guessed.
    """

    name = "codex"

    ACTIVITY_JQ = r"""
      (try fromjson catch null) as $r
      | if $r == null then "  ! unparsed harness line (\(length) bytes)"
        elif $r.type == "thread.started" then "  · thread \($r.thread_id // "?")"
        elif $r.type == "turn.started" then "  · turn started"
        elif $r.type == "item.completed" then
          (($r.item.type // "item") as $k
           | if $k == "agent_message"
             then "  · \((($r.item.text // "") | gsub("\\s+"; " "))[0:160])"
             elif $k == "command_execution" then "  · exec: \((($r.item.command // ""))[0:120])"
             elif $k == "error" then "  ! \($r.item.message // "error")"
             else "  · \($k)" end)
        elif $r.type == "turn.completed" then
          "  · turn completed  in=\($r.usage.input_tokens // 0) out=\($r.usage.output_tokens // 0)"
        elif $r.type == "turn.failed" then "  ! turn failed: \((($r.error.message // "?"))[0:200])"
        elif $r.type == "error" then "  ! \((($r.message // "?"))[0:200])"
        else empty end
    """

    def _common(self, route: RouteEntry, ctx: LaunchContext, argv: list[str]) -> LaunchPlan:
        env = _clean_env(ctx.parent_env, route, {})
        unsupported = _codex_resume_blockers(route.runner.args)
        return LaunchPlan(
            argv=argv,
            env=env,
            version_argv=self.version_argv_for(route),
            model_requested=route.model,
            usage_extractor="codex-turn-stream",
            usage_hint={},
            proves_model=False,
            proves_usage=True,
            structured_format="jsonl",
            activity_jq=self.ACTIVITY_JQ,
            resumable=not unsupported,
            resume_unsupported_reason=(
                _codex_resume_refusal(unsupported) if unsupported else None
            ),
            unproven_reason=(
                "codex 0.147.0 reports no resolved model id in its structured stream; "
                "route identity on this harness is declared, not observed"
            ),
        )

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        args = _codex_args(route)
        argv = [
            route.runner.executable,
            *args,
            "--json",
            "--model", route.model,
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def build_resume(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        if not ctx.resume_session_id:
            raise RefusalError(
                "RESUME_SESSION_MISSING",
                "a codex resume turn needs the native thread id recorded by an earlier "
                "turn; none was supplied",
            )
        args = _codex_args(route)
        blockers = _codex_resume_blockers(args)
        if blockers:
            raise RefusalError("RESUME_ARGS_UNSUPPORTED", _codex_resume_refusal(blockers))
        # `exec` is index 0 by construction (`_codex_args` refuses otherwise), and
        # `resume <id>` goes immediately after it: `codex exec resume <id> [options]`.
        argv = [
            route.runner.executable,
            args[0],
            "resume",
            ctx.resume_session_id,
            *args[1:],
            "--json",
            "--model", route.model,
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def scan(self, records: Sequence[Mapping[str, Any]], *, raw: str) -> TurnFacts:
        thread_id: str | None = None
        usage: dict[str, Any] | None = None
        completed = False
        failed_detail: str | None = None
        error_detail: str | None = None

        for rec in records:
            rtype = rec.get("type")
            if rtype == "thread.started" and isinstance(rec.get("thread_id"), str):
                thread_id = rec["thread_id"]
            elif rtype == "turn.completed":
                completed = True
                if isinstance(rec.get("usage"), Mapping):
                    usage = dict(rec["usage"])
            elif rtype == "turn.failed":
                err = rec.get("error")
                failed_detail = (
                    str(err.get("message")) if isinstance(err, Mapping) else str(err)
                )
            elif rtype == "error":
                error_detail = str(rec.get("message"))

        if failed_detail is not None:
            terminal = HarnessTerminal(kind="error", reason="turn.failed", detail=failed_detail)
        elif completed:
            terminal = HarnessTerminal(kind="completed", reason="turn.completed")
        elif error_detail is not None:
            terminal = HarnessTerminal(kind="error", reason="error_event", detail=error_detail)
        else:
            terminal = HarnessTerminal(
                kind="missing",
                reason="unknown",
                detail="the stream carries neither `turn.completed` nor `turn.failed`",
            )
        return TurnFacts(
            terminal=terminal,
            session_id=thread_id,
            model_resolved=None,
            harness_version=None,
            usage=usage,
            records=len(records),
            digest=stream_digest(raw.encode("utf-8")),
            unavailable_reason=(
                None if usage else "no `turn.completed` record carried a usage block"
            ),
            notes=[
                "codex 0.147.0 exposes no structured budget signal; a usage-limit exit "
                "on this harness is recorded as unknown, never as a budget pass"
            ],
        )


def _codex_args(route: RouteEntry) -> list[str]:
    """The route's static arguments, checked to start with the `exec` subcommand.

    Refused rather than repaired: `codex` with no subcommand starts the interactive TUI,
    and a turn that silently became an interactive session would hang a pane forever with
    no structured output and no way to classify its exit.
    """
    args = list(route.runner.args)
    if not args or args[0] != _CODEX_EXEC:
        raise RefusalError(
            "RUNNER_ARGS_MALFORMED",
            f"a codex route's runner args must begin with {_CODEX_EXEC!r} — the "
            "non-interactive subcommand this adapter builds turns on. Got: "
            f"{args[:1] or ['<empty>']}",
        )
    return args


def _codex_resume_blockers(args: Sequence[str]) -> list[str]:
    """Which of the route's own options `codex exec resume` will not accept."""
    found: list[str] = []
    for a in args:
        name = a.split("=", 1)[0]
        if name in CODEX_RESUME_UNSUPPORTED_OPTS and name not in found:
            found.append(name)
    return found


def _codex_resume_refusal(blockers: Sequence[str]) -> str:
    parts = ", ".join(f"{b} (use {CODEX_RESUME_UNSUPPORTED_OPTS[b]})" for b in blockers)
    return (
        "this codex route carries options that `codex exec resume` does not accept on "
        f"0.147.0: {parts}. The turn can be launched and cannot be resumed. Rewrite the "
        "route's runner args using the equivalents named above; Hive will not drop an "
        "operator's option to make a resume work, because that would wake the worker "
        "under a configuration the operator did not choose"
    )


class GenericAdapter(Adapter):
    """Any harness the operator can name, launched from configuration alone.

    This is what makes the catalog the whole authorization: a new CLI, an operator's own
    wrapper script, a container invocation or a VM entry point becomes launchable by
    writing a `runner` block, with no code change and no descriptor. The cost of that
    freedom is stated rather than hidden — nothing here can read the harness's version
    beyond a `--version` probe, nothing can read a resolved model id, nothing can read
    token counts, and nothing can wake a previous session. So identity is `declared`,
    usage is `unavailable`, resume refuses by name, and any turn on this adapter that
    leaves no worker terminal event on the spine is `unclassified` — which is the honest
    record and the input that justifies writing a specialized adapter later.

    The prompt is appended as the FINAL argument. That position is the contract: an
    operator whose harness wants it somewhere else writes a wrapper that moves it,
    because the alternative is a placeholder syntax, and a placeholder syntax is a
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
            structured_format="none",
            activity_jq="empty",
            resumable=False,
            resume_unsupported_reason=(
                "the generic adapter knows no native resume command for this harness; a "
                "turn on it cannot be woken, and must not be replaced by a fresh session "
                "wearing the old one's turn number"
            ),
            unproven_reason=(
                "the generic adapter runs the operator's argv and exposes no model, "
                "usage or lifecycle surface to read; route identity is declared, not "
                "observed, and every exit on it is evidence-poor by construction"
            ),
        )


class FakeAdapter(Adapter):
    """A deterministic adapter for tests and the operator drill. Never a model.

    It exists so the full path — version probe, started, turn stream, session capture,
    resume, classification, finished — can be exercised end to end with no paid call and
    no network. Its stream is the simplest thing that is still a real structured surface:
    a `session` record and a `result` record, so the drill exercises the same scan, the
    same classifier and the same summary the shipped adapters do rather than a second
    code path that could pass while they fail. The executable comes from the route like
    every other adapter's, so a fake route can only ever run what a catalog names.
    """

    name = "fake"

    ACTIVITY_JQ = r"""
      (try fromjson catch null) as $r
      | if $r == null then "  ! unparsed harness line (\(length) bytes)"
        elif $r.type == "session" then "  · session \($r.session_id // "?")"
        elif $r.type == "result" then "  · terminal: \($r.status // "?")"
        else "  · \($r.type // "record")" end
    """

    def _common(self, route: RouteEntry, ctx: LaunchContext, argv: list[str]) -> LaunchPlan:
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
            structured_format="jsonl",
            activity_jq=self.ACTIVITY_JQ,
            resumable=True,
        )

    def build(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--session-id", ctx.session_id,
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def build_resume(self, route: RouteEntry, ctx: LaunchContext) -> LaunchPlan:
        if not ctx.resume_session_id:
            raise RefusalError(
                "RESUME_SESSION_MISSING",
                "a fake resume turn needs the session id recorded by an earlier turn",
            )
        argv = [
            route.runner.executable,
            *route.runner.args,
            "--model", route.model,
            "--resume", ctx.resume_session_id,
            ctx.kickoff,
        ]
        return self._common(route, ctx, argv)

    def scan(self, records: Sequence[Mapping[str, Any]], *, raw: str) -> TurnFacts:
        session_id: str | None = None
        status: str | None = None
        usage: dict[str, Any] | None = None
        for rec in records:
            if rec.get("type") == "session" and isinstance(rec.get("session_id"), str):
                session_id = rec["session_id"]
            elif rec.get("type") == "result":
                status = rec.get("status") if isinstance(rec.get("status"), str) else None
                if isinstance(rec.get("usage"), Mapping):
                    usage = dict(rec["usage"])
        if status == "completed":
            terminal = HarnessTerminal(kind="completed", reason="completed")
        elif status == "budget":
            terminal = HarnessTerminal(kind="budget", reason="fake_budget_exhausted")
        elif status is not None:
            terminal = HarnessTerminal(kind="error", reason=status)
        else:
            terminal = HarnessTerminal(
                kind="missing", reason="unknown", detail="no `result` record in the stream"
            )
        return TurnFacts(
            terminal=terminal,
            session_id=session_id,
            model_resolved=None,
            harness_version=None,
            usage=usage,
            records=len(records),
            digest=stream_digest(raw.encode("utf-8")),
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
