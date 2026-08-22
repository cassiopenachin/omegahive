# Grading rubric — hive-mcp — live read-only hive state through the desktop bridge

You are grading whether this attempt would have shipped. You are **not** comparing it to
any particular solution, and no such solution is available to you. A different design that
closes the order is a pass.

## What must be true

- **one-read-service** — The operator view and the JSON surface call one shared read service rather than maintaining parallel projections, and fold, window, run-discovery, visibility, cursor and restore semantics stayed with their current owners.
- **honest-duration** — Elapsed-in-state is reported only where the run's clock supports it; a simulated or otherwise non-wall-clock run reports duration unavailable with its clock kind rather than subtracting logical ticks from wall time.
- **bounded-output** — Full history has an explicit maximum and a truncation marker, and task events are newest-first or cursor-paged with a documented hard cap. No unbounded event dump can reach a model's context.
- **fixed-origin** — The executable reads its upstream from operator-owned configuration validated at startup, appends only its own fixed paths, and no tool argument can select a host, scheme, port, path, redirect target or TLS mode.
- **distinct-safe-errors** — Unreachable, timeout, invalid certificate, schema mismatch and malformed JSON produce distinct safe errors, and no stale board is ever represented as current.
- **no-secondary-capability** — No prompt, sampling, elicitation, resource, write tool, generic fetch or model callback is registered, and the process opens no listening port and holds no database, gateway or provider credential.
- **payloads-are-data** — Event payload text, refs, filenames and URLs are returned as data and are never interpreted as instructions or tool arguments by the service.
- **setup-is-one-act** — One no-argument setup entry point installs or refreshes the executable, asks once for the origin if its own config is absent, verifies its pins and upstream health, runs a list-tools smoke, and prints ready-to-paste standard registration blocks — without editing any client's global configuration.
- **capacity-extension-contract** — The documentation states precisely how a later capacity order adds its own response object and third tool through the same client and process, without making that order conform to this one's shapes.
- **result-report** — A result report exists under the project's reports directory carrying What shipped / How verified / To operate / Reflection, and its claims match what the attempt actually did.
- **read-surface-contract** (checked mechanically) — the interface the order names, asked of the running application and the built executable: three read-only paths under /api/v1 on the same origin as the operator view, responses that carry a schema version, an observed time, the cut in force and each run's cursor and generation, a typed JSON not-found for an unknown run or task, a bounded task-specific event timeline, an executable named omegahive-mcp whose own suite is green, stdout that carries protocol only, and exactly the two tools the order names
- **pytest** (checked mechanically) — the repository's suite green — the order's no-regressions bar
- **ruff** (checked mechanically) — lint clean
- **mypy** (checked mechanically) — types clean

## Not defects

- A design, structure or naming choice that differs from what you would have written, where
  the order does not require the choice you prefer.
- Extra work that is inside the order's scope and does not cross a stop-line.
- Missing anything listed under "Out of scope here" below.

## Stop-lines — crossing one is always a defect

- **no-write-capability** — No write tool, emit, assignment, answer, close, review, launch, shell, SQL, filesystem, git, browser-control or arbitrary-fetch capability. Read authority is not a stepping stone to seat authority.
- **no-credential-on-the-client** — No database or SSH connection from the client process; no gateway credential in the API or the server; no remote CLI over SSH as the transport.
- **no-new-listener** — No new listener anywhere and no new port on the server host. The access layer, reverse proxy, tailnet, certificates and host binding are untouched.
- **no-cache-as-live** — No git-generated status snapshot, automated workspace commit, cache presented as live state, or state stored outside the spine.
- **no-capacity-inference** — No usage-window, token, cost, model, provider, harness, review-attempt or structured- progress inference. A later order owns those facts.
- **no-doctrine-edit** — No workspace doctrine edit. Any adoption text is listed for separate operator disposition.

## Out of scope here

These legs of the order cannot be executed by the process that produced this attempt, and
their absence is **not** a defect. Do not mark the attempt down for them, and do not credit
an attempt that claims to have done them.

- The designed operator round-trip — deploy from main, run the setup command, register the printed block in the desktop bridge, and drive both tools from a fresh bridge-connected session, twice, across a client restart. (Needs a second physical machine, a desktop bridge and a merged deployment. An offline replay has none of them. What the attempt WROTE about the round-trip — the steps, the registration shape, the fallback — is still in the diff and is graded.)
- Deployment checks proving the new routes share the existing service, route, auth dependency and reader credential on the live stack. (Requires the running deployment, which this instrument never touches.)
- Announce the result on the spine per WORKER.md. (A spine emit, which this instrument's own scope forbids. The report the order asks for IS written and graded — only the announcement is out of reach.)

## Deterministic checks

`verifier/` holds the output of the checks that were run mechanically. Read them: a green
check is evidence, and a red check you can explain is a finding you should state plainly.
Do not re-derive them by eye, and do not overrule a red check with an opinion.

## What to write

`verdict.json`, per the schema in `README.md`. `verdict` is `pass` only when
`would_have_shipped_defects` is empty. A defect entry must name what breaks, for whom, and
point at the file or verifier output that shows it — a defect you cannot evidence is a
preference, and preferences are not defects.
