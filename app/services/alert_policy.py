"""Conservative Telegram policy: actionable high-ticket leads, not scan updates.

Every observed drop remains on the website. This policy only decides when
the user's phone should be interrupted. Seller and checkout remain unverified.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class AlertPolicy:
    min_amazon_price_eur: float = 120.0
    min_reference_eur: float = 250.0
    min_saving_eur: float = 120.0
    min_history_drop_percent: float = 50.0
    # Advertised "was" prices can be inflated: only very large apparent
    # reductions qualify when no stable Amazon history is available.
    min_list_drop_percent: float = 75.0
    min_list_saving_eur: float = 250.0
    # CeX resale is assessed by exact identity and net profit, independently
    # of the high-ticket Amazon-drop floor. A genuine error can be very cheap.
    min_cex_buy_price_eur: float = 0.01
    min_cex_cash_profit_eur: float = 5.0
    min_cex_roi_percent: float = 0.0
    max_alerts_per_run: int = 3
    min_hotlist_extra_drop_eur: float = 25.0
    min_hotlist_extra_drop_percent: float = 10.0


def _positive(value: Any, default: float, *, allow_zero: bool = False) -> float:
    try:
        v = float(value)
        if not (v < float("inf")) or v < 0 or (v == 0 and not allow_zero):
            return default
        return v
    except (TypeError, ValueError):
        return default


def policy_from_dict(raw: dict | None) -> AlertPolicy:
    source = raw if isinstance(raw, dict) else {}
    baseline = AlertPolicy()
    fields = {
        name: _positive(source.get("telegram_" + name), getattr(baseline, name))
        for name in (
            "min_amazon_price_eur", "min_reference_eur", "min_saving_eur",
            "min_history_drop_percent", "min_list_drop_percent",
            "min_list_saving_eur", "min_cex_buy_price_eur",
            "min_cex_cash_profit_eur", "min_cex_roi_percent",
            "min_hotlist_extra_drop_eur", "min_hotlist_extra_drop_percent",
        )
    }
    try:
        limit = int(source.get("telegram_max_alerts_per_run", baseline.max_alerts_per_run))
    except (ValueError, TypeError):
        limit = baseline.max_alerts_per_run
    return AlertPolicy(**fields, max_alerts_per_run=max(0, min(20, limit)))


@lru_cache(maxsize=2)
def load_alert_policy(path: str = "bestsellers.json") -> AlertPolicy:
    try:
        return policy_from_dict(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return AlertPolicy()


def _discount(price: float, reference: float) -> float:
    return 100 * (reference - price) / reference if reference > 0 else 0.0


def qualifies_price_event(
    current_eur: float | None,
    *,
    historical_reference_eur: float | None = None,
    list_reference_eur: float | None = None,
    policy: AlertPolicy | None = None,
) -> bool:
    policy = policy or AlertPolicy()
    try:
        current = float(current_eur or 0)
    except (ValueError, TypeError):
        return False
    if not (0 < current < float("inf")):
        return False
    for raw_reference, is_advertised_list in (
        (historical_reference_eur, False),
        (list_reference_eur, True),
    ):
        try:
            ref = float(raw_reference or 0)
        except (ValueError, TypeError):
            continue
        if not (policy.min_reference_eur <= ref < float("inf")):
            continue
        saving = ref - current
        if current < policy.min_amazon_price_eur:
            # A genuine error can make an expensive item cheap. Require a
            # stable high-value history and a much deeper cut for this lane;
            # an advertised list price alone never bypasses the normal floor.
            if (is_advertised_list or ref < max(500, policy.min_reference_eur)
                    or saving < max(250, policy.min_saving_eur)
                    or _discount(current, ref) < max(75, policy.min_history_drop_percent)):
                continue
        if saving < (policy.min_list_saving_eur if is_advertised_list else policy.min_saving_eur):
            continue
        threshold = (
            policy.min_list_drop_percent if is_advertised_list
            else policy.min_history_drop_percent
        )
        if _discount(current, ref) >= threshold:
            return True
    return False


def qualifies_observed_drop(row: dict, policy: AlertPolicy | None = None) -> bool:
    ref = row.get("reference_eur", row.get("median_eur"))
    kind = row.get("drop_kind")
    return qualifies_price_event(
        row.get("price_eur"),
        historical_reference_eur=None if kind == "list_price" else ref,
        list_reference_eur=ref if kind == "list_price" else None,
        policy=policy,
    )


def qualifies_cex_alert(row: dict, policy: AlertPolicy | None = None) -> bool:
    policy = policy or AlertPolicy()
    try:
        amazon = float(row.get("price_eur") or 0)
        profit = float(row.get("cex_cash_profit_eur") or 0)
        roi = float(row.get("cex_net_roi_percent") or 0)
        quote = float(row.get("cex_cash_eur") or 0)
    except (TypeError, ValueError):
        return False
    # No title-only matching in interruptive alerts. It is still listed on
    # the dashboard for manual research; CeX grade/checkout is unverified.
    return (
        policy.min_cex_buy_price_eur <= amazon < float("inf")
        and policy.min_cex_cash_profit_eur <= profit < float("inf")
        and policy.min_cex_roi_percent <= roi < float("inf")
        and quote > 0
        and row.get("match_confidence") in ("barcode_verified", "model_verified")
        and row.get("cex_quote_fresh_for_alert") is True
        and row.get("cex_cash_flip") is not False
    )


def materially_cheaper_than_last(
    last_eur: float | None, current_eur: float | None,
    policy: AlertPolicy | None = None,
) -> bool:
    """Do not repeat old deals every 24 hours at an unchanged price."""
    policy = policy or AlertPolicy()
    if last_eur is None:
        return True
    try:
        last = float(last_eur)
        current = float(current_eur or 0)
    except (TypeError, ValueError):
        return False
    return (
        last > current > 0
        and last - current >= policy.min_hotlist_extra_drop_eur
        and _discount(current, last) >= policy.min_hotlist_extra_drop_percent
    )


def hotlist_further_drop_qualifies(
    row: dict, *, previous_eur: float, current_eur: float,
    policy: AlertPolicy | None = None,
) -> bool:
    policy = policy or AlertPolicy()
    if previous_eur <= current_eur or previous_eur <= 0:
        return False
    return (
        qualifies_observed_drop({**row, "price_eur": current_eur}, policy)
        and previous_eur - current_eur >= policy.min_hotlist_extra_drop_eur
        and _discount(current_eur, previous_eur) >= policy.min_hotlist_extra_drop_percent
    )
