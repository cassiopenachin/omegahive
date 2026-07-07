# OmegaHive — Target Architecture & Path

> **OBSOLETE (Jul 6 2026).** Superseded by [omegahive_design_1_1.md](omegahive_design_1_1.md) (the self-contained implementation design against Ben's 1.1 spec) and [omegahive_test_plan.md](omegahive_test_plan.md). Residual content was folded there Jul 6 (secrets-never-in-log invariant → design §3.1; ThreadKeeper positioning → design §7 extensions; artifact-store-as-second-capability de-risk → design §7 stage 5; the live-precursor failure modes → test plan D2). Retained for history only — do not update.

**Status:** The architecture we are building toward, and the path from where we are to there (updated Jul 2 2026). It describes one program — the **unified path** — converging two efforts onto one system: a coordination substrate built bottom-up (the `omegahive` repo), and a live deployment of real agents built top-down (ZeroBot and ProtomegaTron working over Telegram, §2).
**Companion docs:** [omegahive_plan.md](omegahive_plan.md) (the operational plan), [omegahive_omegaclaw_binding.md](omegahive_omegaclaw_binding.md) (the coordinator binding design), the v0/M-specs (the built baseline).
**Lineage:** evolved from Ben Goertzel's `OmegaHive1` proposal (§2).

---

## 1. Thesis

A cooperative hive of real OmegaClaw and OpenClaw agents doing research-grade work — formalization, proof, software, writing — coordinated through a single **event-sourced spine**. Every operational view (task board, human channel, metrics, knowledge) is a *projection* of that one log. Every agent's powers are mediated by a **capability gateway** over one central policy. The coordination plane is deliberately lightweight; the heavy compute — Lean, proof search, inference — lives off the spine, where the real scaling happens.

The shape is the bet: one log, everything else a projection or a consumer, every power gated by a gateway. That shape is what has to scale, and it does — new roles are new emitters, new views are new projections, new powers are new capabilities. None of them reshape the substrate.

The bet now has a live falsification path: the **coordination-demand triage** (plan doc) tests whether the problems we care about actually exhibit coordination-shaped failures before we spend heavily on the comparison experiments. §7 records what a negative answer would mean.

## 2. Lineage: OmegaHive1, and the live precursor

**From OmegaHive1.** The spine of Ben's design is sound, and most of it is kept: role-specialized agents with guiding prompts; a task board with explicit ownership and provenance; deterministic promotion turning the full operational trace into a legible human channel; a versioned HIVE.md constitution; permission tiers enforced at the gateway, not in prompts; a reflective conscience; editorial gating on provenance; a librarian over a shared doc store; out-of-band human recovery; observability and cost dashboards.

**Evolved:** two-tier comms → one event log with derived views; shared-Atomspace-as-substrate → the log is the source of truth, the Atomspace an optional advisor/projection measured against a non-Atomspace baseline; permission tiers in prompts → a capability gateway; a Planner separated from the Coordinator, peers coordinating through the log.

**The live precursor (new).** Ben's `protobots` deployment *is* a hive, unstructured: ProtomegaTron (an OmegaClaw instance whose LLM route is the OpenClaw Gateway), ZeroBot (OpenClaw), and Ben in a shared Telegram group; coordination via chat, memory via markdown notebooks, scheduling via cron lanes, review via humans and peer researchers' bots. The unified path treats this as the hive's **first real population** and its **"before" state**: the same agents, with chat demoted from coordination substrate to ingress/visibility surface and the board becoming the membrane between them. Its documented failure modes — a 998k-token session blow-up, a polluted-history crash cascade, lost cross-session context, role boundaries as prose, no audit trail — are the concrete costs the substrate exists to remove, and the "before" measurements for the substrate-value comparison.

## 3. The fully-realized architecture

**The spine.** One append-only event log of record. It carries *coordination* events — references and small structured fields, never bulk content. Total order, full provenance, deterministic replay for audit.

**Projections & consumers.** The board (current task state), the human view (promoted events, rendered to **Telegram** — concretely, the protobots group), metrics, audit/replay, and the knowledge layer are all read-only projections or consumers of the one log. Adding a view never touches the spine.

**The gateway.** Every agent's sole route to every capability — log, artifact store, tools, memory, outbound actions, credentials — is a governed **gateway**: a policy layer above the resources it fronts. It enforces policy on the way in — who may emit what, and whether a stateful transition is legal — and projects on the way out, while the resources stay dumb. *Structure in the store, policy in the gateway*; dependencies flow one way. All gateways run under one central versioned policy. Two invariants hold forever: *no ungoverned route*, and *one central policy*. Permission tiers are capability grants; the conscience enforces by changing tiers live.

**Roles, by emitter authority.** *planner* · *coordinator(s)* · *workers* · *instruments* (promotion, metrics, review, provenance — deterministic derived events) · *governance* (the conscience). A new role is a new row, not new architecture. Two positioning notes from the merge:

- **ThreadKeeper** (OmegaClaw's parent→child subagent dispatch) is *intra-worker* machinery: a worker's private subagents, beneath the board. Peer coordination happens on the board; what a worker does inside its own turn is its own business, bounded by its capability grants. Where nested delegation should surface *onto* the board is the delegation ladder (plan doc). ThreadKeeper's goal is in scope; our working principle is to support the goal while staying free to reach it a better way.
- **The qwestor `agents_cascade`** (Main/Executor/Coding/Critic as a prose protocol on stock OpenClaw) is a **worked example of a hive**: a role protocol the board hosts, with minor modifications to make it hive-compatible — its Critic becomes our enforced review-gate, its markdown state becomes board + artifact refs. It is a test case for the substrate's usability, not a separate architecture.

**The work / compute plane — where scaling actually happens.** Heavy resources sit *off* the spine, reached as capabilities. **Lean-as-a-service and proof search** keep their named slot: for research-grade math they are the wall-clock bottleneck, and scaling the hive means scaling this plane, not the coordinator. The **qwestor research product** (FastAPI/LangGraph multi-source research app) is at most an *unproven candidate* worker-service here — a research-automation API a task could call — and has not earned a named role.

**Knowledge.** A shared, librarian-curated artifact/doc store, referenced from the spine. The Atomspace is an advisor and a projection of the log — never the coordination substrate itself. **Open pressure-test:** whether Ben's `petta-memory` (PLN-ready append-only MemoryCluster journal with bounded prompt/index/PLN views) converges with the board's promotion/legibility machinery, or evolves separately. Converging early buys shared schema and PLN-readiness; its cost is forcing more OmegaClaw into the substrate and muddying the cognition comparison. Decision deferred to an explicit pros/cons memo (plan doc).

**Governance & recovery.** HIVE.md values injected into every agent; tiers as capabilities; the conscience as live tier control; an out-of-band, agent-free recovery path at all times.

## 4. Invariants (must stay true along the whole path)

The original four:

- **The spine carries references, not bulk.** Artifacts and tool outputs live in their own stores; the log carries refs.
- **Every capability through the gateway, on one policy.** An ungoverned route is a hole in the whole governance story.
- **Global log ≠ global capabilities.** History is auditable to everyone; powers are restricted by tier. Secrets never land in the log.
- **One clock for rules.** Rules and metrics read `logical_ts`, derived from one authoritative source.

And five more that independent writers make non-negotiable (surfaced by code review of the baseline implementation). Under concurrency these are architecture, not implementation detail — they define what "the substrate under real agents" *means*:

- **Atomic gated emits.** Fold→gate→append is one atomic, per-emit-committed unit (per-run advisory lock). Single-writer correctness must be enforced, never accidental.
- **One legality table.** The transition gate and the board fold consult the same declarative legality spec. No event may be accepted and silently inert — accepted-but-dropped is the worst failure mode for an LLM coordinator (no rejection, no effect, no signal).
- **Rejections are recorded, structured feedback.** A refused op returns a machine-readable code + reason *as a value* (never an exception across the boundary) and leaves a trace in the log, so refusals enter the coordinator's context and the log tells the whole story.
- **Idempotent event identity.** Client-supplied idempotency keys with insert-or-return-existing semantics; a network retry can never duplicate an event, and independent writers can never collide.
- **Server-set time.** The gateway sets `wall_ts` and derives `logical_ts` on insert; no caller-supplied clock is trusted.

The simulation harness (DES engine, stub workers, seeded RNG) is **quarantined** as the deterministic regression bed: the reference greedy coordinator runs through both the sim binding and the real port, asserting identical logs.

## 5. The environment

Both existing environments are wrong: our single-process harness (in-process calls, one transaction, no real clock) has run its course, and Ben's deployment is hand-built onto one Pop!_OS laptop (source-built SWI-Prolog, absolute paths, local policy patches). The unified target is a **generic containerized deployment** — one compose profile: Postgres, the gateway service, an OmegaClaw container, an OpenClaw gateway; secrets via env; no hardware or path assumptions — runnable on any stock Linux box (an ordinary developer desktop as the acceptance test). The live deployment's laptop becomes one deployment of it, not the definition of it. The qwestor team's docker-compose (OpenClaw gateway from GHCR + backend + Postgres 16) is the pattern to reuse.

## 6. The path

Named stages; each adds capability behind an existing seam, none reshapes the spine. Operational detail, gates, and sequencing live in [omegahive_plan.md](omegahive_plan.md).

| Stage | Adds | Proves |
|---|---|---|
| **baseline** *(done)* | sim substrate: spine, gateway, fold, promotion, metrics, greedy control; the baseline coordination experiments | the mechanics, promotion legibility, metrics (H1/H3/H6); the yardstick everything else is measured against |
| **triage** | coordination-demand mining of RCBench runs + instrumented solo re-runs | whether — and on which problems — coordination-shaped failure actually occurs; gates the expensive experiments and checks the goal itself (§7) |
| **port** | the gateway service + `HiveCoordinatorPort`; the five invariants implemented; sim quarantined; the generic environment; the OmegaClaw fork (Ben's changes reviewed and cleaned or redone) | the substrate under independent writers |
| **spike** | the coordinator ladder over the board: greedy → vanilla-LLM → OmegaClaw (binding per the binding doc) | the binding works end-to-end; H2, the cognition axis |
| **first executor** | ZeroBot binds via board-op skills; board-vs-chat comparison (same cognition, two bindings) via rcb-eval | the substrate axis: does the board beat chat on real work |
| **cascade host** | the qwestor cascade replicated on the board (enforced Critic, specialist roles) | usability of the substrate for an independently-designed pattern; the enforcement bet |
| **compute plane** | real tools/capabilities behind the gateway (artifact store first, as the second-capability de-risk), Lean-as-a-service, conscience as live tier control | H4/H5; research-grade work at its real bottleneck |

Multiplicity (multiple coordinators over partitioned task sets) remains deferred until measurement shows the coordination plane saturating — not expected before the compute plane does.

## 7. What would make us reconsider

- **The triage coming back negative.** It is entirely possible that the complex coordination we've been assuming a hive needs is only needed for the kinds of problems Ben wants to spend time on. If coordination-shaped failures turn out to be rare on the benchmark problems, the goal itself needs adjusting: *making a scientist like Ben as productive as possible* is a worthwhile goal — but it is not the same product as *robust hive-management software*, and we would re-scope deliberately rather than keep building the second while only the first is demanded.
- **The capability gateway leaking** when the second capability (artifact store) lands — the reason it is front-loaded in the compute-plane stage rather than discovered at full-hive scale.
- **Spine volume or coupling** becoming a problem — not expected under refs-not-bulk; the event schema is transport-portable, so moving the log of record would be a migration, not a redesign.
- **The compute plane saturating first** is the *expected* pressure — that is where capacity planning goes; the coordination plane is built to stay cheap.
