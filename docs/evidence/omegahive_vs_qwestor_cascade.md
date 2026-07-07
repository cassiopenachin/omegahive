# Qwestor Research Cascade vs OmegaHive — Comparison

**Status:** Evaluation of the Qwestor team's `agents_cascade` (in `openclaw-based-research-prototype`) against OmegaHive's coordination infrastructure. Grounded in their `SKILL.md` + `config/openclaw/openclaw_example.json` and the `docs/cascade-architecture` writeup.
**Framing (important):** they are **not** building coordination infrastructure. They are getting **5 agents to do research** on stock OpenClaw. Evaluated on those terms, their choices are largely *right*; the comparison is about layers, not winners.

---

## 1. What the cascade is

A **4-role research cascade** implemented as a **prompt-level protocol** on OpenClaw's generic session primitives — *no orchestration code, no role classes, no engine*:

| Role | Lifecycle | Mandate | "Told not to" |
|---|---|---|---|
| **Main** (the chat session) | per-turn, stateless | plan, define the problem with the user, track progress, write the article files | write/edit code |
| **Executor** | **one persistent** subagent, reused | run one plan step at a time, lit review, delegate coding, validate | write code |
| **Coding** | **ephemeral** ACP session, one per task, killed after | actually write/run code (codex/claude/gemini via ACP) | — |
| **Critic** | **one persistent** subagent, reused | independently review *every* step (correctness vs the docs, code quality, efficiency) | — |

Coordination is OpenClaw `sessions_send` (agent-to-agent, blocking-for-reply) + **shared markdown files** (`worklog_plan.md` = Main's tracker + the Executor's session key; `runs/status.md` = run state; numbered `0X_*.md` = the article/deliverable). A stateless Main re-targets the same Executor/Critic each turn by resolving a **label**. The substance is a genuinely good **7-step research methodology** (problem → theory → data/sanity → experiments → results → conclusions) with templates.

**The only *hard* enforcement** is OpenClaw config: a coarse **13-tool subagent whitelist** (identical for every subagent — and it includes `write`/`edit`/`exec`), a **spawn-depth cap of 2** (structurally bounds Main→Executor→Coding), and ACP allow-list + concurrency caps. **Everything else is prose.**

## 2. The axis: hard vs soft

Their own architecture doc says it best: *"the interesting boundaries are almost all in the soft layer."* That is the whole comparison. Qwestor and OmegaHive sit at opposite ends of one axis:

- **Qwestor:** coordination, roles, and governance live in **prose + labels** over OpenClaw's primitives. Cheap, flexible, *unenforced*.
- **OmegaHive:** the same things live in an **enforced substrate** — one event log, the gateway (per-role emit-authority + transition gates), the board, promotion, capability-keyed policy. Expensive to build, rigid, *enforced*.

## 3. Pros (on their own terms — getting 5 agents to work)

1. **It works now, with ≈zero infrastructure.** One `SKILL.md` + one config gives a running 4-agent research system on stock OpenClaw. We are at M2/M3 of *building* a substrate; they have a shipping system. For their goal this is dramatically cheaper.
2. **It uses the platform instead of reinventing it.** `sessions_spawn/send/list/history`, ACP for heavyweight coding, depth/concurrency caps — they leaned on OpenClaw's primitives rather than writing a coordination layer.
3. **Soft = cheap evolution.** Roles, mandates, and the whole workflow are markdown — change the research method without touching code or schema. (This is "make evolution cheap," taken to the limit.)
4. **The patterns are sound.** Persistent Executor/Critic + ephemeral per-task Coding is the right lifecycle split; files-as-state + label-resolve is a reasonable way to give a stateless Main continuity; an independent Critic reviewing every step is a real quality mechanism.
5. **The research methodology is genuine value we lack.** The 7-step workflow + templates is *content* (how to do ML/science research well) that OmegaHive doesn't provide and would want regardless.

## 4. Cons (versus what enforced infrastructure buys) — and they map onto the known failure data

