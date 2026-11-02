from __future__ import annotations

from typing import Any


def drop_tier(drop_percent: float, tiers: list[float]) -> int | None:
    for threshold in sorted(tiers, reverse=True):
        if drop_percent >= threshold:
            return int(threshold)
    return None


def tiered_deep_drops(drops: list[dict], tiers: list[float]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {str(int(t)): [] for t in sorted(tiers)}
    for item in drops:
        tier = item.get("drop_tier")
        if tier is None:
            continue
        key = str(int(tier))
        if key in out:
            out[key].append(item)
    for key in out:
        out[key].sort(key=lambda d: d.get("drop_percent") or 0, reverse=True)
    return out
