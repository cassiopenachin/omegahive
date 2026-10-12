# OmegaHive — repository briefing

This repository is the **code** for OmegaHive: a coordination substrate for running one
long-lived hive of agents across many projects, built on an append-only Postgres event log.
Start with [README.md](README.md) for what it is and how to run it, and
[docs/INDEX.md](docs/INDEX.md) for everything else.

The **workspace** repository is a separate thing and lives elsewhere: orders, reports,
questions and operating docs are kept there, not here. If you were launched as a hive worker,
your protocol is in that workspace, not in this file.

## This repository is public

The remote is `github.com/cassiopenachin/omegahive` and it is **public**. Every push, branch,
pull request, issue and comment is publication to a public surface. Before adding anything —
benchmark records, logs, transcripts, configuration examples, spend figures — assume a
stranger will read it, and check it carries no credentials, no personal data, and nothing
belonging to a private repository.

## Git protocol

Work on a **task branch**, commit at each milestone rather than in one lump at the end, then
**push the branch and open a pull request**. Do not merge into `main` locally, and do not
push or open a PR unless you have been asked to.

This differs from other repositories on this host, so do not carry a habit in from one of
them:

| repository | protocol |
|---|---|
| **this one (omegahive)** | task branch → push → PR; never a local merge to `main` |
| hive workspace | commit to `main` directly; no branch or PR ceremony |
| `~/src/dotfiles` | commit to `main` directly; no branch or PR ceremony |
| other personal projects | task branch → local merge → push `main` |

Upstream SingularityNET repositories are never a push destination. Work goes to the
operator's own fork under `github.com/cassiopenachin`.

## Working in this repository

- **Python** is managed with `uv` — `uv run` to execute, `uv add` for dependencies. The
  project pins `>=3.12,<3.13`. Never install packages globally with `pip`.
- **Containers** on this host are `podman`, never `docker`.
- **Decisions land in committed files**, not only in chat or terminal output. A conclusion
  that exists only in a transcript did not happen.
- **Run the tests** before proposing a change is finished: `uv run pytest`.

## Canonical checkout

The canonical checkout is `~/src/SNET/omegahive`. Per-session and per-worker clones under
`~/work/` are disposable scratch copies of this same repository — work meant to last belongs
on a branch pushed from a checkout you intend to keep. Identify this repository by its
remote, not by its path.

---

*`AGENTS.md` and `CLAUDE.md` at this root are **byte-identical twins**, because different
agent harnesses auto-load different filenames and a session must not get a different briefing
depending on its vendor. Edit both or neither; a divergence here is a defect, not a
customization.*
