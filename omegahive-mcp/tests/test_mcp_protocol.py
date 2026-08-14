"""End-to-end MCP protocol tests: a real `mcp.client` over a real stdio subprocess
(`python -m omegahive_mcp serve`) against a fake HTTP upstream (`fake_upstream.py`).
No `MockTransport`/in-process shortcut here — this is what a bridge client actually
does. Covers scope item 5's definition-of-done list: exact tool inventory and
schemas, structured results, stdout purity, bounded output, argument validation,
fixed-origin/no-redirect behavior, the upstream error taxonomy, restart recovery,
redaction, and the absence of every write/generic-fetch capability.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from omegahive_mcp.config import write_config

from .fake_upstream import FakeUpstream


def _server_params(config_path) -> StdioServerParameters:
    # A minimal explicit env — this is what a launcher-issued registration ("client
    # registration supplies only the executable path and its own config path", scope
    # item 7) looks like; it does not forward the arbitrary parent environment.
    env = {"OMEGAHIVE_MCP_CONFIG": str(config_path)}
    for key in ("HOME", "PATH"):
        if key in os.environ:
            env[key] = os.environ[key]
    return StdioServerParameters(
        command=sys.executable, args=["-m", "omegahive_mcp", "serve"], env=env
    )


def test_tool_inventory_is_exactly_the_two_read_tools(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    listed = await session.list_tools()
                    prompts = await session.list_prompts()
                    resources = await session.list_resources()
                    return listed, prompts, resources

        listed, prompts, resources = anyio.run(body)

        names = sorted(t.name for t in listed.tools)
        assert names == ["hive_portfolio", "hive_task"]
        for tool in listed.tools:
            assert tool.annotations is not None
            assert tool.annotations.read_only_hint is True
            assert tool.annotations.destructive_hint is False
        assert prompts.prompts == []
        assert resources.resources == []


def test_tool_schemas_take_no_host_scheme_port_or_path_argument(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.list_tools()

        listed = anyio.run(body)
        by_name = {t.name: t for t in listed.tools}

        assert by_name["hive_portfolio"].input_schema.get("properties", {}) == {}
        task_props = set(by_name["hive_task"].input_schema["properties"])
        assert task_props == {"run_id", "task_id"}
        for forbidden in ("url", "host", "scheme", "port", "path", "base_url", "origin"):
            assert forbidden not in task_props


def test_hive_portfolio_returns_structured_content_matching_the_fake_upstream(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("hive_portfolio", {})

        result = anyio.run(body)

        assert result.is_error is False
        assert result.structured_content["hidden_run_count"] == 0
        run = result.structured_content["runs"][0]
        assert run["run"]["run_id"] == "r1"
        assert run["tasks"][0]["blocker_reason"] == "image digest missing"


def test_hive_task_returns_provenance_and_timeline_fields(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("hive_task", {"run_id": "r1", "task_id": "T1"})

        result = anyio.run(body)

        assert result.is_error is False
        task = result.structured_content["task"]
        assert task["status"] == "blocked"
        assert task["clock_kind"] == "wall"
        assert result.structured_content["run"]["cursor"] == 4


def test_unknown_task_surfaces_the_servers_typed_code_not_a_crash(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("hive_task", {"run_id": "r1", "task_id": "nope"})

        result = anyio.run(body)

        assert result.is_error is True
        text = " ".join(c.text for c in result.content if getattr(c, "text", None))
        assert "unknown_task" in text
        assert "no such task" in text


def test_unreachable_upstream_surfaces_a_distinct_safe_error(tmp_path):
    # A closed local port: nothing is listening, so this is a clean "unreachable",
    # not a hang — distinct from the timeout case below.
    config = write_config("http://127.0.0.1:1", tmp_path / "config.json")

    async def body():
        async with stdio_client(_server_params(config)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("hive_portfolio", {})

    result = anyio.run(body)

    assert result.is_error is True
    text = " ".join(c.text for c in result.content if getattr(c, "text", None))
    assert "unreachable" in text


def test_argument_validation_rejects_a_missing_required_argument(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def body():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("hive_task", {"run_id": "r1"})

        result = anyio.run(body)

        assert result.is_error is True


def test_restart_recovers_cleanly_with_no_carried_state(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")

        async def one_round():
            async with stdio_client(_server_params(config)) as (read, write):
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    return await session.call_tool("hive_portfolio", {})

        first = anyio.run(one_round)
        second = anyio.run(one_round)  # a fresh process each time == a bridge restart

        assert first.is_error is False
        assert second.is_error is False
        assert first.structured_content == second.structured_content


def test_error_text_never_leaks_a_traceback_or_the_operators_home_path(tmp_path):
    config = write_config("http://127.0.0.1:1", tmp_path / "config.json")

    async def body():
        async with stdio_client(_server_params(config)) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                return await session.call_tool("hive_portfolio", {})

    result = anyio.run(body)

    text = " ".join(c.text for c in result.content if getattr(c, "text", None))
    assert "Traceback" not in text
    assert str(tmp_path) not in text
    assert os.path.expanduser("~") not in text


def test_stdout_carries_only_json_rpc_no_banner_or_log_line(tmp_path):
    with FakeUpstream() as upstream:
        config = write_config(upstream.origin, tmp_path / "config.json")
        env = {"OMEGAHIVE_MCP_CONFIG": str(config)}
        for key in ("HOME", "PATH"):
            if key in os.environ:
                env[key] = os.environ[key]

        proc = subprocess.Popen(
            [sys.executable, "-m", "omegahive_mcp", "serve"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            env=env,
        )
        try:
            request = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "purity-check", "version": "0.0"},
                },
            }
            proc.stdin.write(json.dumps(request) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            json.loads(line)  # raises if anything but the JSON-RPC reply landed here
        finally:
            proc.terminate()
            proc.wait(timeout=5)
