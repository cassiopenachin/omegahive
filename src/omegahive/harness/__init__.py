"""Worker execution harness: the route catalog, adapters, usage, and lifecycle facts.

The boundary this package draws, in one line each:

  records.py   the one record — the operator-owned deployment route catalog
  migrate.py   a v1 catalog becomes a v2 one, without changing what it says
  plan.py      catalog + route + adapter -> one resolved plan, or one named refusal
  adapters.py  a route becomes an argv vector; an unknown adapter NAME fails closed
  spool.py     the supervised worker's request/receipt shapes
  usage.py     what a harness reported it consumed, or an honest `unavailable`

Nothing here reads a file, a clock, or a database. The shell launcher and the
supervisor own the side effects; this package owns the decisions, which is what makes
the no-model preflight and the launch share one code path.
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
