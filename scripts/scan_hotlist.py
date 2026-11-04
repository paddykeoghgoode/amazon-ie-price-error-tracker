"""Bounded price checks for exact CeX targets and recently observed drops.

The same twelve Amazon pages are shared by both lanes. CeX quote maintenance
belongs to the broad scan's existing budget; this workflow makes no CeX calls.
"""
import argparse
import asyncio
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx

from app.scraper.amazon_ie import fetch_product
from app.services.monitor import BROWSER_HEADERS, send_notification
from app.services.alert_policy import (
    hotlist_further_drop_qualifies, load_alert_policy, qualifies_observed_drop,
    qualifies_cex_alert, materially_cheaper_than_last,
)
from app.services.bestsellers import load_config
from app.services.cex_arbitrage import _match_level, build_cex_row
from app.services.cex_cache import cache_price
from app.services.cex_targets import load_target_config, watch_rows, target_funnel, exact_target_match
from app.snapshot import read_snapshot, write_snapshot
from app.services.resale_relevance import relevance
from app.services.public_price_history import record_price


def candidates(deals, now, limit=12):
    rows = {}
    policy = load_alert_policy()
    for row in (deals.get("drops") or []) + (deals.get("cex_flips") or []):
        if not relevance(row)[0] or not qualifies_observed_drop(row, policy):
            continue
        try:
            stamp = datetime.fromisoformat(row.get("found_at") or row.get("observed_at") or "")
            if stamp.tzinfo is None or not timedelta(0) <= now - stamp <= timedelta(hours=48):
                continue
        except (ValueError, TypeError):
            continue
        if row.get("asin"):
            rows.setdefault(row["asin"], row)
    return sorted(rows.values(), key=lambda r: -float(r.get("drop_percent") or 0))[:limit]


