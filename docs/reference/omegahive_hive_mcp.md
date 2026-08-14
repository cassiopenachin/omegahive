# The hive MCP — live, read-only hive state through the desktop bridge

**Status:** v1, shipped (hive-mcp order). Two components: a versioned JSON API on
Beastie's existing UI origin (`src/omegahive/api/`), and a standalone stdio MCP
server for the operator's Mac (`omegahive-mcp/`). Governed by the operator decision
un-refusing and scoping the hive MCP (workspace `projects/omegahive/decisions.md`,
2026-08-12) and the settled Mac→tailnet→Beastie shape (workspace
`projects/omegahive/capacity-routing.md`).

## 1. What this replaces, and what it does not

Before this: a cloud seat inspected the live board only through a browser bridge
that scrapes rendered HTML, needs both the operator's Mac and browser extension
alive, and has produced incomplete text extraction — or an operator paste. Both
still work and remain the documented fallback.

After this: a bridge-connected seat calls two MCP tools — `hive_portfolio` and
`hive_task` — and gets structured, versioned JSON instead of scraped HTML. This
removes the browser/HTML dependency. It does **not** remove the physical-machine
dependency: the MCP process still needs the operator's Mac and desktop bridge
running, and Beastie still needs to be reachable over the tailnet. If either is
asleep, the fallback (browser bridge, or an operator paste) is the answer — that is
a known property of the chosen bridge path, not a defect to repair with a public
listener.

## 2. Architecture

```
 cloud seat  --(MCP: stdio)-->  desktop bridge  --(spawns, stdio)-->  omegahive-mcp
                                  (operator's Mac)                    (this checkout)
                                                                            |
                                                                    (HTTPS, fixed origin,
                                                                     tailnet, bounded client)
                                                                            v
                                                                 Beastie UI service (loopback,
                                                                 behind house Caddy :8443)
                                                                   FastAPI app
                                                                   /portfolio, /run/*  (HTML)
                                                                   /api/v1/*           (JSON)
                                                                            |
                                                                    HiveCoordinatorPort
                                                                     (hive_reader DSN)
                                                                            v
                                                                        Postgres
```

