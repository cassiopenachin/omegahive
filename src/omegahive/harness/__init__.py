"""Worker execution harness: routes, bindings, adapters, usage, and the lifecycle facts.

The boundary this package draws, in one line each:

  records.py   the two records — a deployment route catalog, a committed launch binding
  plan.py      binding + catalog + adapter -> one resolved plan, or one named refusal
  adapters.py  a route becomes an argv vector; unknown harnesses fail closed
  usage.py     what a harness reported it consumed, or an honest `unavailable`

Nothing here reads a file, a clock, or a database. The shell launcher and the
supervisor own the side effects; this package owns the decisions, which is what makes
the no-model preflight and the launch share one code path.
"""

from omegahive.harness.records import (
    BINDING_SCHEMA_VERSION,
    CATALOG_SCHEMA_VERSION,
    LaunchBinding,
    RefusalError,
    RouteCatalog,
    RouteEntry,
    catalog_digest,
)

__all__ = [
    "BINDING_SCHEMA_VERSION",
    "CATALOG_SCHEMA_VERSION",
    "LaunchBinding",
    "RefusalError",
    "RouteCatalog",
    "RouteEntry",
    "catalog_digest",
]
