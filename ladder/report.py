"""Render the vanilla-half record + interim gate recommendation as markdown (the §8 V4
output). Operates on per-cell `aggregate()` dicts + the frozen config, so it is independent of
the raw rows and easy to test.
"""

from __future__ import annotations

from .gate import completion, evaluate

_CELL_DESC = {
    "L0": "greedy (control)", "L1": "vanilla · strong",
    "L2": "vanilla · cheap", "L3": "vanilla · cheap + KB",
}


def _prunes(a: dict) -> int:
    """Exact prune count. aggregate() now stores `prunes`; fall back to round(rate×n) for older
    records (int(rate×n) truncates — 3/20 stored as 0.15 floats to 2.9999→2)."""
    if "prunes" in a:
        return int(a["prunes"])
    return round(a.get("prune_rate", 0.0) * a.get("n", 0))


def render(aggs: dict[str, dict], models: dict[str, str | None], config: dict) -> str:
    crit = config["criteria"]
    ev = evaluate(aggs, crit)
    g = ev["gate"]
    lines = [
        "# Stage 2 · V4 — vanilla-half record + interim gate recommendation",
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

    lines += ["", "## Knowledge value (L3 vs L2)", ""]
    if "knowledge_value" in ev:
        kv = ev["knowledge_value"]
        ratio = f" (ratio {kv['cost_ratio']:.2f}×)" if kv["cost_ratio"] is not None else ""
        flag = " · **near boundary — replicate**" if kv["needs_replication"] else ""
        verdict = "**supported**" if kv["supported"] else "not supported"
        lines.append(
            f"- {verdict}: completion margin {kv['completion_margin']:+d} seeds "
            f"({kv['completion']}); cost {kv['cost']}{ratio}{flag}")
    else:
        lines.append("- (L2 and L3 not both present)")

    win = aggs[g["winner"]]
    wc, wn, best = completion(win), win.get("n", 0), g["best_completion"]
    if wc < best:
        rec = (f"- **recommended cell: {g['winner']}** — completion {wc}/{wn} (grid-best is "
               f"{best}/{wn}; {g['winner']} chosen within δ={crit['delta_seeds']} by lower "
               f"unconditional cost, then the simpler rung).")
    else:
        rec = (f"- **recommended cell: {g['winner']}** — completion {wc}/{wn} (also the "
               f"grid-best); ties within δ={crit['delta_seeds']} broken by unconditional cost, "
               f"then the simpler rung.")
    lines += [
        "", "## Interim gate recommendation", "", rec,
        f"- contenders within δ: {', '.join(g['contenders'])}",
    ]
    if g["needs_replication"]:
        lines.append("- **a headline contrast lands within ±δ / ±5pp — flag for a 3-replicate "
                     "majority re-run before the call is final (§7)**")
    lines += [
        "",
        "> Vanilla-half only: H-amplifier (L4 vs L1) and the architecture contrast (L4 vs L3) wait "
        "on Track O. The cheap pick is provisional (v0a not yet run); the §5.2 L2/L3 re-run "
        "contingency applies if v0b later replaces it.",
    ]
    return "\n".join(lines)
