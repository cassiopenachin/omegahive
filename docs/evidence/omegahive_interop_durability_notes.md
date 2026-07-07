# OmegaHive — Interop (MCP/A2A) & Durability (Temporal) Notes

**Status:** Side exploration prompted by the competitive eval. Options and implications, not decisions. *Messaging/positioning is out of scope* — this is an internal prototype; if it proves out, honest messaging comes later.

**Bottom line up front:** both attach at seams we already designed, so **M0 needs no changes.** MCP/A2A are gateway capability-types (additive, no spine change). Durable execution is a *different layer* from the coordination log and a Regime-B-and-later question; if we want it, a Postgres-native engine (DBOS) is more coherent for us than a Temporal cluster.

---

## 1. MCP + A2A at the edges

The principle: **the internal coordination log stays the source of truth and stays bespoke; the standards live only at the gateway's outer surface, as governed capability-adapters.** Two distinct edges.

**Inward edge — tools = MCP.** "Use a tool" is a capability. The gateway's tool-adapter is an MCP *client*; registered MCP *servers* are the tools (web search, code execution, browser, eventually Lean). A grant in the central policy reads "role R may call MCP tool T" — tiers become tool allow-lists. The Stage-2 "second capability" (the simulated artifact/doc store) is the first instance; real tools then arrive the same way. Optionally we expose *our own* surfaces (artifact store, board reads) as MCP servers so external tools/agents consume them through one standard.

**Outward edge — agents = A2A.** A worker slot need not be one of our processes. An **A2A adapter** lets an external agent (a third-party math agent, a commercial coder) fill a worker role: it discovers the agent via its Agent Card and bridges our events ↔ A2A tasks.

Concrete flow (external A2A worker):

```
coordinator emits  task.assigned {worker: ext-mathbot}
  → A2A adapter (ext-mathbot's gateway) sends an A2A task:
       { spec, input artifact refs, acceptance criteria }
  → external agent works  (entirely outside our trust boundary)
  → A2A adapter receives A2A status/artifacts and emits on OUR log:
       task.accepted → task.progress* → task.result_posted { artifact_refs }
       (provenance: "via A2A adapter; external agent ext-mathbot")
  → review instrument + board treat it like any worker's result
```

Crucially, the external agent **never touches our log** — the adapter is its gateway, projecting only the task out into A2A and writing only well-formed events back. So our governance, visibility, refs-not-bulk, and audit invariants hold *even for external participants*. That is the property that lets us adopt the standard without becoming an island or losing the auditable trace.

Boundary to keep crisp: **A2A is point-to-point task delegation at the edge; it does not replace our internal event log.** Internally = the event-sourced shared substrate; externally = A2A as the lingua franca for tasking-out and being-tasked. We could also publish the coordinator as an A2A endpoint so other systems submit goals into the hive.

Why it is safe to adopt: it is purely additive at the gateway. `tool:<server>` and `a2a:<agent>` are just new capability types in the *already capability-keyed* policy; the envelope is already emitter-agnostic (a stub, a real OmegaClaw agent, and an A2A-bridged external agent all look identical on the log). No spine or schema change.

## 2. Temporal / durable execution

First, de-conflate — the eval slightly blurred this: **durable execution is not a substitute for our coordination spine.**

- Our **coordination log** is a shared, append-only, multi-agent substrate with board / promotion / governance as projections. It is stigmergic — agents react to events.
- **Temporal** is durable execution for a *single long-running process*: imperative workflow code + activities, an immutable per-workflow history, resume-from-crash, and no double-spend of LLM/tool calls on retry. It makes *one agent's (or the coordinator's) loop* reliable; it is not a shared coordination substrate.

So the real question is not "Temporal vs Postgres-log." It is: **do we also adopt a durable-execution engine to make individual agent processes crash-proof** — separate from, and feeding, the coordination log? Three options:

