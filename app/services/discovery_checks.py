"""Bounded cold-start and exact-identity checks using the existing page budget."""
from datetime import datetime, timezone

from app.scraper.cex_ie import titles_match_for_trade_in
from app.scraper.product_identity import normalise_barcode
from app.services.cex_arbitrage import _compatible_quote, _match_level
from app.services.cex_cache import cache_price
from app.services.resale_relevance import relevance


def choose_detail_checks(catalogue, lookups, drops, *, config, policy):
    """Check profitable provisional matches first, then drops, then cold-start rows.

    All lanes share premium_spot_check_limit. Oldest attempts rotate within a
    lane so an unparseable first card cannot monopolise every run.
    """
    items = {r['asin']: r for r in catalogue.get('items') or [] if r.get('active', True)}
    profitable = {r['asin'] for r in lookups if
                  float(r.get('cex_cash_profit_eur') or 0) >= policy.min_cex_cash_profit_eur
                  and float(r.get('cex_net_roi_percent') or 0) >= policy.min_cex_roi_percent
                  and r.get('match_confidence') == 'title_only'}
    dropped = {r['asin'] for r in drops}
    rows = []
    for asin, row in items.items():
        if not relevance(row)[0]:
            continue
        price = float(row.get('price_eur') or 0)
        minimum = policy.min_cex_buy_price_eur if asin in profitable else policy.min_amazon_price_eur
        if price < minimum:
            continue
        if config.signal_min_price_eur is not None and price < config.signal_min_price_eur:
            continue
        if config.signal_min_review_count is not None and (row.get('review_count') or 0) < config.signal_min_review_count:
            continue
        # Avoid spending the broad-page budget on low-value first-seen goods.
        if asin not in profitable and asin not in dropped and price < policy.min_reference_eur:
            continue
        lane = 0 if asin in profitable else 1 if asin in dropped else 2
        rows.append((lane, row.get('identity_checked_at') or '', -price, asin, row))
    rows.sort(key=lambda x: x[:4])
    # Build exact maps before a price drop occurs, including targets that are
    # currently too expensive. Reserve at most half the SAME six-page budget.
    targets = sorted((r for r in items.values() if r.get('cex_target_box_id')
                      and relevance(r)[1] != 'explicitly_excluded_brand'
                      and r.get('identity_status') != 'confirmed'
                      and float(r.get('price_eur') or 0) >= policy.min_cex_buy_price_eur),
                     key=lambda r: (r.get('identity_checked_at') or '', r['asin']))
    selected = targets[:config.premium_spot_check_limit // 2]
    seen = {r['asin'] for r in selected}
    selected.extend(r[-1] for r in rows if r[-1]['asin'] not in seen)
    return selected[:config.premium_spot_check_limit]


def apply_detail_checks(catalogue, selected, snapshots, lookups, quote_cache):
    """Promote only matching identity evidence; never make title-only alerts eligible."""
    now = datetime.now(timezone.utc).isoformat()
    inventory = {r['asin']: r for r in catalogue.get('items') or []}
    by_asin = {r['asin']: r for r in lookups}
    rejected = set()
    counts = {'attempted': len(selected), 'price_read': 0, 'identity_confirmed': 0,
              'price_changed': 0, 'identity_mismatch': 0, 'identifiers_missing': 0,
              'unreadable': 0}
    for candidate in selected:
        asin = candidate['asin']
        row = inventory[asin]
        row['identity_checked_at'] = now
        row['identity_status'] = 'unreadable'
        snap = snapshots.get(asin)
        if not snap or snap.asin != asin or not snap.price_eur or snap.currency != 'EUR':
            counts['unreadable'] += 1
            continue
        counts['price_read'] += 1
        if abs(snap.price_eur - float(row.get('price_eur') or 0)) > 0.01:
            row['identity_status'] = 'price_changed'
            counts['price_changed'] += 1
            rejected.add(asin)
            continue
        if not titles_match_for_trade_in(row.get('title'), snap.title):
            row.pop('barcode', None)
            row.pop('model_number', None)
            row['identity_status'] = 'identity_mismatch'
            counts['identity_mismatch'] += 1
            rejected.add(asin)
            continue
        if not snap.barcode and not snap.model_number:
            row['identity_status'] = 'identifiers_missing'
            counts['identifiers_missing'] += 1
            continue
        row['barcode'] = snap.barcode
        row['model_number'] = snap.model_number
        row['identity_status'] = 'identifiers_read'
        lookup = by_asin.get(asin)
        prices = cache_price(quote_cache.get(asin))
        if not lookup or not prices:
            continue
        if not _compatible_quote(row, prices):
            row.pop('barcode', None)
            row.pop('model_number', None)
            row['identity_status'] = 'identity_mismatch'
            counts['identity_mismatch'] += 1
            rejected.add(asin)
            continue
        lookup['match_confidence'] = _match_level(row, prices)
        lookup['amazon_barcode'] = normalise_barcode(row.get('barcode'))
        lookup['amazon_model_number'] = row.get('model_number')
        if lookup['match_confidence'] != 'title_only':
            row['identity_status'] = 'confirmed'
            counts['identity_confirmed'] += 1
    # Changed offers/contradictory identifiers cannot remain profitable leads.
    for asin in rejected:
        row = by_asin.get(asin)
        if row:
            row['match_confidence'] = 'rejected'
            row['cex_cash_flip'] = False
    return counts