def select_fast_checks(drops, mapped, prior, limit=12):
    """Normally 8 maps / 4 drops, borrowing empty slots. Rotate old attempts.

    Six map slots go to the 24 nearest buy ceilings; two explore the rest so
    expensive products can still reveal sudden errors. No promise that all
    1,000 mappings are checked on each ten-minute tick.
    """
    limit = max(0, min(12, int(limit)))
    if not limit:
        return []
    mapped = [r for r in mapped if r.get("cex_quote_fresh_for_alert")]
    mapping_ids = {r['asin'] for r in mapped}
    drops = [dict(r, scan_lane='drop') for r in drops if r['asin'] not in mapping_ids]
    def oldest(row):
        return (prior.get(row['asin'], {}).get('checked_at') or '', row['asin'])
    def distance(row):
        previous = prior.get(row['asin']) or {}
        price = previous.get('price_eur') if previous.get('status') in ('price_read', 'profitable', 'above_buy_ceiling') else None
        return float(price or row.get('price_eur') or float('inf')) / max(float(row.get('cex_max_buy_eur') or 0), 0.01)
    drops.sort(key=oldest)
    drop_slots = min(len(drops), limit // 3)
    map_slots = min(len(mapped), limit - drop_slots)
    near = sorted(mapped, key=lambda r: (distance(r), r['asin']))[:24]
    near.sort(key=oldest)
    chosen = near[:max(0, map_slots - min(2, map_slots))]
    seen = {r['asin'] for r in chosen}
    chosen += sorted((r for r in mapped if r['asin'] not in seen), key=oldest)[:map_slots-len(chosen)]
    chosen += drops[:limit-len(chosen)]
    return chosen


def evaluate_target(row, snap, config):
    """Revalidate the current ASIN/variant, then recompute conservative cash ROI."""
    prices = cache_price(row.get('cex_quote_cache'))
    identity = {**row, 'title': snap.title, 'barcode': snap.barcode, 'model_number': snap.model_number}
    if snap.asin != row['asin'] or not prices or not exact_target_match(identity, prices):
        return {'status': 'identity_mismatch', 'cex_cash_flip': False}
    policy = load_alert_policy()
    result = build_cex_row(
        {**row, 'price_eur': snap.price_eur}, prices,
        min_profit_eur=max(config.cex_min_cash_profit_eur, policy.min_cex_cash_profit_eur),
        min_profit_percent=config.cex_min_cash_profit_percent,
        min_roi_percent=max(config.cex_min_roi_percent, policy.min_cex_roi_percent),
        delivery_eur=config.cex_delivery_eur, extra_cost_eur=config.cex_extra_cost_eur,
        payout_haircut_percent=config.cex_payout_haircut_percent,
    )
    result['match_confidence'] = _match_level(identity, prices)
    result['status'] = 'profitable' if qualifies_cex_alert(result, policy) else 'above_buy_ceiling'
    if snap.price_eur < policy.min_cex_buy_price_eur:
        result['status'] = 'below_minimum_buy'
    allowed, reason, brand = relevance(result)
    result.update(resale_eligible=allowed, resale_reason=reason, resale_brand=brand)
    # A large persistent source cache isn't needed in the hotlist output.
    result.pop('cex_quote_cache', None)
    return result


async def recheck(deals, previous, limit=12, notify=True, *, catalogue=None, config=None, now=None):
    now = now or datetime.now(timezone.utc)
    config = config or load_config(Path('bestsellers.json'))
    options = load_target_config()
    state = (catalogue or {}).get('cex_targets') or {}
    mapped = watch_rows(state, config, now=now) if options.enabled and config.cex_enabled else []
    prior = {r['asin']: dict(r) for r in (previous.get('products') or []) if r.get('asin')}
    funnel = target_funnel(state, config, now=now)
    funnel.update(selected=0, amazon_attempted=0, price_read=0, identity_mismatch=0,
                  profitable=0, above_buy_ceiling=0, below_minimum_buy=0, unreadable=0, alerts_sent=0)
    try:
        blocked_until = datetime.fromisoformat(previous.get('blocked_until') or '')
        cooling_down = blocked_until.tzinfo is not None and now < blocked_until
    except (ValueError, TypeError):
        cooling_down = False
    if cooling_down:
        return {**previous, 'generated_at': now.isoformat(), 'checked_this_run': 0,
                'price_read': 0, 'funnel': funnel, 'scan_status': 'provider_cooldown'}
    # Keep unselected mappings and successful alert fingerprints across ticks.
    out = dict(prior)
    selected = select_fast_checks(candidates(deals, now, limit=500), mapped, prior, limit)
    funnel['selected'] = len(selected)
    blocked = None
    sent = 0
    policy = load_alert_policy()
    async with httpx.AsyncClient(headers=BROWSER_HEADERS, timeout=25.0) as client:
        for row in selected:
            asin = row['asin']
            earlier = prior.get(asin) or {}
            item = {**earlier, 'asin': asin, 'title': row.get('title'), 'url': row.get('url'),
                    'scan_lane': row.get('scan_lane', 'drop'), 'status': 'price_not_read',
                    'checked_at': now.isoformat(), 'cex_cash_flip': False}
            # Don't present a previous successful read as today's result.
            for key in ('price_eur', 'cex_cash_profit_eur', 'cex_net_roi_percent'):
                item.pop(key, None)
            if row.get('scan_lane') == 'cex_target':
                for key in ('cex_title', 'cex_url', 'cex_cash_eur', 'cex_max_buy_eur', 'cex_quote_checked_at', 'cex_quote_fresh_for_alert'):
                    item[key] = row.get(key)
            funnel['amazon_attempted'] += 1
            message = None
            try:
                snap = await fetch_product(client, asin)
                if (snap.asin == asin and snap.currency == 'EUR' and snap.price_eur is not None
                        and math.isfinite(snap.price_eur) and snap.price_eur > 0):
                    funnel['price_read'] += 1
                    item.update(status='price_read', price_eur=snap.price_eur)
                    if row.get('scan_lane') == 'cex_target':
                        item.update(evaluate_target(row, snap, config))
                        item['checked_at'] = now.isoformat()
                        funnel[item['status']] += 1
                        if item['status'] == 'profitable':
                            item['found_at'] = (earlier.get('opportunity_found_at')
                                                if earlier.get('opportunity_price_eur') == snap.price_eur
                                                else None) or now.isoformat()
                            item['opportunity_found_at'] = item['found_at']
                            item['opportunity_price_eur'] = snap.price_eur
                            broad_alert = row.get('last_cex_alert') or {}
                            last_price = earlier.get('last_cex_alert_price_eur', broad_alert.get('price_eur'))
                            if materially_cheaper_than_last(last_price, snap.price_eur, policy):
                                message = (
                                    '💰 Exact-match CeX cash research lead\n'
                                    f"{row.get('title') or asin}\nAmazon €{snap.price_eur:.2f} · buy ceiling €{item['cex_max_buy_eur']:.2f}\n"
                                    f"CeX cash €{item['cex_cash_eur']:.2f} · buffered net €{item['cex_cash_profit_eur']:.2f}\n"
                                    f"CeX variant/grade: {item['cex_title']}\n"
                                    'Check condition, accessories, delivered checkout and current CeX acceptance.\n'
                                    f"{row.get('url')}\n{item['cex_url']}"
                                )
                    else:
                        threshold = min(float(earlier.get('price_eur') or float('inf')),
                                        float(row.get('price_eur') or float('inf')))
                        if materially_cheaper_than_last(earlier.get('last_alert_price_eur'), snap.price_eur, policy) and hotlist_further_drop_qualifies(
                                row, previous_eur=threshold, current_eur=snap.price_eur):
                            message = (
                                '🚨 Large high-ticket Amazon reduction (RESEARCH ONLY)\n'
                                f'{snap.title or asin}\nNow €{snap.price_eur:.2f}, last observed €{threshold:.2f}\n'
                                'Seller, condition, variant and delivered checkout are not verified.\n'
                                f"{row.get('url')}"
                            )
                    if message and notify and sent < policy.max_alerts_per_run:
                        ok, sms_ok = await send_notification(message)
                        if ok or sms_ok:
                            prefix = 'last_cex_alert' if row.get('scan_lane') == 'cex_target' else 'last_alert'
                            item[prefix + '_price_eur'] = snap.price_eur
                            item[prefix + '_at'] = now.isoformat()
                            sent += 1
                            funnel['alerts_sent'] += 1
                else:
                    funnel['unreadable'] += 1
                    item['status'] = snap.parse_status
            except (httpx.HTTPError, ValueError, TypeError, OverflowError) as exc:
                from app.scraper.amazon_ie import failure_reason
                funnel['unreadable'] += 1
                item['status'] = failure_reason(exc)
                if item['status'] == 'provider_blocked':
                    blocked = (now + timedelta(minutes=30)).isoformat()
            if item['status'] in ('price_read', 'profitable', 'above_buy_ceiling', 'below_minimum_buy'):
                history = earlier.get('price_history') or []
                if not history and earlier.get('status') in ('price_read', 'profitable', 'above_buy_ceiling', 'below_minimum_buy'):
                    history = record_price([], earlier.get('price_eur'), earlier.get('checked_at'), 'product_page')
                item['price_history'] = record_price(history, item.get('price_eur'), item['checked_at'], 'product_page')
            out[asin] = item
            if blocked:
                break
            await asyncio.sleep(config.request_delay_seconds)
    # Up to 1,000 mappings plus recent drop observations; bounded public state.
    retained = sorted(out.values(), key=lambda r: r.get('checked_at') or '', reverse=True)[:1500]
    result = {'generated_at': now.isoformat(), 'scan_kind': 'hotlist', 'products': retained,
              'checked_this_run': funnel['amazon_attempted'], 'price_read': funnel['price_read'],
              'funnel': funnel, 'scan_status': 'provider_blocked' if blocked else 'completed'}
    if blocked:
        result['blocked_until'] = blocked
    return result


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--deals', type=Path, required=True)
    p.add_argument('--previous', type=Path, required=True)
    p.add_argument('--catalogue', type=Path)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--limit', type=int, default=12)
    p.add_argument('--no-notify', action='store_true')
    a = p.parse_args()
    result = asyncio.run(recheck(read_snapshot(a.deals) if a.deals.exists() else {},
                                read_snapshot(a.previous) if a.previous.exists() else {},
                                limit=a.limit, notify=not a.no_notify,
                                catalogue=read_snapshot(a.catalogue) if a.catalogue and a.catalogue.exists() else {}))
    write_snapshot(a.output, result)
    print(json.dumps(result['funnel']))


if __name__ == '__main__':
    main()
