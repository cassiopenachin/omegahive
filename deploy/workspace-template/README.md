# Hive workspace

This is a hive **workspace**: the operator-and-worker side of an OmegaHive deployment.
Orders, reports, questions, decisions, and per-project metrics live here. Code lives in
each project's own code repo (`CODE_REPO` in `projects/<name>/project.conf`).

Seeded by `scripts/hive-init-workspace` from the omegahive repo. The repo ships this
skeleton; the operating doctrine below is yours to author.

## Layout

```
projects/<name>/
  project.conf        project identity: RUN_ID + CODE_REPO (committed, host-independent)
  orders/             one order file per task — YYYY-MM-DD-<task>.md
  reports/            worker result reports
  questions/          worker questions that blocked on a decision
  metrics/            hive-metrics / hive-score output (generated; committed by the tooling)
```

## The hub

This clone's `origin` is a **bare hub** repository on the deployment host. Workers clone
the workspace from that hub and push their reports back to it, so the hub — not this
clone — is the exchange point. `hive-launch` refuses to launch against an order that is
not pushed to the hub, because the worker's fresh clone comes from there.

Point the operator tooling at both with:

```sh
export WS_HUB=<path to the bare hub>     # clone source and push target
export OPS_WS=<path to this clone>       # where the tooling reads orders and confs
```

## What is NOT here yet: the protocol docs

The omegahive repo deliberately ships the bootstrap, not the doctrine. These documents
are the workspace's own and have to be authored (or copied from an existing workspace)
before the loop runs end to end:

| Document | What it governs |
|---|---|
| `projects/<name>/WORKER.md` | the worker protocol — the one file a launched worker session reads and follows |
| `projects/<name>/OPERATIONS.md` | the operator's loop: what to launch, when to answer, when to close |
| `projects/<name>/INDEX.md` | what every other document here is, so a session can find it |
| `projects/<name>/RUNBOOK.md` | recovery procedures (worker death, reassignment, restore) |
| `projects/<name>/decisions.md` | decisions with dates, so a later session does not relitigate them |

A launched worker's kickoff points it at `WORKER.md`. Until that file exists, a worker
has no protocol to follow — author it first, or the loop's first launch is its last.
