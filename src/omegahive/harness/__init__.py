"""Worker execution harness: the route catalog, adapters, usage, and lifecycle facts.

The boundary this package draws, in one line each:

  records.py   the one record — the operator-owned deployment route catalog
  migrate.py   a v1 catalog becomes a v2 one, without changing what it says
  plan.py      catalog + route + adapter -> one resolved TURN plan, or one refusal
  adapters.py  a route becomes an argv vector; an unknown adapter NAME fails closed
  turns.py     one turn's harness facts + the spine -> one classification, or a refusal
  usage.py     what a harness reported it consumed, or an honest `unavailable`

Nothing here reads a file, a clock, or a database. The shell launcher owns the side
effects — provisioning, the tmux window, running the harness, retaining its stream,
reading the spine — and this package owns the decisions, which is what makes the
no-model preflight and the launch share one code path, and what makes re-classifying a
saved stream and cursor produce byte-identical evidence.
"""

from omegahive.harness.records import (
    CATALOG_SCHEMA_VERSION,
    HIVE_AUTHORITY_ENV_NAMES,
    RefusalError,
    RouteCatalog,
    RouteEntry,
    RunnerSpec,
    catalog_digest,
    is_hive_authority_env,
)

__all__ = [
    "CATALOG_SCHEMA_VERSION",
    "HIVE_AUTHORITY_ENV_NAMES",
    "RefusalError",
    "RouteCatalog",
    "RouteEntry",
    "RunnerSpec",
    "catalog_digest",
    "is_hive_authority_env",
]
