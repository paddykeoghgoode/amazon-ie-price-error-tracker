"""Explicit resale-interest rules, never guesses from spelling or country."""
import json
import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path


@dataclass(frozen=True)
class ResalePolicy:
    enabled: bool = False
    preferred_brands: tuple[str, ...] = ()
    excluded_brands: tuple[str, ...] = ()


@lru_cache(maxsize=2)
def load_resale_policy(path='bestsellers.json'):
    try:
        raw = json.loads(Path(path).read_text()).get('resale_brand_policy') or {}
    except (OSError, ValueError, TypeError):
        return ResalePolicy()
    return ResalePolicy(
        enabled=raw.get('enabled') is True,
        preferred_brands=tuple(str(v).strip() for v in raw.get('preferred_brands') or [] if str(v).strip()),
        excluded_brands=tuple(str(v).strip() for v in raw.get('excluded_brands') or [] if str(v).strip()),
    )


@lru_cache(maxsize=8)
def _brand_matchers(brands):
    canonical = {brand.casefold(): brand for brand in brands}
    pattern = '|'.join(re.escape(brand) for brand in sorted(brands, key=len, reverse=True))
    return canonical, re.compile(r'^(?:new\s+|renewed\s+)?(' + pattern + r')(?!\w)', re.I) if pattern else None


def _prefix_brand(row, brands):
    explicit = str(row.get('brand') or '').strip().casefold()
    title = str(row.get('title') or row.get('product_title') or '').strip()
    canonical, pattern = _brand_matchers(tuple(brands))
    if explicit in canonical:
        return canonical[explicit]
    # A compatibility mention in an accessory title is not its manufacturer.
    match = pattern.match(title) if pattern else None
    if match:
        return canonical[match.group(1).casefold()]
    return None


def relevance(row, policy=None):
    policy = policy or load_resale_policy()
    if not policy.enabled:
        return True, 'filter_disabled', None
    excluded = _prefix_brand(row, policy.excluded_brands)
    if excluded:
        return False, 'explicitly_excluded_brand', excluded
    preferred = _prefix_brand(row, policy.preferred_brands)
    if preferred:
        return True, 'recognised_brand', preferred
    try:
        cash = float(row.get('cex_cash_eur') or 0)
    except (TypeError, ValueError):
        cash = 0
    if (row.get('match_confidence') in ('barcode_verified', 'model_verified')
            and row.get('cex_quote_fresh_for_alert') is True
            and 0 < cash < float('inf')):
        return True, 'exact_cex_resale_evidence', None
    return False, 'unrecognised_brand', None


def annotate_payload(payload, policy=None):
    policy = policy or load_resale_policy()
    payload['resale_policy'] = asdict(policy)
    for field in ('drops', 'general_drops', 'premium_drops', 'rapid_drops',
                  'cex_flips', 'cex_arbitrage', 'cex_lookups', 'cex_near_margin', 'alerts'):
        for row in payload.get(field) or []:
            allowed, reason, brand = relevance(row, policy)
            row.update(resale_eligible=allowed, resale_reason=reason, resale_brand=brand)
    for rows in (payload.get('deep_drops') or {}).values():
        for row in rows:
            allowed, reason, brand = relevance(row, policy)
            row.update(resale_eligible=allowed, resale_reason=reason, resale_brand=brand)
    return payload