- **(A) Keep the Postgres log; hand-roll durability as needed.** What M0 does. Simplest, no new infra, fine while agents are stubs and deterministic. Cost: when real long-running agents arrive, we hand-build retries / resume / timeouts / idempotency — the exact plumbing the eval warned against reinventing.
- **(B) Postgres log + a durable-execution engine for real agent processes (Regime B).** Best-of-both: the log stays our coordination substrate; each long worker task (a Lean proof run, a multi-step research loop) runs as a durable workflow that survives crashes and deploys and does not re-spend on retries. More infra, but a clean separation of concerns (durability vs coordination).
- **(C) Build the coordination log *on* Temporal's history.** Wrong shape — Temporal history is per-workflow, not a shared cross-agent substrate; you would be fighting the tool. Rejected.

If we ever go (B), the architecturally coherent pick is probably **not Temporal but DBOS** (or Restate): DBOS is *Postgres-native* durable execution, so it composes with the substrate we already run instead of adding a separate stateful cluster. Temporal is more battle-tested (OpenAI, Replit, Lovable) but is a second stateful system with an opinionated workflow model that wraps awkwardly around OmegaClaw's own MeTTa loop.

**Pros** of adopting durable execution (B), when real agents land:

- Long tasks survive crashes / restarts / deploys (resume, not restart) — matches Anthropic's checkpoint and rainbow-deploy lessons.
- No re-spend of LLM/tool calls on retry — directly helps the cost story.
- Retries, timeouts, human-in-loop signals, scheduling, and workflow versioning — solved, not hand-built.

**Cons:**

- Operational weight (a cluster — unless DBOS on our existing Postgres), against "keep prototyping light."
- An opinionated execution model that sits awkwardly around autonomous LLM / MeTTa agent loops.
- Two histories (the engine's per-workflow history + our coordination log) — needs a clean "durability ≠ coordination" boundary or it confuses.
- For research-math, the heaviest durability need is the **compute plane** (Lean runs), which the Lean service's own queue/checkpointing may handle better than wrapping agents in a workflow engine.

**Lean:** not for v0 (deterministic, stubbed — durability is irrelevant). Revisit at Regime B specifically for worker-process reliability, and evaluate DBOS-first given our Postgres substrate. Keep the agent/append code so a durable backend is a swap, not a rewrite.

## 3. Does M0 change in anticipation?

**No — keep M0 as written.** The useful part is *why*: the seams these attach to already exist because of earlier decisions.

- **MCP/A2A** slot into the **capability-keyed policy** (`role, capability, action`) and the **emitter-agnostic envelope** (a stub, a real agent, and an A2A-bridged external agent are indistinguishable on the log). Adding `tool:*` / `a2a:*` capabilities later touches the policy and a new adapter — never the spine or M0's schema.
- **Durable execution** sits *under* agent processes (Regime B) and *feeds* the log; M0's single-process deterministic harness neither needs it nor blocks it. The one related caveat is already on the books: deterministic `event_id = uuid5(run_id:emit_index)` becomes `run_id:agent_id:index` for multi-process — a Regime-B change, not an M0 one.

So M0 ships unchanged. The only worthwhile *anticipatory* act is a one-line note (in the architecture doc, whenever) that `tool` and `external-agent` are expected capability types and that external/bridged agents are first-class emitters — making the seam explicit for whoever builds Stage 2 / Regime B. No code, no schema, no M0 edit.

## 4. Summary

| Question | Answer |
|---|---|
| MCP edges | Gateway tool-adapter as MCP client; tools = MCP servers; "role may call tool T" = a capability grant. Additive. |
| A2A edges | External agents fill worker roles via an A2A adapter bridging events ↔ A2A tasks; the external agent never touches the log; governance and audit preserved. Additive. |
| Internal vs external | Internal coordination = our event log (unchanged). External = MCP (tools) + A2A (agents) at the gateway only. |
| Temporal | A different layer (agent-process durability), not a spine replacement. Defer to Regime B; prefer Postgres-native DBOS over a Temporal cluster for coherence. |
| M0 changes | None. The capability-keyed policy + emitter-agnostic envelope already are the seams. Optional: one doc note naming the `tool` / `external-agent` capability types. |
