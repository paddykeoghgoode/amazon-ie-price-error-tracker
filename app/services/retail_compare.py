"""Conservative, cached price comparisons for explicitly linked Irish retailers.

Never infer a matching product solely from a shop search result. A direct
product page must expose the exact configured model / barcode and one clear
EUR offer in Product JSON-LD. No Google scraping or hidden APIs are used.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from urllib.parse import urljoin, urlsplit

import httpx
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

RETAILERS = {
    "currys.ie": "Currys",
    "smythstoys.com": "Smyths",
    "harveynorman.ie": "Harvey Norman",
    "did.ie": "DID Electrical",
    "electrocity.ie": "ElectroCity",
    "petermurphyelectrical.ie": "Peter Murphy Electrical",
    "euronics.ie": "Euronics",
    "expert.ie": "Expert",
    "joyces.ie": "Joyces",
    "arnotts.ie": "Arnotts",
    "keanscm.ie": "Keans",
    "irwinelectrical.ie": "Irwin Electrical",
    "very.ie": "Very",
}
MAX_LINKS_PER_PRODUCT = 8
MAX_HTML_BYTES = 1_000_000
MAX_REDIRECTS = 4
SUCCESS_TTL_HOURS = 12
ERROR_TTL_HOURS = 3
REQUEST_DELAY_SECONDS = 1.0
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; PriceWatchResearch/1.0)",
    "Accept": "text/html,application/xhtml+xml;q=0.9",
    "Accept-Language": "en-IE,en;q=0.8",
}
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{3,55}$")
_EAN_RE = re.compile(r"^\d{8,14}$")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def retailer_from_url(url: str | None) -> str | None:
    if not isinstance(url, str) or len(url) > 1800:
        return None
    try:
        parsed = urlsplit(url)
    except ValueError:
        return None
    if parsed.scheme != "https" or parsed.username or parsed.password:
        return None
    try:
        if parsed.port not in (None, 443):
            return None
    except ValueError:
        return None
    host = (parsed.hostname or "").lower().rstrip(".")
    if host.startswith("www."):
        host = host[4:]
    return RETAILERS.get(host)


def trusted_retailer_url(url: str | None) -> bool:
    return retailer_from_url(url) is not None


def clean_model(value: object) -> str | None:
    value = str(value or "").strip()
    return value.upper() if _MODEL_RE.fullmatch(value) else None


def clean_barcode(value: object) -> str | None:
    value = re.sub(r"\s+", "", str(value or ""))
    return value if _EAN_RE.fullmatch(value) else None


def _money(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        raw = str(value).replace("€", "").replace("\u00a0", "").replace(" ", "").strip()
        if "," in raw and "." in raw:
            # Both 1,299.99 (Irish) and 1.299,99 (EU) are supported.
            raw = (raw.replace(".", "").replace(",", ".")
                   if raw.rfind(",") > raw.rfind(".")
                   else raw.replace(",", ""))
        elif "," in raw:
            raw = raw.replace(",", ".") if re.search(r",\d{1,2}$", raw) else raw.replace(",", "")
        elif raw.count(".") > 1:
            return None
        price = Decimal(raw)
        if not price.is_finite() or price <= 0 or price > 100000:
            return None
        return float(price.quantize(Decimal("0.01")))
    except (InvalidOperation, ValueError):
        return None


def parse_retail_spec(data: dict | None) -> dict:
    """Accept trusted direct product links only, with optional manual RRP."""
    raw = data if isinstance(data, dict) else {}
    urls = []
    for url in (raw.get("retailer_urls") or []):
        if isinstance(url, str) and trusted_retailer_url(url):
            url = url.strip()
            if url not in urls:
                urls.append(url)
        if len(urls) >= MAX_LINKS_PER_PRODUCT:
            break
    rrp = _money(raw.get("rrp_eur"))
    source = str(raw.get("rrp_source") or "").strip()
    try:
        parsed_source = urlsplit(source)
        source_safe = (
            parsed_source.scheme == "https"
            and bool(parsed_source.hostname)
            and not parsed_source.username
            and not parsed_source.password
            and parsed_source.port in (None, 443)
            and len(source) <= 1000
        )
    except ValueError:
        source_safe = False
    if not source_safe:
        source = None
    return {
        "model": clean_model(raw.get("model")),
        "barcode": clean_barcode(raw.get("barcode")),
        "rrp_eur": rrp,
        "rrp_source": source,
        "retailer_urls": urls,
    }


def _schema_nodes(value: object):
    if isinstance(value, list):
        for item in value:
            yield from _schema_nodes(item)
    elif isinstance(value, dict):
        if "@graph" in value:
            yield from _schema_nodes(value["@graph"])
        kind = value.get("@type")
        kinds = kind if isinstance(kind, list) else [kind]
        if any(str(k or "").lower().rsplit("/", 1)[-1] == "product" for k in kinds):
            yield value


def _ids(obj: dict, keys: tuple[str, ...]) -> list[str]:
    return [str(obj[key]).strip() for key in keys if obj.get(key)]


def _matches_expected(
    product: dict,
    expected_model: str | None,
    expected_barcode: str | None,
    *,
    title_fallback: str | None = None,
    cex_category_name: str | None = None,
) -> str:
    """Confirm EAN, exact model, or CeX title match for linked product pages."""
    if not expected_model and not expected_barcode:
        if title_fallback:
            from app.scraper import cex_ie
            name = str(product.get("name") or "")
            if cex_ie.titles_match_for_trade_in(name, title_fallback, cex_category_name=cex_category_name):
                return "title_confirmed"
        return "identity_required"
    barcodes = _ids(product, ("gtin", "gtin8", "gtin12", "gtin13", "gtin14", "ean"))
    if expected_barcode and barcodes and expected_barcode not in {clean_barcode(v) for v in barcodes}:
        return "barcode_mismatch"
    declared_models = _ids(product, ("mpn", "model"))
    normalized = {clean_model(x) for x in declared_models}
    if expected_model and normalized and expected_model not in normalized:
        return "model_mismatch"
    if expected_barcode and barcodes:
        return "barcode_confirmed"
    if expected_model and expected_model in normalized:
        return "model_confirmed"
    # Some Irish shops use their manufacturer model code in Product.sku
    # rather than Product.mpn; count only an exact SKU match, never a fuzzy one.
    sku_models = {clean_model(x) for x in _ids(product, ("sku",))}
    if expected_model and expected_model in sku_models:
        return "model_confirmed"
    # A verified model token embedded in the retailer's actual Product name is
    # acceptable. A different colour/version code is never a fuzzy match.
    title = str(product.get("name") or "")
    if expected_model and re.search(r"(?<![A-Z0-9])" + re.escape(expected_model) + r"(?![A-Z0-9])", title, re.I):
        return "model_confirmed"
    return "model_not_confirmed"


def parse_retail_html(
    html: str, *,
    expected_model: str | None,
    expected_barcode: str | None,
    title_fallback: str | None = None,
    cex_category_name: str | None = None,
) -> dict:
    """Return one clear retailer offer or a non-actionable reason."""
    soup = BeautifulSoup(html, "lxml")
    products = []
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            products.extend(_schema_nodes(json.loads(tag.string or tag.get_text())))
        except (ValueError, TypeError):
            continue
    if not products:
        return {"status": "no_product_metadata"}
    best_issue = "identity_not_confirmed"
    for product in products:
        identity = _matches_expected(
            product, expected_model, expected_barcode,
            title_fallback=title_fallback, cex_category_name=cex_category_name,
        )
        if identity not in ("model_confirmed", "barcode_confirmed", "title_confirmed"):
            best_issue = identity
            continue
        offers = product.get("offers")
        offer_list = offers if isinstance(offers, list) else [offers]
        candidates = []
        for offer in offer_list:
            if not isinstance(offer, dict):
                continue
            offer_type = offer.get("@type")
            kinds = offer_type if isinstance(offer_type, list) else [offer_type]
            if not any(str(k or "").lower().rsplit("/", 1)[-1] == "offer" for k in kinds):
                continue  # AggregateOffer lowPrice is not a checkout offer.
            currency = str(offer.get("priceCurrency") or "").upper()
            spec = offer.get("priceSpecification")
            if not currency and isinstance(spec, dict):
                currency = str(spec.get("priceCurrency") or "").upper()
            if currency != "EUR":
                continue
            price = _money(offer.get("price"))
            if price is None and isinstance(spec, dict):
                price = _money(spec.get("price"))
            if price is None:
                continue
            availability = str(offer.get("availability") or "").rsplit("/", 1)[-1].lower()
            if availability in ("outofstock", "soldout", "discontinued"):
                stock = "out_of_stock"
            elif availability in ("instock", "limitedavailability"):
                stock = "in_stock"
            else:
                stock = "unknown"
            candidates.append((price, stock))
        if not candidates:
            return {"status": "price_unavailable", "identity": identity, "title": product.get("name")}
        # Multiple variants at different prices cannot safely be mapped to
        # the watched model without an offer-level model/sku mapping.
        if len({price for price, _ in candidates}) != 1:
            return {"status": "ambiguous_variants", "identity": identity, "title": product.get("name")}
        price, stock = candidates[0]
        return {
            "status": "matched" if stock == "in_stock" else
                      "out_of_stock" if stock == "out_of_stock" else "stock_unknown",
            "identity": identity,
            "title": str(product.get("name") or "")[:220],
            "price_eur": price,
            "availability": stock,
        }
    return {"status": best_issue}


async def fetch_retail_offer(
    client: httpx.AsyncClient,
    url: str,
    *,
    expected_model: str | None,
    expected_barcode: str | None,
    title_fallback: str | None = None,
    cex_category_name: str | None = None,
) -> dict:
    """Request only approved retailer HTTPS URLs, including redirect targets."""
    name = retailer_from_url(url)
    stamp = _utc_now()
    result = {"url": url, "retailer": name or "Unknown", "checked_at": stamp}
    if not name:
        return {**result, "status": "unsupported_retailer"}
    if not expected_model and not expected_barcode and not title_fallback:
        return {**result, "status": "identity_required"}
    current = url
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not trusted_retailer_url(current):
                return {**result, "status": "unsafe_redirect"}
            async with client.stream("GET", current, follow_redirects=False) as response:
                if response.status_code in (301, 302, 303, 307, 308):
                    new_url = urljoin(current, response.headers.get("location") or "")
                    if not trusted_retailer_url(new_url):
                        return {**result, "status": "unsafe_redirect"}
                    current = new_url
                    continue
                if response.status_code in (403, 429, 503):
                    return {**result, "status": "rate_limited"}
                if response.status_code != 200:
                    return {**result, "status": "http_error"}
                content_type = response.headers.get("content-type", "")
                if content_type and "text/html" not in content_type:
                    return {**result, "status": "not_html"}
                chunks = bytearray()
                async for part in response.aiter_bytes():
                    chunks.extend(part)
                    if len(chunks) > MAX_HTML_BYTES:
                        return {**result, "status": "page_too_large"}
                data = parse_retail_html(
                    chunks.decode("utf-8", "replace"),
                    expected_model=expected_model, expected_barcode=expected_barcode,
                    title_fallback=title_fallback, cex_category_name=cex_category_name,
                )
                return {**result, **data}
        return {**result, "status": "too_many_redirects"}
    except (httpx.HTTPError, ValueError, UnicodeError):
        return {**result, "status": "fetch_error"}


def _cache_fresh(row: dict, now: datetime) -> bool:
    try:
        dt = datetime.fromisoformat(row.get("checked_at") or "")
        if dt.tzinfo is None:
            return False
        age = now - dt
        ttl = SUCCESS_TTL_HOURS if row.get("status") in ("matched", "stock_unknown", "out_of_stock") else ERROR_TTL_HOURS
        return timedelta(0) <= age < timedelta(hours=ttl)
    except (ValueError, TypeError):
        return False


def _make_summary(
    spec: dict,
    offers: list[dict],
    amazon_price: float | None,
    checked_at: str,
) -> dict:
    good = [offer for offer in offers
            if offer.get("status") == "matched"
            and isinstance(offer.get("price_eur"), (float, int))]
    lowest = min(good, key=lambda x: x["price_eur"]) if good else None
    price = _money(amazon_price)
    diff = round(price - lowest["price_eur"], 2) if price is not None and lowest else None
    return {
        "checked_at": checked_at,
        "model": spec.get("model"),
        "barcode": spec.get("barcode"),
        "rrp_eur": spec.get("rrp_eur"),
        "rrp_source": spec.get("rrp_source"),
        "rrp_kind": "user_supplied" if spec.get("rrp_eur") is not None else None,
        "offers": offers,
        "lowest_checked_retailer": lowest["retailer"] if lowest else None,
        "lowest_checked_retailer_price_eur": lowest["price_eur"] if lowest else None,
        "amazon_minus_lowest_retail_eur": diff,
        "matched_offer_count": len(good),
        "basis": "item prices only; delivery, discounts and checkout not verified",
    }


async def compare_watchlist(
    products: list[dict],
    specs: dict[str, dict],
    previous: dict | None,
    *,
    max_requests: int = 12,
) -> None:
    """Mutate watchlist snapshot rows; do not block existing Amazon price checks.

    A merchant product page is refreshed at most every 12h (3h on errors).
    No new retailer request occurs if the watched item has no merchant URLs.
    """
    prior_by_asin = {x.get("asin"): x for x in ((previous or {}).get("products") or [])}
    now = datetime.now(timezone.utc)
    remaining = max(0, max_requests)
    async with httpx.AsyncClient(headers=HEADERS, timeout=15.0) as client:
        for product in products:
            asin = str(product.get("asin") or "")
            raw = specs.get(asin) or {}
            spec = parse_retail_spec(raw)
            previous_product = prior_by_asin.get(asin) or {}
            previous_cmp = previous_product.get("retail_comparison") or {}
            # Manual model has priority over Amazon detail metadata.
            spec["model"] = spec["model"] or clean_model(product.get("model_number"))
            spec["barcode"] = spec["barcode"] or clean_barcode(product.get("barcode"))
            # Preserve last reliably observed identifiers during an Amazon
            # page scrape outage, but not old user settings.
            spec["model"] = spec["model"] or clean_model(previous_product.get("model_number"))
            spec["barcode"] = spec["barcode"] or clean_barcode(previous_product.get("barcode"))
            key = hashlib.sha256(json.dumps({
                "urls": spec["retailer_urls"],
                "model": spec["model"],
                "barcode": spec["barcode"],
            }, sort_keys=True).encode()).hexdigest()
            cached = (previous_cmp.get("offers") or []) if previous_cmp.get("cache_key") == key else []
            cache_by_url = {o.get("url"): o for o in cached if isinstance(o, dict)}
            offers = []
            for url in spec["retailer_urls"]:
                prior_offer = cache_by_url.get(url)
                if prior_offer and _cache_fresh(prior_offer, now):
                    offers.append(prior_offer)
                elif remaining > 0:
                    latest = await fetch_retail_offer(
                        client, url,
                        expected_model=spec["model"], expected_barcode=spec["barcode"],
                    )
                    offers.append(latest)
                    if latest["status"] != "identity_required":
                        remaining -= 1
                        await asyncio.sleep(REQUEST_DELAY_SECONDS)
                else:
                    offers.append({
                        "url": url,
                        "retailer": retailer_from_url(url),
                        "status": "pending_refresh",
                        "checked_at": None,
                    })
            summary = _make_summary(spec, offers, product.get("price_eur"), _utc_now())
            summary["cache_key"] = key
            product["retail_comparison"] = summary
            if spec.get("model"):
                product["model_number"] = spec["model"]
            if spec.get("barcode"):
                product["barcode"] = spec["barcode"]
