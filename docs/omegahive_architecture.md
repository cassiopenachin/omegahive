# OmegaHive — Target Architecture & Path

**Status:** The architecture we are building toward, and the staged path from where we start to there.
**Companion docs:** the v0 spec and M0 spec detail the first stage — read those for the *how* of stage one. This document is the *why* and the *destination*.
**Lineage:** evolved from Ben Goertzel's `OmegaHive1` proposal. Section 2 sets out what we kept, evolved, and replaced, and why.

---

## 1. Thesis

A cooperative hive of real OmegaClaw and OpenClaw agents doing research-grade work — formalization, proof, software, writing — coordinated through a single **event-sourced spine**. Every operational view (task board, human channel, metrics, knowledge) is a *projection* of that one log. Every agent's powers are mediated by a **capability membrane** over one central policy. The coordination plane is deliberately lightweight; the heavy compute — Lean, proof search, inference — lives off the spine, where the real scaling happens.

The shape is the bet: one log, everything else a projection or a consumer, every power gated by a membrane. That shape is what has to scale, and it does — new roles are new emitters, new views are new projections, new powers are new capabilities. None of them reshape the substrate.

## 2. How this evolved from OmegaHive1

The spine of Ben's design is sound, and most of it is kept. The changes below are about *substance* — each with a reason.

**Kept:** a hive of role-specialized agents with guiding prompts; a task board with explicit ownership and provenance; deterministic promotion rules that turn a full operational trace into a legible human channel; a versioned HIVE.md constitution; permission tiers enforced at the gateway, not in prompts; a reflective conscience (Psyche); editorial gating on provenance; a librarian over a shared doc store; single-image fleet management with an out-of-band human recovery path; Lean-as-a-service and a centralized browser; observability and cost dashboards.

**Evolved:**

- *Two-tier comms (a fast bus + Slack)* → **one event log with derived views.** Ben already says the fast tier is "curated out of Slack — editorial, not visibility," which *is* an event-log-with-views model. So there is one log of record; "bus" and "human channel" are filters over it. The two-tier question becomes a promotion config we can test, not two transports to build and keep in sync.
- *A shared Atomspace as the coordination substrate (stigmergy)* → **the event log is the source of truth; the Atomspace is an optional advisor and projection,** measured against a non-Atomspace baseline. This lets us actually test whether Hyperon cognition pays for itself rather than assuming it carries the hive's semantic traffic.
- *Permission tiers in config and prompts* → **a capability membrane.** Every agent reaches every power — log, artifacts, tools, memory, outbound actions, credentials — only through a governed adapter on one central policy. Tiers become capability grants; the conscience pauses an agent by flipping its tier live.
- *Added a Planner, separated from the Coordinator.* Ben's roster has a chief-of-staff but no planner. Fusing "what work should exist and why" with "who does it right now" tends to either under-plan or stall the control loop. They are peers that coordinate through the log, never directly.

**Replaced / deferred:** the assumption of one OpenClaw instance per agent; and the premise that every cognitive module is its own standing coordination role. Several of the OmegaClaw "mind modules" — the philosopher, the practical thinker, the communicator — are better modeled as *workers behind the event interface* than as standing parts of the coordination core. The motivated core is **planner + coordinator**, with the conscience as an observer; the rest are workers, brought in as the work demands them.

## 3. The fully-realized architecture

**The spine.** One append-only event log of record. It carries *coordination* events — references and small structured fields, never bulk content. Total order, full provenance, deterministic replay for audit.

**Projections & consumers.** The board (current task state), the human view (promoted events, rendered to Slack), metrics, audit/replay, and the knowledge layer are all read-only projections or consumers of the one log. Adding a view never touches the spine.

**The membrane & capability gateway.** Every agent's sole route to every capability — log, artifact store, tools, memory, outbound actions, credentials — is a governed adapter, all under one central versioned policy. Two invariants hold forever: *no ungoverned route*, and *one central policy*. Permission tiers are simply capability grants; the conscience enforces by changing tiers live, and gateways respect the change immediately.

**Roles, by emitter authority.** *planner* (the plan); *coordinator(s)* (assignment and board control); *workers* (the cognitive and tool agents that do the work); *instruments* (promotion, metrics, review, provenance, permission — deterministic derived events); *governance* (the conscience: observes the full log, flags drift, holds the live tier control). The full roster maps onto these classes; a new role is a new row, not new architecture.