1. **Role boundaries are not boundaries.** Every session is the *same* `main` agent with the *same* 13-tool set including `write`/`edit`/`exec`. "Executor doesn't write code," "Critic is read-only," "Main doesn't edit" are **hopes, not facts** — a confused or misaligned model violates them silently. This is exactly MAST's dominant failure class (specification / inter-agent misalignment) and Cognition's "conflicting implicit decisions." OmegaHive's gateway makes these *credential-level* facts.
2. **No single source of truth.** State is smeared across `worklog_plan.md`, `runs/status.md`, the article files, and session transcripts, coordinated by direct A2A sends. Continuity hangs on the model remembering to record/reuse the Executor key — their doc flags that it "silently degrades into spawning fresh sessions, losing context." OmegaHive's event log + board *is* the single auditable truth this lacks.
3. **The Critic is advisory, not a gate.** It reviews every step, but nothing *blocks* progress on a bad review — Main is merely instructed to incorporate feedback. OmegaHive's review instrument + enforced done-gate make it a real gate (no `done` without a pass). Their Critic is our review *concept*, un-enforced.
4. **No least-privilege.** One uniform tool set for all subagents; no per-role restriction. (Their doc's §8 notes per-agent tool policies *could* harden this — a coarse cousin of our capability-keyed gateway — but it isn't used.)
5. **No legibility layer.** A human watching a 10-hour, 4-agent run gets raw transcripts + files, no curation. OmegaHive's promotion (H3) is exactly the missing legibility.
6. **No determinism, replay, or coordination metrics.** Real LLM sessions; nothing to replay or measure coordination quality over. Fine for a product; it means you can't *study* the coordination the way our harness can.

## 5. What it means for OmegaHive

- **It validates our premise.** Their *own* writeup concludes the risk lives in the soft layer and that hardening it requires per-agent enforced policies — which is precisely OmegaHive's bet (move coordination/roles/governance from prose to an enforced substrate). Their analysis points straight at the gap our infrastructure fills.
- **It's also a reality check.** They have a *working* 4-agent research system with none of our infrastructure. So our infra is **unproven-as-necessary** for the 5-agent research use case — the soft approach may be *sufficient* until coordination failures actually bite. The honest question our prototype exists to answer: *does enforcing the boundaries measurably beat the prose version on real research?*
- **RCBench is where that gets tested** — and the cascade is a natural **third baseline** there, alongside OpenClaw-solo: does an OmegaHive-coordinated hive beat *both* solo OpenClaw (16.6) *and* the prose-cascade on the same tasks/rubric?
- **What to borrow.** (a) The **research-workflow skill** (7 steps + templates) is good content we'd want as a planner/worker skill regardless — orthogonal and complementary to our infra. (b) Their lifecycle split (persistent Executor/Critic, ephemeral Coding) validates our roster shape (planner/coordinator + workers + review). (c) `sessions_send` is a real OpenClaw A2A primitive — relevant to our "A2A at the edges" thought. (d) Their Critic = our review; their files = our (un-evented) board.
- **The merge is a layering, not a contest.** When we merge their work (before the expensive runs), the natural shape is: **OmegaHive hosts the cascade.** The research-workflow becomes a planner decomposition + worker roles; the gateway/board/review-gate harden their soft boundaries (Executor *genuinely* can't write code; the Critic's verdict gates `done`; coordination is one auditable log). Qwestor = the *what* (methodology + roles); OmegaHive = the *enforced how*.

## 6. Bottom line

They made the right call **for their goal**: prose-on-OpenClaw is the minimal path to 5 working agents, and it ships. OmegaHive is a *different bet* — that enforced coordination and governance pay off in reliability and at scale — and their own architecture doc names the exact soft-layer gap our substrate targets. The two aren't really competitors; they're **layers**: their research methodology + role protocol could run *on* our substrate, and RCBench is where we find out whether the enforced version actually wins.
