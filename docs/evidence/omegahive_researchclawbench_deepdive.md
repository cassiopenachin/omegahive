# ResearchClawBench — Deep Dive for OmegaHive

**Status:** Evaluation of [ResearchClawBench](https://internscience.github.io/ResearchClawBench-Home/) (InternScience/SJTU; [arXiv 2606.07591](https://arxiv.org/abs/2606.07591), 9 Jun 2026; MIT-licensed) as a source of real-difficulty scenarios and an external benchmark for OmegaHive.
**Sources mined:** GitHub README, HF dataset card, and the arXiv paper (results + error analysis). Where a claim is from the paper's figures I haven't fully parsed, it's flagged.

---

## 1. What it is

An **end-to-end auto-research** benchmark: given a curated scientific *workspace* (raw data + reference papers + task instructions) and a research goal, can an agent independently explore, write code, and produce a `report/report.md` that reaches the **same scientific conclusions as a real published paper**? A two-stage pipeline: (1) autonomous research, (2) a multimodal LLM judge scores the report against the target paper on an expert-built rubric.

It's credible and current: a large SJTU / Shanghai-AI-Lab-adjacent author list (Wanli Ouyang, Lei Bai, Bo Zhang, …), MIT license, an active leaderboard updated through the present week, an HF dataset, and a community task-submission pipeline.

**Lineage worth noting:** the agents it benchmarks include **OpenClaw**, **ResearchClaw**, and others in the same "Claw" family OmegaClaw/OpenClaw belong to. This is effectively the *native benchmark for our own substrate* — OpenClaw (what OmegaHive's workers are) already has runs and scores here.

## 2. The tasks — real, and genuinely varied

**40 core tasks = 10 domains × 4**, plus **16 community tasks** mirrored on HF (≈56 available), each derived from a real high-impact paper, with an expert-validated rubric a human reproduced from the workspace. Domains:

> Astronomy · Chemistry · Earth · Energy · Information · Life · Material · **Math** · Neuroscience · Physics

So your instinct on caveat #4 is right — it's *not* all data-science-on-a-CSV. It spans `.h5`/`.pdb`/`.npy`/`.pt`/grid-maps across physics, chemistry, bio, neuro, materials, astro, earth, energy. (One nuance: the **Math** domain is *multi-agent pathfinding / optimization* — algorithmic, **not** formal proof / Lean — so it's research-difficulty but not Ben's Hyperseed/Lean niche. Given you'd rather have variety than Ben's sample, that's fine.)

**A task** = a folder: `task_info.json` (description + data manifest), `data/` (raw datasets), `related_work/` (reference papers), `target_study/` (the paper + the rubric + figures). The **rubric** is the gold: weighted checklist items, each with technical keywords, a weight, and a type (`text` or `image`), scored 0–100 in two modes — **A: quantitative** (metrics vs the paper) and **B: diagnostic** (evidence/logic vs the paper). 41–50 = comparable to the paper, **50 = match, 70+ = surpass**. The judge is deliberately strict ("longer reports don't score higher; substance over style").

## 3. The agents — and yes, several are multi-agent (your caveat #1)

Built-in roster: **Claude Code, Codex CLI, ARIS Codex, OpenClaw, Nanobot, EvoScientist, ResearchClaw, LingTai**, plus a lightweight **ResearchHarness** baseline that runs ~17 standalone LLMs under one workflow. The harness is **agent-agnostic** — each agent is a one-line `agents.json` command template, all fed the same unified prompt.

The ones that matter for "are these multi-agent, do logs show plans":

- **LingTai** — billed as a *"substrate for an AI organization."* That is the closest thing on the board to OmegaHive itself — a multi-agent org substrate. Its runs are the ones to scrutinize hardest.
- **EvoScientist** — *"self-evolving AI Scientists"* (evolutionary/multi-agent).
- **ARIS Codex** — autonomous "research-in-sleep" wrapper around Claude Code; imported runs only.
- **OpenClaw / Nanobot / ResearchClaw** — tool-using agents (OpenClaw is our substrate; Nanobot is an OpenClaw-alternative; ResearchClaw is a research assistant with paper-search/lit-review skills).

So the field already includes multi-agent organizations, and their traces (below) are inspectable. **OmegaHive's natural competitive frame here is OpenClaw-solo and LingTai.**

## 4. Run data — translation is feasible (your caveats #1/#2)

Every run (Web UI or the `rcb-eval` batch CLI) writes a standard bundle:

- `_meta.json` — run metadata
- **`_agent_output.jsonl`** — the agent's event stream / trace (+ a sibling `<run_id>_trace/`)
- `report/report.md` — the deliverable
- **`_score.json`** — per-rubric-item scores **with reasoning** (the graded, itemized outcome)

That's exactly the raw material to translate into OmegaHive scenarios: a **real event trace** (`_agent_output.jsonl`) gives realistic worker behavior over time, and **`_score.json`** gives a *real, graded, itemized* ground-truth outcome — far richer than our binary `quality: ok|bad` stub. The paper analyzes **280 runs** (7 agents × 40 tasks); the harness, tasks, and agents are all open (MIT), so we can also **re-run locally** to generate full traces for whichever agent/config we want — including OpenClaw, our own substrate.

## 5. The findings that should reshape our thinking

Three empirical results bear directly on the failure-mode design we were just debating:

1. **The dominant failure is "executed well, didn't reach the goal" — confirmed, not assumed.** Across all 280 runs, failures group into six error types and **concentrate on *Experiment Design Mismatch*, *Evidence Mismatch*, and *Scientific Core Missing* — not *Execution Failure*, *Reliability/Reporting Failure*, or *Goal Misalignment*.** In the paper's words: "the main problem is not that agents cannot generate reports or that execution simply fails; agents gradually depart from the target paper in protocol, key evidence, or mechanistic interpretation." This is exactly the case I said a stub can't generate and you said would be the majority — now grounded in real data. **The operational recovery ladder (retry / reconfigure / grant-tool) targets the *minority* (execution) failures; the majority are substantive and need cognition / a different scientific approach, not an operational fix.**

2. **More compute doesn't rescue it.** Score has only a weak positive relationship with cost/runtime; "even when a model spends more time, the additional computation does not necessarily produce a stable improvement." So this isn't an iteration/retry problem — which means coordination's value, *if any*, has to come from **division of labor and evidence-chain coverage**, not from throwing more cycles at one agent. (Efficiency knees: Qwen3.7-Max on cost, **OpenClaw on runtime**; Claude Code buys its top score with high cost + long runtime.)

3. **The case study is almost an argument *for* OmegaHive.** OpenClaw is the best autonomous agent on Physics_002 — and still only **27.45**. It recovers the most direct trend (fidelity vs depth) but *misses the finer evidence chain*: log-XEB, multi-metric consistency, mirror-circuit inference, the gate-counting error model. "Agent analysis often stops at the most direct observable trend while missing the finer verification steps." That is precisely the gap a **coordinated set of specialists** (a verification specialist, a physical-modeling specialist) could fill where a lone generalist stops — the OmegaHive thesis, on a real task, measurable against OpenClaw-solo.

## 6. How we use it — the three moves, made concrete

1. **Mine now (free).** Pull the run traces for the multi-agent substrates (**LingTai**, EvoScientist, ARIS) to see what real research-coordination looks like, and **OpenClaw's** traces as the solo baseline our hive has to beat. The paper's **six-error taxonomy** is, conveniently, an empirically-derived *cause vocabulary* — a good seed for our soft attribution claims (kept soft per the facts/claims decision; we don't freeze it).
2. **Translate → Regime-A scenarios.** Re-run a handful of tasks (esp. with OpenClaw) via `rcb-eval`, then map `_agent_output.jsonl` + `_score.json` into OmegaHive event scenarios — real difficulty, real graded outcome, real per-item "what was missed." This replaces our *invented* worker stubs with *real* ones, which is the right answer to "stub policies encode our assumptions." (Honest: the trace→our-event-vocab mapping still encodes choices — but grounded in real traces, not fabricated distributions.)
3. **Run for real → the Regime-B north-star.** Add OmegaHive to `agents.json` in one line; the coordinated hive attempts tasks, produces `report/report.md`, gets scored by the same rubric, and lands on the leaderboard **head-to-head with OpenClaw-solo and LingTai**. The crisp win condition: **does coordinating OpenClaw workers via OmegaHive beat OpenClaw alone (and beat LingTai) on the same tasks?** That's the central bet (H2/H3) measured against the field on real research — exactly the external yardstick the competitive eval said we lacked.

## 7. Caveats and risks (the critical read)

- **Re-discovery, not new-discovery.** The rubric is anchored to an existing paper; the benchmark's own Limitation #3 admits evaluating *truly new* conclusions needs better methods. So it measures "match a known result," not Ben's ultimate "novel discovery." Fine for measuring coordination value; not the whole vision.
- **It scores the *report*, not the *coordination*** (Limitation #2: final-report scoring, not fine-grained steps). So we measure coordination's value only *indirectly* — does a coordinated output score higher. Good enough for the bet, but it won't directly credit a clean plan or a good handoff.
- **Dry-lab only** (Limitation #1) — existing data/code/lit; no wet-lab. And **Math ≠ formal proof** here. Aligned with "variety over Ben's niche," but note it's not the Lean/Hyperseed target.
- **We'd be measured on someone else's benchmark** — their rubric + LLM-judge define "good," and it's from the same Claw ecosystem we're competing in (LingTai is a direct rival substrate). Strategically strong (credible, external, native to our substrate), but the judge is a strict, possibly noisy LLM, and the absolute bar is brutal (best ~21–27 / 100) — so frame results *relatively* (vs OpenClaw-solo), never as "we solved research."
- **Cost.** Real runs need agent + judge model APIs and tool keys (SERPER / JINA / MINERU). Re-running 40–56 tasks across configs is real money and time.

## 8. Recommendation

Do all three, sequenced, and treat it as **the** external evaluation for the prototype:

- **Now:** clone the repo, pull OpenClaw's and LingTai's run traces, and read 3–4 `_score.json` + `_agent_output.jsonl` pairs end-to-end to confirm the mapping is tractable and to calibrate what our worker substrate actually does and where it falls short.
- **Soon (feeds Regime A):** translate ~5 tasks (varied domains) into OmegaHive scenarios with real graded outcomes — the first scenarios not "invented by Claude and Cassio."
- **Later (Regime B north-star):** register OmegaHive as an agent and run the hive for real, with the explicit, falsifiable hypothesis: *coordinated OpenClaw workers beat OpenClaw-solo on RCBench.* If yes, the thesis is validated against the field on real science; if no, that's a real, publishable finding either way.

## 9. Translation mapping (verified against the cloned tasks)

Examined the cloned main repo (`tasks/` + `evaluation/`). The structure makes the mapping concrete.

**A task → an OmegaHive scenario:**

- `task_info.json.task` (the goal — *input / output / scientific targets*) → the planner's `goal.received`. The decomposition into subtasks is *ours* to design — RCBench gives the goal + data, not the multi-agent split.
- `task_info.json.data` (named datasets with rich descriptions) → the workspace inputs the worker(s) reference (artifact refs).
- `target_study/checklist.json` — the rubric — → the **acceptance criteria + graded-outcome structure**. Each task carries **~3–8 weighted items (mean ≈4, weights sum to 1.0; 37/40 include figure-matching `image` items)**, each a real scientific claim with verifiable keywords. A result is scored per item; the *missed* items are the "evidence chain not recovered" — the dominant (substantive) failure, grounded in real criteria.
- `target_study/paper.pdf` = the ground-truth target.

**This sharpens the real/fake split into three realism layers:**

1. **Real outcome structure** — the rubric (≈4 real weighted scientific criteria per task). Available *now* from this clone; grounds the *substantive* outcome even when worker behavior is synthesized.
2. **Synthesized operational behavior** — timeouts / tool-gaps / retries. Freely faked (low-nuance, and underrepresented in real RCBench runs anyway).
3. **Real result artifacts + graded outcome** — from the Home repo (~860 run bundles): each run's `data.json` carries the **per-item scored rubric** (`score.items`: content, weight, 0–100, judge reasoning) plus `cost_usd` / `duration_seconds`, and `workspace/{report/report.md, outputs/*, code/*}` holds the agent's *real* work products. (The `output.json` "trace" turned out to be capped token/cache telemetry — *not* a behavior log — so true trajectory realism would need local re-runs; but we don't need it. The artifacts + itemized scores are the high-fidelity signal.)

So we can build *outcome-grounded* scenarios from the tasks + real scored runs now, without re-running anything.

**OpenClaw calibration (its 40 runs).** Mean **16.6/100**, range **0–47.3**: it fails ≈⅓ of tasks outright (≤10), partially recovers most of the rest, and only twice nears the 50 match-line — high variance. That is the solo baseline a coordinated hive must lift. Concrete per-item example (Astronomy_001, total 36): the two figure items scored 48 and 42, but the MCMC-chain item scored **0** — partly because the workspace shipped only reduced summary data, not the full likelihood set. A clean reminder that a *missed item can be incompetence **or** a data/task limitation* — the attribution problem, live and real. (And a hopeful sign for the thesis: the multi-agent EvoScientist 0.1.1 scored **55.6** on Physics_003 — above the 50 match-line, where every solo agent stays below.)

**Run-for-real registration is one line.** `evaluation/agents.json` maps an agent to `{label, icon, logo, cmd}` with `<PROMPT>` / `<WORKSPACE>` placeholders (OpenClaw = `openclaw agent --agent main --timeout 3600 --message <PROMPT>`; EvoScientist is a multi-line bash wrapper, so multi-agent harnesses fit). OmegaHive registers as one entry whose `cmd` launches the hive on the workspace + prompt and writes `report/report.md`; the harness scores it via the rubric. One caveat: the injected prompt (`instructions_tmpl.py`) is *single-agent-framed* ("no human on the other end," "every response must include a tool call," "never finish early") — OmegaHive wouldn't use it verbatim; we'd feed the goal + data to the **planner** and let the hive's own protocol replace those single-agent guardrails.
