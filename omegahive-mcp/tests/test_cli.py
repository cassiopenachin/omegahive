"""`setup`/`doctor`/`serve` orchestration — the parts that are not already exercised
end-to-end by `test_mcp_protocol.py`. `_discover_checkout` is stubbed to `None` in
every test here unless a test is specifically about the install/refresh step: `uv
run pytest` runs with cwd inside this very checkout, so an unstubbed call would
trigger a REAL `uv tool install --reinstall` against the operator's own `uv tool`
state — the one real side effect this whole test suite must never cause by accident.

Any test whose `cmd_setup`/`cmd_doctor` call reaches the smoke-test step spawns a
real `python -m omegahive_mcp serve` subprocess (`_run_smoke_test` uses no fakes —
that is the point, scope item 8 asks for a *real* smoke test). That subprocess reads
its own config fresh from `OMEGAHIVE_MCP_CONFIG`, so those tests set the env var
with `monkeypatch.setenv` rather than only patching `cli.config_path` (which the
subprocess, a separate process, would never see).
"""

from __future__ import annotations

import json
import subprocess

import pytest

from omegahive_mcp import cli
from omegahive_mcp.config import load_config, write_config

from .fake_upstream import FakeUpstream

_real_discover_checkout = cli._discover_checkout


@pytest.fixture(autouse=True)
def _no_real_checkout(monkeypatch):
    monkeypatch.setattr(cli, "_discover_checkout", lambda start: None)


def test_discover_checkout_finds_this_packages_own_pyproject(tmp_path):
    root = tmp_path / "omegahive-mcp"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text('[project]\nname = "omegahive-mcp"\n')
    nested = root / "src" / "omegahive_mcp"
    nested.mkdir()

    assert _real_discover_checkout(nested) == root


def test_discover_checkout_returns_none_when_no_pyproject_matches(tmp_path):
    assert _real_discover_checkout(tmp_path) is None


def test_registration_block_is_the_standard_stdio_shape():
    block = json.loads(cli.registration_block())

    assert block == {
        "mcpServers": {"omegahive-hive": {"command": "omegahive-mcp", "args": ["serve"]}}
    }


def test_setup_writes_config_on_first_run_and_prints_a_registration_block(
    tmp_path, monkeypatch, capsys
):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setenv("OMEGAHIVE_MCP_CONFIG", str(config_path))

    with FakeUpstream() as upstream:
        monkeypatch.setattr("builtins.input", lambda _prompt: upstream.origin)

        code = cli.cmd_setup(None)

    assert code == 0
    assert load_config(config_path) == upstream.origin
    out = capsys.readouterr().out
    assert "mcpServers" in out
    assert "tools: ['hive_portfolio', 'hive_task']" in out


def test_setup_reuses_an_existing_config_without_prompting(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setenv("OMEGAHIVE_MCP_CONFIG", str(config_path))

    with FakeUpstream() as upstream:
        write_config(upstream.origin, config_path)

        def _no_prompt(_prompt):
            raise AssertionError("setup must not prompt when a config already exists")

        monkeypatch.setattr("builtins.input", _no_prompt)

        code = cli.cmd_setup(None)

    assert code == 0


def test_setup_refuses_an_invalid_pasted_origin(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "not-a-url")

    code = cli.cmd_setup(None)

    assert code == 1
    assert not config_path.exists()


def test_setup_fails_cleanly_when_the_upstream_is_unreachable(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setattr("builtins.input", lambda _prompt: "http://127.0.0.1:1")

    code = cli.cmd_setup(None)

    assert code == 1


def test_doctor_refuses_when_unconfigured(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "config_path", lambda: tmp_path / "missing.json")

    code = cli.cmd_doctor(None)

    assert code == 1
    assert "run `omegahive-mcp setup`" in capsys.readouterr().out


def test_doctor_does_not_touch_the_install_or_the_config(tmp_path, monkeypatch):
    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setenv("OMEGAHIVE_MCP_CONFIG", str(config_path))

    def _fail_if_called(checkout):
        raise AssertionError("doctor must never install/refresh")

    monkeypatch.setattr(cli, "_install_or_refresh", _fail_if_called)

    with FakeUpstream() as upstream:
        write_config(upstream.origin, config_path)
        before = config_path.read_text()

        code = cli.cmd_doctor(None)

    assert code == 0
    assert config_path.read_text() == before


def test_setup_runs_install_or_refresh_when_a_checkout_is_found(tmp_path, monkeypatch):
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.setattr(cli, "_discover_checkout", lambda start: checkout)

    calls = []

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="installed\n", stderr="")

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    config_path = tmp_path / "config.json"
    monkeypatch.setattr(cli, "config_path", lambda: config_path)
    monkeypatch.setenv("OMEGAHIVE_MCP_CONFIG", str(config_path))

    with FakeUpstream() as upstream:
        monkeypatch.setattr("builtins.input", lambda _prompt: upstream.origin)

        code = cli.cmd_setup(None)

    assert code == 0
    assert calls == [
        ["uv", "tool", "install", "--reinstall", "--from", str(checkout), "omegahive-mcp"]
    ]


def test_install_or_refresh_reports_a_timeout_instead_of_hanging(tmp_path, monkeypatch):
    def _fake_run(argv, **kwargs):
        assert kwargs.get("timeout") == cli.INSTALL_TIMEOUT_SECONDS
        raise subprocess.TimeoutExpired(argv, kwargs["timeout"])

    monkeypatch.setattr(cli.subprocess, "run", _fake_run)

    ok, detail = cli._install_or_refresh(tmp_path)

    assert ok is False
    assert "timed out" in detail


def test_serve_refuses_cleanly_when_unconfigured(monkeypatch, capsys):
    def _raise(*_a, **_k):
        raise cli.ConfigError("no config")

    monkeypatch.setattr(cli, "load_config", _raise)

    code = cli.cmd_serve(None)

    assert code == 1
    assert "refused" in capsys.readouterr().err