- **The JSON API** (`src/omegahive/api/`) is mounted on the *same* `FastAPI` app
  the HTML UI already serves (`ui.app.create_app`), through the *same* read-service
  seam (`report.reader`) the HTML board and the portfolio CLI already call. There is
  one fold site (the port's), one active-window rule (`report.portfolio`), one place
  DTOs are built from board state (`api.service`) — the HTML board and
  `GET /api/v1/portfolio` can never drift into two different projections of the same
  run.
- **The MCP process** (`omegahive-mcp/`) is a fully standalone Python project — its
  own `pyproject.toml`/`uv.lock`, no dependency on `omegahive`'s runtime deps
  (psycopg, fastapi, litellm). It holds no database URL, SSH key, hive actor
  credential, or provider API key. Its only state is one non-secret config file
  naming the Beastie UI origin. It talks to Beastie over HTTPS, through the same
  tailnet route the browser bridge already used, and is launched by the desktop
  bridge over stdio — the bridge owns the process's lifetime, exactly as it already
  does for Bear, Spotify, and Control_Chrome (per the operator decision).

## 3. Threat-boundary table

| Boundary | What crosses it | What cannot cross it |
|---|---|---|
| Cloud seat → desktop bridge | An MCP tool call (`hive_portfolio` or `hive_task(run_id, task_id)`) | Any other tool name, a host/scheme/port/path argument, an event, a write operation |
| Desktop bridge → `omegahive-mcp` process | Process spawn (stdio), a minimal env (the bridge's own registration; the SDK's default stdio launch does **not** forward the arbitrary parent environment — only `HOME`/`LOGNAME`/`PATH`/`SHELL`/`TERM`/`USER` unless a client explicitly sets `env`) | A database URL, SSH key, hive actor credential, API/model key, or reverse-proxy credential — none of these are ever passed, because none exist on this side of the boundary |
| `omegahive-mcp` → Beastie | One `GET` to one of three fixed paths (`/api/v1/health`, `/api/v1/portfolio`, `/api/v1/runs/{run_id}/tasks/{task_id}`) against one operator-configured, non-secret origin, over HTTPS with normal certificate verification, bounded connect/read timeouts, no redirect followed, no cache | A different host/scheme/port/path (there is no code path that accepts one — `client.py` builds every URL from the fixed origin plus a fixed template; `run_id`/`task_id` are percent-encoded into their own path segment so neither can smuggle a `/` into the request), any non-`GET` method, any credential |
| Beastie UI service → Postgres | Reads via `HiveCoordinatorPort` under the `hive_reader` role (the same connection the HTML UI and `board-view --json` already use) | Any INSERT/UPDATE/DELETE — `hive_reader` is structurally incapable of writing (migration 0003, two-role scheme) |

No endpoint or tool in this order's scope accepts an event, operation, SQL,
filesystem ref, shell command, or arbitrary upstream URL. Event payload text, refs,
filenames, and URLs are returned only as `payload: dict` data — nothing in the API
or the MCP server interprets them as instructions.

## 4. Deployment facts

- **Same service, same route.** The JSON API is mounted on the existing UI
  `FastAPI` app (`app.include_router(build_api_router(...))` in `ui.app.create_app`),
  so it inherits deployment #0's existing loopback publish
  (`127.0.0.1:8811` → container `8000`) and the house Caddy reverse-proxy route at
  `https://<host>:8443/omegahive` (see `docs/deployments/deployment-0-beastie.md`).
  No new container, port, secret, or access-control change — `docker-compose.yml`
  and `secrets-manifest.yaml` are untouched by this order.
- **Same credential.** `GET /api/v1/*` reads through `report.reader.database_port`/
  `database_healthcheck`, both built on `db.connect()` — the same `hive_reader` DSN
  the HTML UI already uses (`OMEGAHIVE_DATABASE_URL`). No gateway/INSERT credential
  reaches the API layer.
- **The origin an operator pastes into `omegahive-mcp setup`** is the same value
  already exported as `OMEGAHIVE_UI_BASE_URL` on Beastie's operator shell, e.g.
  `https://beastie.<tailnet-name>.ts.net:8443/omegahive` — deployment #0's actual
  value, unchanged by this order, is the one to paste.

## 5. Setup (operator, on the Mac)

See `omegahive-mcp/README.md` for the full walkthrough; summary:

```sh
git -C ~/src/SNET/omegahive pull
cd ~/src/SNET/omegahive/omegahive-mcp
uv tool install --from . omegahive-mcp
omegahive-mcp setup
```

`setup` installs/refreshes the executable (when run from this checkout), asks for
the UI origin only the first time, validates and writes it to
`~/.config/omegahive-mcp/config.json` (0600), verifies the pinned SDK version and
upstream health, runs a real MCP list-tools/call smoke test over stdio against its
own just-installed `serve` command, and prints the standard stdio registration
block:

```json
{
  "mcpServers": {
    "omegahive-hive": { "command": "omegahive-mcp", "args": ["serve"] }
  }
}
```

Paste that block into the desktop bridge's MCP registration (the same standard
shape Claude Desktop's `claude_desktop_config.json` and Claude Code's `.mcp.json`
accept — no vendor-specific fork). `omegahive-mcp doctor` repeats the verify+smoke
checks at any time without touching the installation or the config.

## 6. Designed operator round-trip (scope item 9 — not executed by a worker session)

