# O2 — OmegaClaw fork patches (self-contained work order)

**Context (all you need):** you are working in the **OmegaClaw fork repo** (base already cut from upstream HEAD; the pre-patch **base image** is built and recorded — do not modify it; your output is the **hive image**, a new tag). These patches bind the agent to the OmegaHive board for the stage-2 spike ([omegahive_stage2_spec.md](omegahive_stage2_spec.md) §4 is the behavioral contract; this file adds the operational decisions). Patches to a live agent runtime move at review pace: small commits, each with tests, no drive-bys. **Stop-lines:** no upstream contact (pulls only, nothing outbound before the mid-July release); no changes beyond the patch list; the coordinator-side cognition (personas, KB content) is not yours.

## Runtime facts you need (verified against source; trust these)

- **Channels:** a channel is a Python adapter exposing `start_<name>()`, `getLastMessage() -> str` (read-once, destructive), `send_message(str)`. Exactly one channel is active (`commchannel` param); `initChannels`, `(receive)`, and `(send)` each contain a hardcoded if-chain over channel names — registering a new channel means editing **all three** (plus the Python module). State lives in the adapter's module globals; threads are fine (the IRC adapter runs one).
- **Skills:** a skill = a MeTTa equation `(= (skill $arg) body)` + a description line in the `getSkills` catalog text + an entry in `helper.py`'s `LLM_COMMANDS` set. Three sites; they drift — your tests must assert they agree. Do **not** add the new command to `special_two_arg_cmds`. Parser hazard: a line whose command is *not* in `LLM_COMMANDS` gets swallowed into a preceding `send`'s payload — one test must cover "board op on the line after a send".
- **Config is argv, not env:** params arrive as `param=value` startup args (the entrypoint scrubs env; only `SAFE_VARS` survive).
- **Landlock policy:** the writable set is `memory/`, `/tmp`, caches — `include_workdir: false`. Anything the binding persists goes under `memory/`.
- **Raw-reply logging:** upstream already prints `[LLM_RAW] ts=… provider=… raw=…` per call from every provider's `chat()`. Your usage patch (§4 below) sits beside it.

## Shared-code decisions (settled here; flag disagreement rather than diverging)

1. **The view renderer lives in `omegahive.port.render` — one implementation, two consumers.** The fork's adapter calls it; the vanilla rung's harness calls the same function. The fork never defines its own view format. Schema v1 (size-budgeted; the budget is a cell property passed to the renderer):

```
(board
  (tasks
    (t1 :status in_progress :owner w1 :deps () )
    (t3 :status created :ready_when 1 :deps (t1 t2) :pruned-deps ()))
  (attempts (t1 (fail fail)) (t2 (fail)))
  (rejections ((op "assign t1 w2") (code ALREADY_OWNED) (reason "t1 owned by w1")))
  (cursor 142))
```

2. **Dev dependency:** the fork imports `omegahive` (the port client) as a pinned dev dependency for local tests (path or wheel install); the **hive image** bakes it at the pinned tag per the deployment spec. Never vendor port code into the fork.
3. **Delivery rule (simplification of "semantic delta"): deliver when the cursor advanced.** Notes don't exist as board events in the spike, so every event is real; the agent seeing the board after its own accepted op is confirmation, not noise; self-excitation is bounded by the pinned small `maxNewInputLoops`. No content inspection.
4. **Skill client process model: tiny in-process client per call**, constructor-seeded from the shared state store. The subprocess-per-call precedent was for the long-haul LLM bridge (huge payloads through the embedded-Prolog boundary), not for a small emit. If in-process psycopg via the skill's `py-call` proves unstable in practice, fall back to a short-lived subprocess — record which, as a declared property.
5. **Shared client state:** one store (SQLite or JSON with atomic replace) at `memory/hive-client-state` holding **basis, cursor, generation** for this actor. The adapter (reader thread) write-throughs the view anchor on **every** poll including no-change reads; each skill-call client constructor-seeds from it. Persist nothing from a mismatch view (port spec §2).

## Patch 1 — `SAFE_VARS`: the hive DSN

Add the Postgres DSN variable (name per the omegahive compose profile) to the entrypoint's `SAFE_VARS` allow-list. Test: boot with the var set → visible in the agent process env; a non-listed var is still scrubbed.

## Patch 2 — board channel adapter (`channels/board.py` + the three dispatch-chain edits)

Behavior per stage-2 spec §4.1, with the decisions above: a polling thread (1s; the port's no-change read is O(1)) holding one long-lived port client; on cursor advance, render via `omegahive.port.render` (include this actor's `gateway.rejected` events since the last delivered view) and place the message in the read-once buffer with **replace-with-latest** semantics; `getLastMessage` returns-and-clears it; `send_message` writes a log line (no board event). Registration: the channel module + all three if-chains, with a **channel-registration consistency test** (boot with `commchannel=board` under the mock harness → receive path returns a rendered view).
**Fork-side tests (DB-free):** buffer replace-with-latest; registration chain; renderer *call* mocked.
**Binding tests (DB-required) are not yours:** the replay-vs-repeat matrix and adapter-under-real-policy tests land in the **omegahive repo** as the follow-up that consumes your hive image (they need Postgres and the gateway — machinery that repo already has). Don't build DB fixtures in the fork.

## Patch 3 — `board` skill (single-string payload)

Catalog line + equation + `LLM_COMMANDS` entry. Payload grammar (parsed in the body; unknown verb → error string result, no crash): `assign <task> <worker>` · `reassign <task> <worker>` · `escalate <task>` · `close <task>` · `reopen <task>` · `prune <task>`. One call = one emit; key derivation is the port client's job (content+basis — never derive keys in fork code); the `Accepted`/`Rejected(code, reason)` outcome is the skill's string result.
**Fork-side tests:** parser round-trip for `board "assign t1 w2"` (quoted and bare); the board-op-after-`send` swallowing case; three-site consistency (equation ↔ catalog text ↔ `LLM_COMMANDS`).

## Patch 4 — LLM-usage logging (`lib_llm_ext.py`)

Beside the existing `[LLM_RAW]` line, emit `[LLM_USAGE] ts=<iso> provider=<name> model=<id> tokens_in=<n> tokens_out=<n>` from every provider's `chat()`, reading each provider's usage metadata (fall back to `tokens_in=-1 tokens_out=-1` if a provider returns none — never crash the call path). Test per provider class with mocked responses.

## Optional rider (include only if trivial): history bounding

A config param capping the `episodes` skill's scan (line bound) — the full rotation story is pre-residency work, not spike work. Skip if it grows.

## Definition of done

Fork CI green (existing boot smoke + all new fork-side tests); the **hive image** builds (base + these patches + pinned `omegahive` port client and Postgres driver), digest recorded in the deployments record; a boot with `commchannel=board` and a reachable Postgres survives startup (the full R2 binding smoke runs omegahive-side afterwards — not this work order's gate). No edits to the base image, no upstream contact, nothing outside the patch list.