**The work / compute plane — where scaling actually happens.** The heavy resources — Lean-as-a-service and proof search, LLM inference, browser automation — sit *off* the spine, reached as capabilities. For research-grade math this is the bottleneck: Lean elaboration and proof search dominate wall-clock, and LLM proof generation outruns the checker. The coordination plane, by contrast, does trivial work per task. Scaling the hive means scaling *this* plane — more Lean workers, more inference capacity — not the coordinator. Coordinator saturation is unlikely to be a binding constraint before the compute plane is.

**Knowledge.** A shared, librarian-curated artifact/doc store, referenced from the spine. The Atomspace is an advisor the coordinator and cognitive agents can query, and a projection of the log — never the coordination substrate itself.

**Governance & recovery.** HIVE.md values injected into every agent; permission tiers as capabilities; the conscience as live tier control; an out-of-band, agent-free recovery path available at all times.

## 4. Scaling invariants (must stay true along the whole path)

- **The spine carries references, not bulk.** Artifacts, proofs, and tool outputs live in their own stores; the log carries refs. This is what keeps a single log viable at full scale.
- **Every capability through the membrane, on one policy.** The gateway is universal; an ungoverned route is a hole in the whole governance story.
- **Global log ≠ global capabilities.** The coordination *history* is auditable to everyone — nothing secret in the record — while *powers* are restricted by tier. Secrets and credentials never land in the log.
- **One clock for rules.** Rules and metrics read `logical_ts` (a sim clock in Regime A, wall-derived in Regime B) — never a mode-dependent field.

## 5. Where we start: v0

Regime A — single process, simulated clock, one capability (`log`). A real spine + board + promotion + metrics + review, driven by a *stubbed* planner, a *greedy* coordinator policy, and *stubbed* workers, fully deterministic and replayable. Enough to test coordination coherence, promotion legibility, and metrics (H1/H3/H6) cheaply, and to establish the baseline the real coordinator is later measured against. Detailed in the v0 spec; the first build slice is the M0 spec.

## 6. The path: v0 → fully realized

Each stage adds fidelity or capability *behind an existing seam*; none reshapes the spine. Each names what it proves.

| Stage | Adds | Proves |
|---|---|---|
| **0** (M0) | spine + envelope + planner events + trace render | the substrate exists and replays |
| **1** (M1–M3, Regime A) | board reducer; stub workers/coordinator/review/promotion/metrics; open- and closed-loop scenarios | coordination mechanics, promotion legibility, metrics (H1/H3/H6) |
| **2 — gateway de-risk** | a **simulated artifact store** as the *second* capability behind the membrane | the capability gateway holds for more than one capability, and refs-not-bulk — the one bet we should not leave untested |
| **3** (Regime B begins) | swap the greedy coordinator → a real OmegaClaw BossyTron via the channel binding; multi-process; wall clock | the OmegaClaw-coordinator path; the substrate under independent writers |
| **4 — real agents, one role at a time** | replace a stub worker with a real cheap-LLM / OpenClaw agent, then more; real artifacts + content-inspecting review and provenance (H4) | the worker ladder; content-dependent coordination; the editorial gates |
| **5 — capabilities & governance** | real tool / outbound capabilities + permission tiers as grants; conscience as live tier control; Slack renderer for the human view | the permission and governance story end to end (H5) |
| **6 — compute plane & research roster** | Lean-as-a-service + proof workers; the research roster (the Xirtus → math-agent pipeline); the Atomspace advisor (H2) | research-grade work at its real bottleneck; whether Hyperon cognition pays for itself |
| **7 — multiplicity, only if needed** | multiple coordinators over partitioned task sets | *only if* measurement shows the coordination plane saturating — not expected before the compute plane does |

**Stage 2 is the key early de-risk, and it is cheap.** The second capability is not arbitrary: it is exactly the artifact store that invariant #1 (refs-not-bulk) already requires, in *simulated* form. So one move proves the capability-gateway abstraction *and* exercises refs-not-bulk, with no real artifacts yet. We test the gateway (the abstraction the whole hive's governance rests on) through a simulated second store — before anything depends on it.

## 7. What would make us reconsider

- **The capability gateway leaking** when the second capability lands at Stage 2 — the reason we front-load it rather than discover it at full-hive scale.
- **Spine volume or coupling** becoming a problem — not expected under refs-not-bulk; and if it did, the event *schema* is transport-portable, so moving the log of record to a distributed log would be a migration, not a redesign.
- **The compute plane is the expected scaling pressure** — Lean, proof search, inference — as it should be for research-grade work. That is where capacity planning goes; the coordination plane is built to stay cheap.
