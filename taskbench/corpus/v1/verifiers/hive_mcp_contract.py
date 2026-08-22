"""hive-mcp: the read surface's contract, asked of the running app and the built executable.

The order names its own interface precisely — three paths under `/api/v1`, two MCP tools
called `hive_portfolio` and `hive_task`, an executable called `omegahive-mcp`, stdout that
carries protocol only — so every check below is read off the order rather than off the
accepted patch. Nothing here imports a module the historical solution happened to add.

Two seams make this possible without a database or a network:

* the UI application factory already accepted injected factories at the task's baseline,
  and already had a demo mode that serves a whole board from memory. The API routes are
  specified to live on that same application, so the app is built in demo mode and the
  routes are called for real.
* the MCP executable is specified to run over stdio and to register exactly two tools, so
  a plain JSON-RPC handshake on its stdin and stdout answers the inventory question with
  no SDK on this side and no upstream at all.

What is NOT decided here, and is left to the rubric with that stated: whether the freshness
and duration metadata is *truthful* (a wall-clock claim over a simulated run is a defect a
schema check cannot see), whether the error taxonomy really distinguishes unreachable from
invalid TLS, and whether the fixed origin resists a redirect. Those need an upstream the
candidate configures, and the path to that configuration is not something the order fixes.

    uv run --frozen python hive_mcp_contract.py

Runs inside a candidate root. Touches no database, opens no socket, sends nothing outward.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, os.path.abspath("src"))

API = "/api/v1"
HEALTH, PORTFOLIO = f"{API}/health", f"{API}/portfolio"
TOOLS = {"hive_portfolio", "hive_task"}
SCRIPT = "omegahive-mcp"

#: A response is allowed to spell these however it likes; what the order requires is that
#: the fact is present. Each entry is one required fact and the substrings that would
#: name it.
FRESHNESS = {
    "a schema version": ("schema_version", "schemaversion", "version"),
    "the server's observed time": ("observed", "as_of", "asof", "server_time", "generated_at"),
    "the window or filter in force": ("window", "filter", "active", "cut"),
}
PER_RUN = {
    "a per-run cursor": ("cursor",),
    "a per-run generation": ("generation",),
}


def walk_keys(obj, out: set[str]) -> set[str]:
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.add(str(k).lower())
            walk_keys(v, out)
    elif isinstance(obj, list):
        for v in obj:
            walk_keys(v, out)
    return out


def api_cases() -> list[str]:
    findings: list[str] = []
    os.environ["OMEGAHIVE_UI_DEMO"] = "1"
    try:
        from fastapi.testclient import TestClient

        from omegahive.ui.app import create_app
    except Exception as exc:  # noqa: BLE001 — the message is the product
        return [f"FAIL api: could not build the application ({type(exc).__name__}: {exc})"]

    app = create_app()
    client = TestClient(app)

    # Read the route table from the application's own OpenAPI document rather than by
    # walking `app.routes`: a router included with a prefix appears there as an opaque
    # object with no `.path` on current FastAPI, and a route table this check cannot see
    # is a route table it would report as absent.
    try:
        schema_paths: dict = app.openapi().get("paths", {})
    except Exception as exc:  # noqa: BLE001
        return [f"FAIL api: the application could not produce its OpenAPI document ({exc})"]

    api_paths = {p: ops for p, ops in schema_paths.items() if p.startswith(API)}
    if not api_paths:
        return [
            f"FAIL api: the application registers no route under {API}. The order's whole "
            "read surface is three paths there, on this same origin."
        ]

    for path, ops in sorted(api_paths.items()):
        writes = {m.upper() for m in ops} - {"GET", "HEAD", "OPTIONS", "PARAMETERS"}
        if writes:
            findings.append(
                f"FAIL api-read-only: {path} accepts {sorted(writes)}. The order stop-lines "
                "every write capability on this surface."
            )
    if not findings:
        print(f"pass  every {API} route is read-only ({len(api_paths)} route(s))")

    r = client.get(HEALTH)
    if r.status_code == 200:
        print(f"pass  {HEALTH} answers 200")
    else:
        findings.append(f"FAIL api: {HEALTH} answered {r.status_code}")

    r = client.get(PORTFOLIO)
    if r.status_code != 200:
        findings.append(f"FAIL api: {PORTFOLIO} answered {r.status_code}: {r.text[:200]}")
        return findings
    try:
        portfolio = r.json()
    except ValueError:
        return findings + [f"FAIL api: {PORTFOLIO} did not answer JSON"]
    print(f"pass  {PORTFOLIO} answers 200 with JSON")

    keys = walk_keys(portfolio, set())
    for fact, needles in {**FRESHNESS, **PER_RUN}.items():
        if any(any(n in k for n in needles) for k in keys):
            print(f"pass  the portfolio response carries {fact}")
        else:
            findings.append(
                f"FAIL api-freshness: the portfolio response carries no field naming {fact}. "
                "The order requires every response to say when it was observed, what cut it "
                "used, and which cursor and generation each run was read at — a board with "
                "no anchor cannot be told from a stale one."
            )

    run_id, task_id = _first_task(portfolio)
    if run_id is None:
        findings.append(
            "FAIL api: the demo portfolio exposed no run/task pair, so task detail could not "
            "be exercised. The portfolio must return active runs and their active tasks."
        )
        return findings

    detail_path = f"{API}/runs/{run_id}/tasks/{task_id}"
    r = client.get(detail_path)
    if r.status_code == 200:
        detail_keys = walk_keys(r.json(), set())
        print(f"pass  {detail_path} answers 200")
        if any("event" in k for k in detail_keys):
            print("pass  task detail carries a task-specific event timeline")
        else:
            findings.append(
                "FAIL api: task detail carries no event timeline. The order names a bounded, "
                "task-specific one as part of the surface."
            )
    else:
        findings.append(f"FAIL api: {detail_path} answered {r.status_code}: {r.text[:200]}")

    r = client.get(f"{API}/runs/no-such-run/tasks/no-such-task")
    if r.status_code == 404 and r.headers.get("content-type", "").startswith("application/json"):
        print("pass  an unknown run/task returns a typed JSON not-found")
    else:
        findings.append(
            f"FAIL api-not-found: an unknown run/task answered {r.status_code} "
            f"({r.headers.get('content-type')}). The order requires a typed not-found "
            "response, distinguishable from an upstream failure."
        )

    if "/" not in schema_paths:
        findings.append(
            "FAIL api: the existing HTML operator view is gone from the route table; this "
            "order adds a JSON surface beside it, it does not replace it"
        )
    else:
        print("pass  the existing HTML operator view is still served")
    return findings


def _ident(container, *keys) -> str | None:
    """A string identifier under one of `keys`, looking one level into a nested object.

    The response shape is the candidate's to choose — a run may carry `run_id` directly or
    hold an anchor object that carries it — so this looks for the identifier rather than
    for a particular layout, and refuses anything that is not a plain string.
    """
    if not isinstance(container, dict):
        return None
    for key in keys:
        value = container.get(key)
        if isinstance(value, str) and value:
            return value
        if isinstance(value, dict):
            inner = _ident(value, *keys)
            if inner:
                return inner
    return None


def _first_task(portfolio) -> tuple[str | None, str | None]:
    """Find any run/task pair in the response, whatever shape it chose."""
    runs = portfolio if isinstance(portfolio, list) else None
    if runs is None and isinstance(portfolio, dict):
        for key in ("runs", "portfolio", "items", "data"):
            if isinstance(portfolio.get(key), list):
                runs = portfolio[key]
                break
    for run in runs or []:
        if not isinstance(run, dict):
            continue
        rid = _ident(run, "run_id", "run", "id")
        tasks = None
        for key in ("tasks", "active_tasks", "items"):
            if isinstance(run.get(key), list):
                tasks = run[key]
                break
        for task in tasks or []:
            tid = _ident(task, "task_id", "task", "id")
            if rid and tid:
                return rid, tid
    return None, None


def find_mcp_project(root: Path) -> Path | None:
    """The directory whose pyproject declares the executable the order names."""
    for candidate in sorted(root.rglob("pyproject.toml")):
        if ".venv" in candidate.parts or "node_modules" in candidate.parts:
            continue
        try:
            data = tomllib.loads(candidate.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if SCRIPT in (data.get("project", {}).get("scripts") or {}):
            return candidate.parent
    return None


def _rpc(proc, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def mcp_cases(root: Path) -> list[str]:
    findings: list[str] = []
    project = find_mcp_project(root)
    if project is None:
        return [
            f"FAIL mcp: no pyproject.toml in the tree declares a `{SCRIPT}` console script. "
            "The order names that executable and makes installing it the one operator act."
        ]
    print(f"pass  `{SCRIPT}` is declared by {project.relative_to(root)}/pyproject.toml")

    if shutil.which("uv") is None:
        return findings + ["FAIL environment: uv is not on PATH; the MCP legs cannot run"]

    sync = subprocess.run(
        ["uv", "sync", "--group", "dev"], cwd=str(project), capture_output=True, text=True,
        check=False, timeout=1800,
    )
    if sync.returncode != 0:
        return findings + [
            f"FAIL mcp: `uv sync` in {project.name} failed: {sync.stderr.strip()[-500:]}"
        ]

    tests = subprocess.run(
        ["uv", "run", "pytest", "-q"], cwd=str(project), capture_output=True, text=True,
        check=False, timeout=1800,
    )
    if tests.returncode == 0:
        print(f"pass  {project.name}'s own suite is green")
    else:
        findings.append(
            f"FAIL mcp: {project.name}'s own suite fails: {(tests.stdout + tests.stderr)[-800:]}"
        )

    findings += _stdio_cases(project)
    return findings


#: Sub-commands an executable might put its stdio server behind. The order fixes the
#: executable's NAME and its transport and leaves its argv to the implementation, so the
#: client asks the executable what it offers instead of assuming.
_SERVE_WORDS = ("serve", "stdio", "server", "run", "start")


def _serve_argv(project: Path, env: dict[str, str]) -> list[str]:
    """`[]` if the bare executable is the server, else the sub-command it advertises."""
    help_out = subprocess.run(  # noqa: S603 — argv list, shell=False
        ["uv", "run", SCRIPT, "--help"], cwd=str(project), env=env,
        capture_output=True, text=True, check=False, timeout=300,
    )
    text = help_out.stdout + help_out.stderr
    for word in _SERVE_WORDS:
        if f"{word}," in text or f"{word}}}" in text or f" {word} " in text:
            print(f"note  the executable advertises a `{word}` sub-command; using it")
            return [word]
    return []


def _stdio_cases(project: Path) -> list[str]:
    """Start the executable over stdio and ask it what it registers.

    The origin the server points at is operator configuration whose PATH the order does not
    fix, so it is offered through every seam a reasonable implementation might read. If the
    server still refuses to start, that refusal is itself one of the order's requirements —
    the config is validated at startup — and the inventory question moves to the rubric,
    said out loud rather than quietly dropped.
    """
    findings: list[str] = []
    home = Path(tempfile.mkdtemp(prefix="taskbench-mcp-home-"))
    try:
        cfg = {"origin": "http://127.0.0.1:9", "upstream": "http://127.0.0.1:9",
               "url": "http://127.0.0.1:9", "base_url": "http://127.0.0.1:9"}
        paths = [
            home / "mcp.json",
            home / ".config" / "omegahive" / "mcp.json",
            home / ".config" / "omegahive-mcp" / "config.json",
            home / ".omegahive" / "mcp.json",
        ]
        for p in paths:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(cfg))
        env = dict(os.environ)
        env.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "OMEGAHIVE_MCP_CONFIG": str(paths[0]),
                "OMEGAHIVE_MCP_ORIGIN": cfg["origin"],
            }
        )
        argv = ["uv", "run", SCRIPT, *_serve_argv(project, env)]
        proc = subprocess.Popen(  # noqa: S603 — argv list, shell=False
            argv, cwd=str(project), env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        try:
            _rpc(proc, {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "taskbench", "version": "1"},
                },
            })
            _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            proc.stdin.close()
            proc.wait(timeout=180)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            findings.append("FAIL mcp-stdio: the server did not answer a tools/list within 180s")
            return findings
        finally:
            out = proc.stdout.read()
            err = proc.stderr.read()
            proc.stdout.close()
            proc.stderr.close()

        messages = []
        stray = []
        for line in out.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                messages.append(json.loads(line))
            except ValueError:
                stray.append(line)
        if stray:
            findings.append(
                "FAIL mcp-stdout-purity: the server wrote non-protocol lines to stdout "
                f"({stray[0][:120]!r}). stdout is the transport; logs belong on stderr."
            )
        else:
            print("pass  stdout carried protocol only")

        listed = next(
            (m for m in messages if m.get("id") == 2 and "result" in m), None
        )
        if listed is None:
            print(
                "NOTE the server did not return a tool list here — it exited or refused to "
                f"start (exit {proc.returncode}). Startup validation of its own config is "
                "one of the order's requirements, and the path that config lives at is not "
                "fixed by the order, so this is reported and NOT scored. The tool inventory "
                "is a rubric question for this attempt. stderr tail: "
                + (err.strip()[-300:] or "(empty)")
            )
            return findings

        names = {t.get("name") for t in (listed["result"].get("tools") or [])}
        if names == TOOLS:
            print(f"pass  exactly the two tools the order names: {sorted(names)}")
        else:
            findings.append(
                f"FAIL mcp-inventory: the server registers {sorted(names)}; the order names "
                f"exactly {sorted(TOOLS)} and stop-lines every other capability."
            )
        for tool in listed["result"].get("tools") or []:
            if not tool.get("inputSchema"):
                findings.append(
                    f"FAIL mcp-schema: tool {tool.get('name')} declares no input schema"
                )
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return findings


def main() -> int:
    root = Path.cwd()
    findings = api_cases()
    findings += mcp_cases(root)
    for f in findings:
        print(f)
    if findings:
        print(f"\n{len(findings)} contract failure(s)")
        return 1
    print("\nok: the read surface and the executable are what the order specifies")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
