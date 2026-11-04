"""Bounded observations for public charts; never manufacture missing history."""
from datetime import datetime
from math import isfinite


def record_price(history, price, observed_at, source):
    points = {}
    for row in list(history or []) + [{'price_eur': price, 'observed_at': observed_at, 'source': source}]:
        try:
            value = float(row['price_eur'])
            stamp = datetime.fromisoformat(row['observed_at'])
            if not stamp.tzinfo or not isfinite(value) or value <= 0:
                continue
            points[stamp.timestamp()] = {'price_eur': round(value, 2), 'observed_at': stamp.isoformat(),
                                        'source': row.get('source', 'observation')}
        except (ValueError, TypeError, KeyError, OverflowError):
            continue
    compact = []
    for key in sorted(points):
        point = points[key]
        if len(compact) >= 2 and all(p['price_eur'] == point['price_eur'] and p['source'] == point['source'] for p in compact[-2:]):
            compact[-1] = point
        else:
            compact.append(point)
    return compact[-120:]
