# OmegaHive — Human UI Spec

**Status:** v0.1 DRAFT — for discussion. Scope: the operator-facing web UI for a single OmegaHive deployment (rootless podman compose on one Fedora host, tailnet-only access per [omegahive_remote_access_spec.md](omegahive_remote_access_spec.md); 1–2 operators, one traveling with a phone for ~5 weeks). Synthesized from a three-lens ideation (operator ergonomics / architecture purity / pragmatic build); this document is the single source. Companions: [omegahive_port_spec.md](omegahive_port_spec.md) (the read/write surface this consumes), [omegahive_design_1_1.md](omegahive_design_1_1.md) (§3.2 projections, §3.4 port binding, §3.5 check-ins/steering, §10.3 run mapping — an input of §9 below), [omegahive_remote_access_spec.md](omegahive_remote_access_spec.md) (tailnet transport, port 8443).

---

## 1. What it is

The UI is a **projection consumer** first and a **governed write surface** later. It reads the one log of record through the coordinator port and renders four things every operator needs — the board, the event stream, metrics, and record detail — as plain server-rendered pages over the tailnet. It holds **no state that is not an event**: kill it, replay the log, and it renders identically. It never grades, never stores, never invents; it shows exactly what the log supports. In a later revision it gains a write path (acks, steering notes, permitted board ops) built as first-class human actors through the same gateway that governs every agent — no admin side-door, no INSERT credential in the UI process. Until then, the push-and-decide surface is Telegram; the web UI is a read-only companion the operator opens when a notification isn't enough. The goal is a thing that feels **alive** — "a hive I'm watching think," not a dashboard I remember to check — while staying honest enough that its every rendered fact and every future action is one auditable log event.

---

## 2. Reading architecture

**Read through the port, as a read-only actor.** The UI does not query Postgres directly (a third fold site the port spec forbids — §2 read contract) and does not stand up a second read API service. It constructs a `HiveCoordinatorPort` and calls `read(cursor)`. It inherits, identically to agents, run-scoped cursors, one-snapshot anchoring (board + events at one log point S), the generation token, and role-scoped visibility filtering. The UI adds **views, not facts** — every screen is a fold over events that already exist; it introduces zero new event types on the read path.

**Role and credential — v0.1 vs later (adjudicated).** For **v0.1**, the UI process constructs the port as a **coordinator-role actor** and holds the single application DB role that the port milestone ships with. This is the lower-friction path and needs nothing that does not already exist. A dedicated read-only role is the correct end state but is deliberately **deferred to the write-path revision** and folded into the port spec's existing two-role trigger (`hive_gateway` / `hive_reader`), which fires "when an external agent first holds credentials." When the UI gains its own process identity and the write path lands together, the UI reads under **`hive_reader`** (read-only DB role, structurally incapable of INSERT) and writes only through the gateway (which alone holds `hive_gateway`). We do **not** accelerate the two-role scheme for a read-only v0.1 — the acceleration lands with the writes it exists to fence. (See §9 for the owner decision if a read-only credential is wanted sooner as defense-in-depth.)

**SSE over poll, riding the no-change short-circuit.** The live surface is server-sent events, not websockets: the update channel is one-directional (server → browser), which is SSE's exact shape; future writes are ordinary POSTs, not socket frames. The SSE endpoint is a server-side poll loop holding a cursor:

1. `read(cursor)` → the port's O(1) `max(seq)` no-change short-circuit returns an empty view without folding when the board is quiet. **The dashboard IS the fast-polling-reader case the short-circuit was designed for** — a 1–2s tick on N mostly-quiet projects costs near nothing.
2. If events arrived: yield the updated board fragment and the new event rows (as out-of-band swaps), advance the cursor.
3. Repeat.

Push (LISTEN/NOTIFY) is **not** built for the UI. The port spec's armed NOTIFY trigger fires on *agent wake latency*, not on UI wants — the UI is not a valid reason to fire it. If NOTIFY lands anyway for agents, the UI may subscribe to it as a **loss-tolerant hint only**; the cursor remains the sole correctness carrier.

**Generation handling.** Every `PortView` carries a generation token. On `GENERATION_MISMATCH` (a restore-from-dump bumped the generation), the UI shows a banner, **drops its cursor, takes a full snapshot read** (which adopts the new generation), and re-renders from scratch. It **persists nothing** from a mismatch view. This is the same client obligation the port spec pins for every client (§2); the UI is not special.

