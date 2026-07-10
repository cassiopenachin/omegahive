"""The §7 decision layer for the vanilla half of the grid: cross-cell contrasts, the
knowledge-value verdict (L3 vs L2), the interim gate chain over L0–L3, and the
boundary-replication flag. All operate on per-cell `aggregate()` dicts; the criteria come
from the frozen run-config.

§7 rules (pinned): completion margin **δ = 2 seeds (0.10)**; "cheaper" = **≤ 0.8×** cost;
"cost ≈" = within **±15%**; gate = best completion → tie-within-δ → lowest unconditional cost →
simpler/cheaper rung (time-to-prune is NOT in the chain); boundary = a contrast within ±δ
completion or ±5pp of a cost threshold → flag for a 3-replicate majority re-run.
"""

from __future__ import annotations

# simpler/cheaper rung order for the gate's final tie-break (lower = simpler).
_RUNG_ORDER = {"L0": 0, "L2": 1, "L3": 2, "L1": 3}


def completion(agg: dict) -> int:
    """Completed-seed count (out of n) — the §7 unit; aggregate stores the rate."""
    return int(round(agg.get("completion_rate", 0.0) * agg.get("n", 0)))


def _cost(agg: dict) -> float:
    return float(agg.get("cost_usd_total", 0.0))


def _near_boundary(margin: int, cost_ratio: float | None, crit: dict) -> bool:
    delta = crit["delta_seeds"]
    if abs(margin) <= delta:                                   # within the tie margin
        return True
    if cost_ratio is not None:
        edge = crit.get("boundary_cost_pp", 5) / 100.0
        # the actual cost-classification edges contrast() uses: "cheaper" (≤0.8) and the top of
        # the "approx" band (1+cost_approx=1.15). 1−cost_approx (0.85) is NOT an edge — it sits
        # inside the approx band — so flagging near it would call spurious replications.
        thresholds = (crit["cheaper"], 1 + crit["cost_approx"])
        if any(abs(cost_ratio - t) <= edge for t in thresholds):
            return True
    return False


def contrast(a: dict, b: dict, crit: dict) -> dict:
    """Compare cell a vs cell b. `completion_margin` is in seeds (a − b); `cost_ratio` is
    a_cost / b_cost (None when b is free, e.g. L0)."""
    margin = completion(a) - completion(b)
    delta = crit["delta_seeds"]
    ratio = (_cost(a) / _cost(b)) if _cost(b) > 0 else None
    if margin > delta:
        comp = "a_better"
    elif margin < -delta:
        comp = "b_better"
    else:
        comp = "tie"
    if ratio is None:
        cost = "a_costlier" if _cost(a) > 0 else "equal"
    elif ratio <= crit["cheaper"]:
        cost = "a_cheaper"
    elif ratio <= 1 + crit["cost_approx"]:
        cost = "approx"
    else:
        cost = "a_costlier"
    return {"completion_margin": margin, "completion": comp, "cost_ratio": ratio,
            "cost": cost, "needs_replication": _near_boundary(margin, ratio, crit)}


def knowledge_value(l3: dict, l2: dict, crit: dict) -> dict:
    """Knowledge value in the vanilla track (§5.3): supported if L3 clears L2 on completion by
    > δ, or is cheaper (≤ 0.8×) at completion within δ."""
    c = contrast(l3, l2, crit)
    supported = (c["completion"] == "a_better") or \
                (c["completion"] == "tie" and c["cost"] == "a_cheaper")
    return {"supported": supported, **c}


def gate(aggs: dict[str, dict], crit: dict) -> dict:
    """The interim gate recommendation over the ladder cells passed in — the L0 greedy control
    plus the vanilla cells (L1–L3), per the §7 chain: best completion; ties within δ broken by
    lowest unconditional cost; residual ties by the simpler/cheaper rung (greedy is the simplest,
    so L0 can legitimately win when it is within δ of the best — it is the control, in-chain by
    design, not an omission)."""
    cells = list(aggs)
    best = max(completion(aggs[c]) for c in cells)
    contenders = [c for c in cells if best - completion(aggs[c]) <= crit["delta_seeds"]]
    if len(contenders) > 1:
        min_cost = min(_cost(aggs[c]) for c in contenders)
        band = min_cost * (1 + crit["cost_approx"])
        cost_tied = [c for c in contenders if _cost(aggs[c]) <= band]
        winner = min(cost_tied, key=lambda c: _RUNG_ORDER.get(c, 99))
    else:
        winner = contenders[0]
    return {"winner": winner, "contenders": contenders,
            "best_completion": best,
            "needs_replication": any(
                contrast(aggs[c], aggs[winner], crit)["needs_replication"]
                for c in contenders if c != winner)}


def evaluate(aggs: dict[str, dict], crit: dict) -> dict:
    """Bundle the vanilla-half §7 read: the gate recommendation + the L3-vs-L2 knowledge value
    (when both cells are present)."""
    out: dict = {"gate": gate(aggs, crit)}
    if "L3" in aggs and "L2" in aggs:
        out["knowledge_value"] = knowledge_value(aggs["L3"], aggs["L2"], crit)
    return out