This proves the bridge path end-to-end and can only be run by the operator, from
their own Mac and desktop bridge — a Beastie-resident worker session has neither.
**Record the result of this section here, appended, once run**, per the order's
definition of done ("tool inventory, redacted registration shape, observed
time/cursor/generation, direct-read comparison, restart result, and exact operator
steps").

1. Merge this PR and deploy from `main` per the ordinary operator deploy procedure.
2. On the Mac: `git pull`, then run §5's setup, and register the printed block in
   the desktop bridge.
3. From a **fresh** cloud coordinator session with the bridge connected: list the
   two tools, call `hive_portfolio`, then call `hive_task` on one returned task.
4. Record: the tool inventory returned, the registration block with the origin
   redacted (host only, e.g. `https://<beastie-host>:8443/omegahive`), the
   `observed_at`/`cursor`/`generation` the call returned, and a direct comparison —
   `omegahive board-view <run> --json` or the UI board for the same run at the same
   moment (or naming any events that landed in the gap between the two reads).
5. Restart the desktop bridge (or the registered MCP client) once and repeat step 3
   — the result should be identical modulo any real board change, proving no hidden
   in-process cache or session state (the MCP process holds none; a restart is a
   fresh process, fresh `HiveApiClient`, fresh config read).
6. If the bridge cannot launch a standard stdio process, or the UI origin is not
   reachable outside a browser session, report that as a bridge incompatibility —
   the documented fallback (browser bridge, or an operator paste) remains available;
   this is not repaired with a vendor-private protocol or a new network listener.

*(Round-trip record: pending — append here once the operator has run the six steps
above.)*

## 7. Security notes on the pinned SDK

Pinned: `mcp==2.0.0` (`omegahive-mcp/uv.lock`), the current stable release line —
not a pre-release (`2.0.0a1`/beta tags exist upstream and are explicitly not used).
Advisories checked at authoring time:

- **CVE-2026-59950** (WebSocket origin-validation error in the SDK's deprecated
  `websocket_server` transport, fixed 1.28.1): not applicable — this server only
  ever calls `MCPServer.run("stdio")`; the websocket transport is never imported or
  registered.
- **CVE-2026-52870** (experimental task-handler authorization bypass, versions
  ≥1.23.0 <1.27.2, fixed 1.27.2): not applicable — `mcp==2.0.0` postdates the fix,
  and this server registers no prompts, resources, sampling, or the tasks
  extension (SEP-2663 tasks are not in this SDK release at all).
- **Shell-injection class reported against MCP stdio *clients*** (a client that
  shell-interprets an untrusted configured command string): not applicable to this
  server's own code — `mcp.client.stdio.stdio_client` (used only by
  `omegahive-mcp`'s own tests and its `setup`/`doctor` smoke test) spawns via
  `anyio`'s subprocess API with an explicit argv list, never `shell=True`, and the
  command/args this project ever spawns are its own fixed
  `[sys.executable, "-m", "omegahive_mcp", "serve"]` — never a string built from
  untrusted input.

Re-check this section (and the pin) before any future bump of the `mcp` dependency.

## 8. The `capacity-view` extension point (scope item 10)

`capacity-view` (sequenced after `worker-harness`, per `capacity-routing.md`) can
add its own read surface through the same two seams this order built, without
touching this order's DTOs or tools:

- **API side:** add a new Pydantic response model to `src/omegahive/api/models.py`
  (its own `Meta`-carrying shape — do not reuse `PortfolioResponse`/
  `TaskDetailResponse`), a DTO builder function in `src/omegahive/api/service.py`
  reading whatever new spine facts `worker-harness` lands (execution identity,
  token/cost consumption), and one new route in `src/omegahive/api/routes.py` —
  e.g. `GET /api/v1/capacity`. Regenerate `docs/reference/omegahive_api_schema.md`
  (`scripts/emit_api_schemas.py`).
- **MCP side:** mirror the new response model into
  `omegahive-mcp/src/omegahive_mcp/schemas.py` (the same hand-synced-with-a-parity-
  test pattern this order established), add one `client.py` method (`GET` against
  the new fixed path, same bounded-client rules), and register one new
  `@server.tool()` — e.g. `hive_capacity` — in `server.py`, read-only annotated,
  alongside the existing two.

Explicitly out of scope for that later order to inherit from this one: dynamic tool
registration, a generic HTTP tool, or coupling its acceptance to this order's own
DoD. `capacity-view` conforms to *no* shape this order defined — it adds a sibling
tool/route, never reuses `hive_portfolio`/`hive_task`'s DTOs for a different fact
domain.