---

## 3. Screens (MVP cut)

Four screens, each a thin adapter over an existing fold — no new SQL, no charting library, no client-side folding. Every screen is reachable, and the URL carries the run from day one (§6).

| # | Screen | Route | Shows | Reads via |
|---|--------|-------|-------|-----------|
| 1 | **Board** | `/run/{run}/board` | The current task graph — objectives, status, owner, priority, blockers — through the existing board renderer wrapped to HTML. Rejected/blocked states styled distinctly. | `PortView.board` (server's authoritative fold; never a client fragment) |
| 2 | **Events** | `/run/{run}/events` | The event stream, reverse-chronological, with filter chips by actor/agent and type. `gateway.rejected` events styled distinctly so refusals are visible as they land. | `PortView.events` |
| 3 | **Metrics** | `/run/{run}/metrics` | The metric folds available at the deployed stage, as a plain table. **No sparklines, no charts** in v0.1 (a flagged rabbit hole). | existing metric fold functions |

**Deliberately out of v0.1:** interactive diff viewers, math rendering in markdown, semantic/full-text search over transcripts, any charting. Each is named a rabbit hole and deferred.

### 3.2 Data-boundary deferrals (implementation alignment, Jul 9)

The first read-only deployment renders only facts that the current port and projection expose.
Three specified features are deliberately deferred rather than reconstructed in the UI:

1. **Project filtering and a project index** wait for the run-mapping decision to add an addressable project attribute to events. The Events screen ships with actor and type filters only.
2. **Stage-4 metrics** — loop coefficient, output consumption, abandonment gap, and memory reuse — appear only when their deterministic projections and feedstock exist. The initial Metrics screen renders the current core metric set without placeholder values.
3. **Generic record detail** waits for an artifact-reference resolver with explicit allowed roots and checksum semantics. The UI must never interpret an event reference as an arbitrary filesystem path.

**The self-narrating ticker ships in v0.1 (spicy-idea adjudication — see §3.1).** It is a live region on the Board and Events screens.

### 3.1 The self-narrating ticker (v0.1)

A deterministic `event → sentence` function (~20 lines) turns each new event into one plain line: "w1 claimed t3", "coordinator escalated t7 — no legal op", "✗ w2 refused t3: ALREADY_OWNED". Rendered as a live "hive activity" feed off the same SSE stream that drives the board. It costs almost nothing, surfaces rejections the instant they occur, makes idle-vs-busy legible at a glance, works identically on a phone, and degrades to a static list when the stream drops. This is the single feature that makes the thing feel alive, so it is in v0.1. (The other two spicy ideas — time-travel board scrub and catch-up replay — are adjudicated to *later* in §9; both are cheap re-folds at a cursor, but neither is load-bearing for a first-useful UI.)

---

## 4. Write path (design now, build later)

The write path is **designed here and built in a later revision**, not in v0.1. v0.1 web is read-only. This section fixes the design so v0.1's seams (§6) are cut for it.

**Humans are first-class actors, per-person.** A human who writes is an actor with a role and capability grants — **not** an admin side-channel, and **not** a shared "operator" actor (a shared actor collapses attribution and risks key collisions; the audit must answer *who* acked). Each operator is a distinct actor id.

**The operations, and their events.** Permitted human ops are: **escalation acks**, **steering notes** attached to a task (design §3.5 — delivered to the worker at its next check-in), and the **subset of board ops the human is granted**. Every op is one auditable event. An ack is an **event referencing the escalation's event id** — "resolved" is a *fold over that reference*, never a mutable flag. There are no new event *types* invented for the UI beyond what the write path already defines; the UI emits the same ops agents do, attributed to a human actor.

**The ack button and the stage-4 outbound gate are the same mechanism — stated plainly.** Design §3.3 Tier 1.5 is "the acting capability is held by the gateway until a review/acknowledgment event exists." The UI's ack button *is* that acknowledgment event. When a human taps "approve," they emit the ack event that releases the gateway-held capability. The UI ack control and the stage-4 outbound gate are not two features that resemble each other; they are one mechanism with a button on one end. **The write path therefore lands with — and no earlier than — stage-4's Tier-1.5 / ack machinery.** Building the UI ack button before the gateway holds capability behind an ack event would be building a button wired to nothing.

**Idempotency from the view's basis (§3a).** A write is keyed by the port's content+basis rule, with `basis_seq` derived from the **anchoring basis of the exact view the human acted on**. Consequence: a double-tap on the same rendered view produces the same key → one event (double-tap is safe). An *intended* repeat necessarily follows a fresh read → `basis_seq` moved → new key → executes. The key is derived server-side at POST-parse time from the canonicalized op plus the basis the form carries — never from raw text. **Ack-loss recovery is the honest spinner** (port spec §8 test 4): the client shows a pending state, the same-key retry returns `Accepted` with the original event id, and the receipt confirms. Every write returns a **receipt** — silent success is a governance hazard that trains double-taps; idempotency makes the double-tap safe, the receipt makes it unnecessary.

**Generation carried into every write.** The UI carries the anchoring cursor/generation into each POST. On `GENERATION_MISMATCH` it drops the cursor, full-re-reads, and re-renders **before letting the action land** — no write is emitted from a stale view.

**Friction proportional to irreversibility.** Steering, hold, defer = one tap, reply-like. Outbound or money-touching acks = a confirm step that restates *what will happen* and its cost. The "approve" affordance is visually distinct from "seen it, defer" — a triaging operator must never accidentally authorize.

**Never writable** (structural, not policy): anything outside the human's granted op union; no direct INSERT (enforced at the credential level once `hive_reader` lands); no board mutation outside granted ops; no projection edits; no policy/tier edits from the dashboard; no timestamps (server-set); no secrets.

---

## 5. Tech stack

Named, boring, no build step, one compose service.

- **FastAPI + uvicorn** — async, and SSE is native; the port's wire types are already pydantic, so the API models are free.
- **Jinja2** — server-side templates.
- **A tiny native EventSource fragment swapper** — local, under 20 lines, and it swaps server-rendered HTML only; it never folds log data in the browser. This replaces htmx/SSE for the read-only MVP without changing the later POST seam.
- **One local desktop-first stylesheet** — no build step and no external asset dependency. It replaces Pico.css because the board needs a deliberately designed dense desktop layout rather than a classless document skin.
- **Markdown and code rendering** are deferred with generic record detail (§3.2); select a pure-Python renderer and Pygments when that artifact contract lands.

**Rejected, with reasons:** an SPA (rebuilds fold logic client-side or forces a versioned REST API — the port spec forbids client-side incremental folding, so an SPA fights the grain); static generation (live updates are required); a browser TUI (phone-hostile, dead-ends the write path). No charting library in v0.1.

**Compose + binding.** One compose service in the existing rootless-podman profile, built from the same repo image family. It binds to the tailnet interface on **port 8443** (the placeholder already reserved in the remote-access ACL) — reverse-proxied or port-forwarded by deliberate compose change, never auto-exposed. **Postgres stays loopback-only**; the UI reaches it over the compose network, never the tailnet. The operator's phone reaches the UI because the phone runs Tailscale; **deep links from Telegram to `beastie.<tailnet>.ts.net:8443/...` URLs work for exactly that reason.**

---

## 6. Growth seams (decide now, cheap later)

Four seams cut in v0.1 so later features are additive, not rewrites:

1. **URL carries the run from day one.** Every route is `/run/{run_id}/...`; v0.1 hardcodes the current run and redirects `/`. Multi-run needs no route surgery. This is also the UI's stake in the §10.3 run-mapping decision (§9): project must be an addressable attribute, so a project index is a fold, not a run-boundary accident.
2. **Base layout + fragments.** Every live region is its own `_fragment.html`. SSE out-of-band swaps need it today; write controls (later) target the same fragments.
3. **The port is the API boundary, in-process.** A later `/api` JSON router is the same port calls plus `model_dump()` — roughly an hour's work, not a redesign.
4. **Auth is one FastAPI dependency.** Today it returns the tailnet identity (transport auth maps connection → actor id; per-op grants still gate everything on the write path). That dependency is the single swap point when real auth lands — no change elsewhere.

---

## 7. What the UI must never do

The honesty rules, condensed. These are structural commitments, not style.

- **Render only what the log supports.** No computed-and-cached truth the log can't reproduce.
- **Every action is exactly one auditable event**, attributed to a per-person actor.
- **No persistent UI state that isn't an event.** Kill it, replay, render identical.
- **Surface freshness; carry the cursor.** Show generation/cursor freshness; carry the anchoring cursor into every write; on `GENERATION_MISMATCH`, full-re-read and re-render **before** any action lands; persist nothing from a mismatch view.
- **The UI is an actor.** Its grants are visible in policy, its reads are filtered by role, its writes are gated and attributed. It holds no INSERT credential (once the two-role scheme lands) and has no admin side-door.
- **Never grades, never mutates a projection, never edits policy/tiers/timestamps, never touches secrets.**

---

## 8. Build plan (Claude-Code-session units)

- **Session 1–2 (MVP, read-only, v0.1):** first land the board projection fields (title, priority, blocker context), then the FastAPI app with the Board, Events, and Metrics screens reading in-process via the port; the SSE poll-loop endpoint with the no-change short-circuit and generation-banner handling; base layout + fragments; the self-narrating ticker; the URL-carries-run seam and the auth dependency stub. Local development and visual iteration precede tailnet/8443 deployment work. **Useful after this.**
- **Session 3 (write path):** per-person human actors, ack/steering/board ops as `/emit` form POSTs with server-side §3a keying, receipts, the honest spinner, confirm-step for irreversible ops. **Lands with stage-4 Tier-1.5 machinery, not before** (§4). Brings the `hive_reader` / gateway split with it (§2, §9).
- **Session 4–5 (polish):** record-detail rendering depth, filter chips, and whichever of the deferred spicy ideas (§9) earn their keep.

---

## 9. Open items

- **§10.3 run-mapping is an input the UI needs, not a decision the UI makes.** The UI requires that **project be an addressable attribute on events regardless of how runs map onto projects** — so the project index is a fold and cross-project views are consistent. This is stated to §10.3 as a constraint; the UI does not resolve one-run-per-project vs. project-as-attribute. (If project-as-attribute is chosen, the port spec's fold-budget trigger fires — noted there, not decided here.)
- **Two-role-scheme acceleration.** Owner decision: ship v0.1 under the single application role (recommended — the write path is what the split fences, and v0.1 has no INSERT path), or accelerate `hive_reader` into v0.1 read-only as defense-in-depth. See the decision shortlist accompanying this draft.
- **Spicy-idea backlog (deferred, not cut):**
  - *Catch-up replay* — the log knows the operator's last-seen position; a ~30-second animated replay of everything since (board evolving, escalations rising/resolving). Pure projection, no new truth. **Later** — genuinely nice, not load-bearing for first-useful.
  - *Time-travel board scrub* — re-fold the board at any cursor (free, because the board is always a fold); escalation review replays the exact board the coordinator saw at that cursor. **Later.** Its twist — emit a `human.viewed(cursor, generation)` event so "what was the human looking at when they acked" is auditable — is a genuine one-event-type addition; **owner decision** whether that legibility is worth one new promoted event type (shortlist item).
- **Telegram ↔ web boundary (v0.1).** v0.1 web is read-only; **Telegram remains the push/ack surface** until the write path lands. Every promoted Telegram event carries a deep link to its exact web view, and the Telegram message must be **decidable without the link** (flaky signal on the road). When the write path lands, web actions echo back into the chat thread — both are projections of one log, so they never disagree.

---

## 10. Revision record

- **v0.1 DRAFT (Jul 8 2026)** — initial synthesis. Adjudications made: (1) v0.1 reads as a coordinator-role actor under the single application DB role; dedicated `hive_reader` deferred to the write-path revision, not accelerated for read-only. (2) v0.1 ships read-only; the write path (per-person human actors, ack/steering ops) is designed here but built with stage-4 Tier-1.5 machinery — the UI ack button and the stage-4 outbound gate are identified as one mechanism. (3) §10.3 run-mapping stated as an input (project-as-addressable-attribute), not re-decided. (4) Telegram remains the push/ack surface until the write path lands; v0.1 web is read-only; deep links work via operator-phone Tailscale. (5) Spicy-idea triage: self-narrating ticker → v0.1; catch-up replay and time-travel scrub → later; `human.viewed` event flagged as an owner decision.
- **Implementation alignment (Jul 9 2026):** Board title, priority, and blocker context become authoritative board-projection fields. Project filtering/indexing, stage-4 metrics, and generic record detail are explicitly deferred in §3.2; local screen work and visual iteration precede tailnet deployment.
- **MVP stack simplification (Jul 9 2026):** the read-only implementation uses a local native SSE fragment swapper and one local desktop-first stylesheet in place of htmx/SSE and Pico.css. This preserves server rendering and removes asset/vendor work before the first useful operator view; the write-path seam remains ordinary form POSTs.
