# omegahive-mcp

A read-only, vendor-neutral stdio MCP server exposing live `omegahive` hive state
(what runs and tasks exist, their status, blockers, and provenance) to a
bridge-connected seat session — the hive-mcp order's Mac-side half. The other half
is the versioned JSON API in `../src/omegahive/api`, served from Beastie's existing
UI origin; this package is a thin, bounded HTTP client over that API plus an MCP
stdio front end. It holds no database, SSH, gateway, or provider credential, and no
tool it exposes can write to the hive.

## Install and set up (on the operator's Mac)

```sh
git -C ~/src/SNET/omegahive pull          # a merged checkout, per the hive-mcp order
cd ~/src/SNET/omegahive/omegahive-mcp
uv tool install --from . omegahive-mcp     # first install; `setup` below refreshes it later
omegahive-mcp setup
```

`setup` is the one no-argument entry point: it installs/refreshes the executable
when run from inside this checkout, asks for the omegahive UI origin only the first
time (e.g. `https://beastie.<tailnet>.ts.net:8443/omegahive` — paste the same value
Beastie's own `OMEGAHIVE_UI_BASE_URL` uses), validates it, writes it to
`~/.config/omegahive-mcp/config.json` (0600, non-secret — it is a URL, not a
credential), verifies the pinned SDK version and upstream health, runs a real
MCP list-tools/call smoke test over stdio, and prints a ready-to-paste standard
stdio registration block for your MCP client or desktop bridge.

Run `omegahive-mcp doctor` any time afterward to repeat the verify+smoke checks
without touching the installation or the config.

## Tools

- `hive_portfolio()` — every active run and its active tasks, under the same
  active-window/exclusion rules the operator's board applies.
- `hive_task(run_id, task_id)` — one task's full detail: status, owner, blocker
  context, result provenance, and a bounded, newest-first event timeline.

Both are read-only GETs against `GET /api/v1/portfolio` and
`GET /api/v1/runs/{run_id}/tasks/{task_id}`; neither tool, nor any argument to it,
can select a different host, scheme, port, path, or redirect target — the upstream
origin is fixed at `setup` time and never touched by a tool call.

## Development

```sh
uv sync --group dev
uv run pytest
uv run ruff check src tests
uv run mypy src
```

The schema-parity test (`tests/test_schema_parity.py`) imports `../` (the server
package, `omegahive`) as a dev-only dependency to assert `schemas.py` stays a
byte-for-byte field mirror of `omegahive.api.models`. That dependency is never
installed by `uv tool install` (which does not pull dev groups) — the shipped
executable never carries the server's dependencies.
