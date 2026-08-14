"""The one non-secret config file this executable reads: the Beastie UI origin.

`setup` (scope item 8) writes it; the server and `doctor` read it. Nothing else is
configurable from outside this file — in particular there is no environment variable
that changes the upstream host, because a tool call or a launcher's env block must
never be able to redirect this process at a fixed origin it was set up against
(order stop-line: "no arbitrary-fetch capability").
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from urllib.parse import urlsplit

_CONFIG_ENV = "OMEGAHIVE_MCP_CONFIG"
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "omegahive-mcp" / "config.json"


class ConfigError(Exception):
    """The config is missing, unreadable, or names an invalid origin."""


def config_path() -> Path:
    """`OMEGAHIVE_MCP_CONFIG` overrides the path — for tests only; the operator setup
    flow never sets it (scope item 8: one fixed, named config path)."""
    override = os.environ.get(_CONFIG_ENV)
    return Path(override) if override else DEFAULT_CONFIG_PATH


def normalize_origin(raw: str) -> str:
    """Validate and canonicalize an operator-pasted origin: `scheme://host[:port]`,
    optionally with a path prefix (Beastie serves the UI at `/omegahive`, per
    `docs/deployments/deployment-0-beastie.md`), never a query or fragment — those
    would be a hidden place for the origin to smuggle a redirect or extra argument."""
    candidate = raw.strip()
    if not candidate:
        raise ConfigError("origin must not be empty")
    parts = urlsplit(candidate)
    if parts.scheme not in ("https", "http"):
        raise ConfigError(f"origin must start with https:// or http://, got {raw!r}")
    if not parts.netloc:
        raise ConfigError(f"origin must include a host, got {raw!r}")
    if parts.query or parts.fragment:
        raise ConfigError(f"origin must not carry a query or fragment, got {raw!r}")
    if parts.username or parts.password:
        raise ConfigError(f"origin must not carry userinfo, got {raw!r}")
    path = parts.path.rstrip("/")
    return f"{parts.scheme}://{parts.netloc}{path}"


def load_config(path: Path | None = None) -> str:
    """The validated origin, or a `ConfigError` naming exactly what is wrong —
    `setup`/`doctor` and the server all raise the same message for the same fault."""
    target = path or config_path()
    if not target.exists():
        raise ConfigError(f"no config at {target} — run `omegahive-mcp setup`")
    try:
        raw = json.loads(target.read_text())
    except json.JSONDecodeError as exc:
        raise ConfigError(f"config at {target} is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("origin"), str):
        raise ConfigError(f"config at {target} is missing a string 'origin' field")
    return normalize_origin(raw["origin"])


def write_config(origin: str, path: Path | None = None) -> Path:
    """Validate, then write `{"origin": ...}` to `path` (0600, parent 0700) — a
    non-secret value (it is the operator's own UI URL) held at file permissions
    that still keep it out of a shared machine's other accounts by default."""
    normalized = normalize_origin(origin)
    target = path or config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    os.chmod(target.parent, stat.S_IRWXU)
    target.write_text(json.dumps({"origin": normalized}, indent=2, sort_keys=True) + "\n")
    os.chmod(target, stat.S_IRUSR | stat.S_IWUSR)
    return target
