"""`omegahive-mcp setup | doctor | serve` (hive-mcp order, scope item 8).

`setup` is the one no-argument, Mac-side operator act this order asks for: it
installs/refreshes the executable when run from a merged checkout, asks for the UI
origin only on first use, verifies the pinned SDK version and upstream health, runs
a real MCP list-tools/call smoke test against the just-installed `serve` command,
and prints a ready-to-paste standard stdio registration block. `doctor` repeats the
verify+smoke steps without touching the installation or the config.

None of the three subcommands discovers network state (no Tailscale call, no port
scan) or edits any file outside this package's own config path.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.types import TextContent

from . import __version__
from .client import HiveApiClient, UpstreamError
from .config import ConfigError, config_path, load_config, normalize_origin, write_config

PACKAGE_NAME = "omegahive-mcp"
SDK_PACKAGE_NAME = "mcp"


def _print(*args: object) -> None:
    print(*args)  # noqa: T201 - setup/doctor/serve(before stdio) are operator-facing CLI output


def _discover_checkout(start: Path) -> Path | None:
    """Walk up from `start` looking for this package's own `pyproject.toml` — "a
    merged checkout" (scope item 8). An installed-but-not-checked-out invocation
    (the common case for `doctor`, or `setup` run from an unrelated directory)
    returns None, and the install/refresh step is skipped rather than guessed at."""
    for candidate in (start, *start.parents):
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and f'name = "{PACKAGE_NAME}"' in pyproject.read_text():
            return candidate
    return None


INSTALL_TIMEOUT_SECONDS = 120.0


def _install_or_refresh(checkout: Path) -> tuple[bool, str]:
    """`uv tool install --reinstall --from <checkout> omegahive-mcp` — the one
    install-mutating step, and only `setup` calls it. Bounded: a stalled resolve
    against a flaky tailnet must fail loudly, not hang `setup` forever."""
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell, no caller-supplied input
            ["uv", "tool", "install", "--reinstall", "--from", str(checkout), PACKAGE_NAME],
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {INSTALL_TIMEOUT_SECONDS:.0f}s"
    ok = result.returncode == 0
    output = (result.stdout + result.stderr).strip()
    return ok, output


def _installed_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


@dataclass
class SmokeResult:
    ok: bool
    tool_names: list[str]
    detail: str


def _smoke_test_env() -> dict[str, str]:
    """The env for the spawned `serve` subprocess. `StdioServerParameters(env=None)`
    would fall back to the SDK's own minimal-inherit default (HOME/PATH/etc, no
    `OMEGAHIVE_MCP_CONFIG`) — safe for a real operator run (`setup` just wrote the
    *default* config path), but silently wrong the moment `OMEGAHIVE_MCP_CONFIG` is
    set, because the smoke-tested subprocess would then read a different config than
    the one this same process just verified. Forwarding it explicitly keeps the two
    in sync always, not only in the common case."""
    env = {key: os.environ[key] for key in ("HOME", "PATH") if key in os.environ}
    override = os.environ.get("OMEGAHIVE_MCP_CONFIG")
    if override:
        env["OMEGAHIVE_MCP_CONFIG"] = override
    return env


async def _run_smoke_test() -> SmokeResult:
    """A real MCP client, over real stdio, against `python -m omegahive_mcp serve`
    running in this same environment — the exact command the printed registration
    block tells a bridge client to run. Lists tools, then calls `hive_portfolio` for
    real (read-only, so a live smoke test is not a load-bearing risk)."""
    params = StdioServerParameters(
        command=sys.executable, args=["-m", "omegahive_mcp", "serve"], env=_smoke_test_env()
    )
    try:
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                names = sorted(t.name for t in listed.tools)
                result = await session.call_tool("hive_portfolio", {})
                if result.is_error:
                    text = "; ".join(c.text for c in result.content if isinstance(c, TextContent))
                    return SmokeResult(False, names, f"hive_portfolio returned an error: {text}")
                return SmokeResult(True, names, "hive_portfolio: ok")
    except Exception as exc:  # noqa: BLE001 - any smoke-test fault is reported, not raised
        return SmokeResult(False, [], f"{type(exc).__name__}: {exc}")


def registration_block() -> str:
    """The standard stdio registration stanza (scope item 5's portability boundary:
    "Standard MCP plus printed registration"). Every MCP-aware host — Claude
    Desktop's `claude_desktop_config.json`, Claude Code's `.mcp.json`, and any
    standard-stdio-capable desktop bridge — accepts this same `command`+`args`
    shape; only where it is pasted differs."""
    return json.dumps(
        {"mcpServers": {"omegahive-hive": {"command": "omegahive-mcp", "args": ["serve"]}}},
        indent=2,
    )


def _verify(origin: str) -> tuple[bool, str]:
    with HiveApiClient(origin) as client:
        try:
            health = client.health()
        except UpstreamError as exc:
            return False, f"upstream health check failed ({exc.code}): {exc.message}"
    return True, f"upstream ok, schema version {health.schema_version}, database {health.database}"


def _report_versions() -> None:
    mcp_version = _installed_version(SDK_PACKAGE_NAME)
    _print(f"omegahive-mcp version: {__version__}")
    _print(f"mcp SDK version: {mcp_version or 'NOT INSTALLED'}")


def cmd_setup(_args: argparse.Namespace) -> int:
    checkout = _discover_checkout(Path.cwd())
    if checkout is not None:
        _print(f"merged checkout found at {checkout} — installing/refreshing {PACKAGE_NAME}")
        ok, output = _install_or_refresh(checkout)
        _print(output)
        if not ok:
            _print("install/refresh failed; fix the error above and re-run `setup`")
            return 1
    else:
        _print(
            "not run from a merged checkout — skipping install/refresh. "
            f"Run `{PACKAGE_NAME} setup` from inside the checked-out omegahive-mcp/ "
            "directory (after `git pull`) to install or refresh the executable."
        )

    path = config_path()
    try:
        origin = load_config(path)
        _print(f"using existing config at {path}: {origin}")
    except ConfigError:
        _print(f"no config at {path} yet.")
        raw = input("Paste the omegahive UI origin (e.g. https://<host>:8443/omegahive): ")
        try:
            origin = normalize_origin(raw)
        except ConfigError as exc:
            _print(f"refused: {exc}")
            return 1
        write_config(origin, path)
        _print(f"wrote {path}")

    _report_versions()
    ok, detail = _verify(origin)
    _print(detail)
    if not ok:
        return 1

    _print("running an MCP list-tools/call smoke test over stdio...")
    smoke = anyio.run(_run_smoke_test)
    _print(f"tools: {smoke.tool_names}")
    _print(smoke.detail)
    if not smoke.ok:
        return 1

    _print("\nsetup complete. Paste this into your MCP client's config:\n")
    _print(registration_block())
    return 0


def cmd_doctor(_args: argparse.Namespace) -> int:
    path = config_path()
    try:
        origin = load_config(path)
    except ConfigError as exc:
        _print(f"refused: {exc}")
        return 1
    _print(f"config: {path} -> {origin}")

    _report_versions()
    ok, detail = _verify(origin)
    _print(detail)
    if not ok:
        return 1

    _print("running an MCP list-tools/call smoke test over stdio...")
    smoke = anyio.run(_run_smoke_test)
    _print(f"tools: {smoke.tool_names}")
    _print(smoke.detail)
    return 0 if smoke.ok else 1


def cmd_serve(_args: argparse.Namespace) -> int:
    """Run the stdio MCP server. stdout is the JSON-RPC stream from this point on —
    every diagnostic goes to stderr (scope item 5: "stdout is MCP protocol only")."""
    logging.basicConfig(
        level=logging.INFO, stream=sys.stderr, format="%(levelname)s %(name)s: %(message)s"
    )
    try:
        origin = load_config()
    except ConfigError as exc:
        print(f"refused: {exc}. Run `{PACKAGE_NAME} setup` first.", file=sys.stderr)
        return 1

    from .server import build_server

    client = HiveApiClient(origin)
    try:
        server = build_server(client)
        server.run("stdio")
    finally:
        client.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=PACKAGE_NAME)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("setup", help="install/refresh, configure, verify, and smoke-test")
    subparsers.add_parser("doctor", help="repeat the verify+smoke checks; no install/config change")
    subparsers.add_parser("serve", help="run the stdio MCP server (what a bridge client launches)")

    args = parser.parse_args(argv)
    handler = {"setup": cmd_setup, "doctor": cmd_doctor, "serve": cmd_serve}[args.command]
    return handler(args)
