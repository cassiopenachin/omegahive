"""omegahive-mcp: a vendor-neutral, read-only local MCP server bridging a
bridge-connected seat session to one operator's Beastie hive, over stdio.

Phase-1 scoped (hive-mcp order): two structured-output tools (`hive_portfolio`,
`hive_task`), no write path, no resource/prompt/sampling registration, fixed to one
operator-configured origin (`config.py`) reached only via the bounded HTTP client in
`client.py`. See `cli.py` for the `setup`/`doctor`/`serve` entry points.
"""

from __future__ import annotations

__version__ = "0.1.0"


def main() -> int:
    from .cli import main as _main

    return _main()


__all__ = ["__version__", "main"]
