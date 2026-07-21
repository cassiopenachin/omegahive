"""Render the vanilla-half record's descriptive per-cell table as markdown (the §8 V4
output). Operates on per-cell `aggregate()` dicts + the frozen config, so it is independent of
the raw rows and easy to test.

The §7 decision layer (cross-cell contrasts, the knowledge-value verdict, the interim gate
recommendation, and boundary-replication flagging) was retired with the ladder's closure — see
`ladder/README.md` and `docs/omegahive_stage2_verdict.md`. This module now renders only the
records' descriptive tables; the verdicts they once computed live in git history.
"""

from __future__ import annotations

_CELL_DESC = {
    "L0": "greedy (control)", "L1": "vanilla · strong",
    "L2": "vanilla · cheap", "L3": "vanilla · cheap + KB",
}


def completion(agg: dict) -> int:
    """Completed-seed count (out of n) — the descriptive unit; aggregate stores the rate."""
    return int(round(agg.get("completion_rate", 0.0) * agg.get("n", 0)))


def _prunes(a: dict) -> int:
    """Exact prune count. aggregate() now stores `prunes`; fall back to round(rate×n) for older
    records (int(rate×n) truncates — 3/20 stored as 0.15 floats to 2.9999→2)."""
    if "prunes" in a:
        return int(a["prunes"])
    return round(a.get("prune_rate", 0.0) * a.get("n", 0))


def render(aggs: dict[str, dict], models: dict[str, str | None], config: dict) -> str:
    lines = [
        "# Stage 2 · V4 — vanilla-half record",
        "",
        f"Frozen run-config dated {config['date']} ({config['config_version']}); 20-seed set; "
        f"caps {config['caps']}; USD at the price table dated {config['price_table']['date']}.",
        "",
        "## Per-cell results",
        "",
        "| cell | rung | model | completion | cost USD | decisions (mean) | prunes | false | "
        "premature | loss buckets |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for name in ("L0", "L1", "L2", "L3"):
        a = aggs.get(name)
        if a is None:
            continue
        buckets = a.get("loss_buckets") or {}
        loss = ", ".join(f"{k}×{v}" for k, v in sorted(buckets.items(), key=lambda kv: -kv[1])) \
            or "—"
        lines.append(
            f"| {name} | {_CELL_DESC.get(name, '')} | {models.get(name) or '—'} | "
            f"{completion(a)}/{a.get('n', 0)} | {a.get('cost_usd_total', 0.0):.4f} | "
            f"{a.get('decisions_mean', 0):.1f} | {_prunes(a)} | "
            f"{a.get('false_prunes', 0)} | {a.get('premature_prunes', 0)} | {loss} |")
    return "\n".join(lines)
