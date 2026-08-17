# Finding: `--permission-mode auto` was silently downgraded, not redefined

**The comment at `taskbench/launch/lib.sh:70-84` is wrong about the cause.** Its
conclusion (use explicit `--allowedTools` + `acceptEdits` for headless runs) is
correct and should stay. Its stated reason — "on 2.1.233 the SAME FLAG DENIES
EDITS OUTRIGHT" — is not what happened, and will mislead the next reader.

## What actually happened

`--permission-mode auto` fell back to `default`. Claude Code records the
effective mode in each session transcript (`permissionMode`), and the probe runs
under `~/work/sess-hip1-bench-qualify-0814/.permtest/` show it directly — all on
build 2.1.233:

| probe        | asked for     | ran as        |
|--------------|---------------|---------------|
| `auto`       | `auto`        | **`default`** |
| `dontAsk`    | `dontAsk`     | `dontAsk`     |
| `acceptEdits`| `acceptEdits` | `acceptEdits` |
| `least-priv` | `acceptEdits` | `acceptEdits` |
| `auto` (re-run today, post-fix) | `auto` | **`auto`** |

Only `auto` degraded. The denials in the failing transcript are generic
default-mode text ("This command requires approval" / "you haven't granted it
yet"), not auto-mode text — auto's own denials read "denied by the Claude Code
auto mode classifier. Reason: …". The classifier never ran.

**Mechanism.** A migration in the binary deletes the user's stored auto-mode
opt-in when the "auto mode is now the default" rollout reaches the account:

```js
if (state.hasResetAutoModeOptInForDefaultOffer) return;
if (rolloutGate() !== "enabled") return;
if (userSettings?.skipAutoPermissionPrompt &&
    userSettings?.permissions?.defaultMode !== "auto") {
    write("userSettings", { skipAutoPermissionPrompt: undefined });  // opt-in cleared
}
```

Between the rollout flipping and the operator re-accepting the opt-in dialog,
`--permission-mode auto` was a no-op. Under `--print`, `default` turns every tool
call into an error.

**It was never a version regression.** That migration is present in 2.1.231,
2.1.232 and 2.1.233 alike. The trigger is a server-side rollout state that
happened to flip near the 2.1.233 upgrade. Re-running the original probe command
verbatim on 2.1.233, in the same directory, now completes the task end to end —
edit and verifier both.

**The downgrade is silent**: empty stderr, exit 0, `is_error: false`.

## Why headless should still avoid `auto`

Independent of the downgrade, `auto` is the wrong instrument for `--print`:

- Non-interactive fails closed. Classifier unreachable → "denying with retry
  guidance (fail closed)". Soft-block → interactive prompts, headless denies.
  Long transcript → "Agent aborted: auto mode classifier transcript exceeded
  context window in headless mode".
- It puts a second paid model inside the measurement — each non-fast-pathed tool
  call is a Sonnet 5 classifier request (~1.6s observed), adding latency, cost
  and non-determinism to a cell's score.

Edits inside the workspace fast-path without the classifier ("Skipping auto mode
classifier for Edit: would be allowed in acceptEdits mode"), which is why
`acceptEdits` was never affected.

## Actions

1. **Rewrite the `lib.sh:70-84` rationale** — keep the grant, replace the cause:
   the flag silently degrades to `default` when the auto-mode opt-in is not
   active, and headless auto fails closed regardless. Keep the ordering
   constraint note; it is correct and load-bearing.
2. **Assert the mode instead of trusting the flag.** The transcript's
   `permissionMode` field is ground truth and free to read. Launchers and
   preflight should refuse to start when recorded mode ≠ requested mode. This
   generalises the principle the comment already states.
3. **Immunise the operator settings** — add `"permissions": {"defaultMode":
   "auto"}` to `~/.claude/settings.json`. The migration guard only clears the
   opt-in when `defaultMode !== "auto"`, so this makes future default-offer
   migrations skip the account.
4. **Interactive launch paths keep `auto`** (`scripts/hive-common.sh:44`,
   `src/omegahive/harness/adapters.py:151`) — that is the mode auto is designed
   for, where a block becomes a clearable prompt. But they were exposed to the
   same silent downgrade, which would leave a nominally autonomous pane stalled
   on prompts — the exact failure the flag is there to prevent. Worth the same
   mode assertion at launch.
5. **Variadic-flag trap, generalised.** `--debug` takes an *optional* argument
   and will swallow a trailing prompt exactly as `--allowedTools` does
   ("Input must be provided either through stdin or as a prompt argument").
   Use `--debug-file <path>`. Rule for every launcher: no optional-or-variadic
   flag immediately before the prompt.

## Still true

The pinned incumbent record `2026-08-13-incumbent-fidelity-v0-1-2` ran before the
rollout, with auto genuinely active, so it remains valid. It is not reproducible
with `taskbench/launch/incumbent-fidelity.sh` as that script stands today —
that launcher still emits `--permission-mode auto` for headless agent and
reviewer (lines 117 and 128), as does
`taskbench/configs/incumbent-fidelity.example.yaml` (lines 21, 48).
