"""`python -m omegahive_mcp <setup|doctor|serve>` — identical to the `omegahive-mcp`
console script, and what the smoke test in `cli.py` actually spawns (independent of
whether the console-script shim is on PATH)."""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
