from __future__ import annotations

import logging
from app.scraper.product_identity import catalog_barcodes, cex_model_number
import re
import math
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

PLATFORM_ID = 18
WSS_BASE = "https://wss2.cex.ie.webuy.io/v3"
ALGOLIA_APP_ID = "LNNFEEWZVA"
ALGOLIA_SEARCH_KEY = "bf79f2b6699e60a18ae330a1248b452c"
ALGOLIA_INDEX = "prod_cex_ie"
SITE_ORIGIN = "https://ie.webuy.com"

CEX_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; AmazonIEPriceTracker/1.0)",
    "Accept": "application/json",
    "Origin": SITE_ORIGIN,
    "Referer": f"{SITE_ORIGIN}/",
}

# Strip size/colour noise so Algolia gets a product name, not an Amazon title soup.
_TITLE_NOISE = re.compile(
    r"\b(black|white|grey|gray|silver|gold|red|blue|green|new|refurbished|renewed|"
    r"with warranty|free delivery|amazon\.ie)\b",
    re.I,
)
_TOKEN = re.compile(r"[a-z0-9]+", re.I)

# Words too generic to prove the same product.
_GENERIC_TOKENS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "usb",
        "wireless",
        "wired",
        "bluetooth",
        "gaming",
        "game",
        "headset",
        "headphones",
        "earbuds",
        "earphones",
        "controller",
        "console",
        "edition",
        "version",
        "series",
        "pro",
        "max",
        "plus",
        "lite",
        "mini",
        "pack",
        "set",
        "kit",
        "new",
        "official",
        "compatible",
        "pc",
        "ps4",
        "ps5",
        "xbox",
        "nintendo",
        "switch",
        "iphone",
        "ipad",
        "android",
        "windows",
        "mac",
        "rgb",
        "noise",
        "cancelling",
        "canceling",
        "microphone",
        "mic",
        "stereo",
        "surround",
        "digital",
        "hdmi",
        "led",
        "oled",
        "inch",
        "inches",
    }
)

_SKIP_LEADING_BRAND = frozenset({"amazon", "basics", "renewed", "certified"})

# If both titles mention a brand from this set, they must share at least one.
_KNOWN_BRANDS = frozenset(
    {
        "apple",
        "samsung",
        "sony",
        "microsoft",
        "nintendo",
        "logitech",
        "razer",
        "steelseries",
        "hyperx",
        "corsair",
        "astro",
        "bose",
        "jbl",
        "beats",
        "sennheiser",
        "ozeino",
        "trust",
        "turtle",
        "beach",
        "asus",
        "acer",
        "lenovo",
        "dell",
        "hp",
        "canon",
        "nikon",
        "gopro",
        "meta",
        "oculus",
        "quest",
        "playstation",
        "xbox",
    }
)


@dataclass(frozen=True)
class CexPrices:
    box_id: str
    box_name: str
    category_name: str | None
    cash_eur: float
    voucher_eur: float
    retail_eur: float
    url: str
    barcodes: tuple[str, ...] = ()
    model_number: str | None = None


def product_url(box_id: str) -> str:
    return f"{SITE_ORIGIN}/product-detail?id={box_id}"


def search_query_from_title(title: str | None) -> str:
    if not title:
        return ""
    text = _TITLE_NOISE.sub(" ", title)
    text = re.sub(r"[^\w\s&+\-/]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90]


def _tokens(text: str) -> list[str]:
    return [m.group(0).lower() for m in _TOKEN.finditer(text)]


def _significant_tokens(text: str) -> set[str]:
    return {t for t in _tokens(text) if len(t) >= 3 and t not in _GENERIC_TOKENS}


def leading_brand(title: str | None) -> str | None:
    """First non-generic token — usually the Amazon listing brand."""
    if not title:
        return None
    for token in _tokens(title):
        if token in _SKIP_LEADING_BRAND:
            continue
        if token in _GENERIC_TOKENS:
            continue
        if len(token) >= 3:
            return token
    return None



# Conservative checks for variants that change CeX's cash offer. If either
# listing explicitly specifies one of these features, both must agree.
_CAPACITY_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:gb|tb)\b", re.I)
_MODEL_RE = re.compile(
    r"\b(?:iphone\s*\d{1,2}|galaxy\s*[sa]\d{2}|pixel\s*\d{1,2}|"
    r"arctis\s*\d{1,3}[a-z]?|rtx\s*\d{4}(?:\s*(?:ti|super))?|"
    r"gtx\s*\d{3,4}|rx\s*\d{4}(?:\s*xt)?)\b",
    re.I,
)
_PLATFORM_RE = re.compile(
    r"\b(?:ps\s*[345]|playstation\s*[345]|xbox\s*(?:one|series\s*[sx])|"
    r"nintendo\s+switch(?:\s*2)?|switch(?:\s*2)?(?=\s*(?:console|oled|lite|\)|\])))\b",
    re.I,
)
_GAME_EDITION_RE = re.compile(r"\b(?:fifa|fc|nba|nhl|wwe|f1)\s*(\d{2,4})\b", re.I)
_PACK_RE = re.compile(r"\b(\d+)\s*[- ]?\s*(?:pack|pk)\b|\bpack\s+of\s+(\d+)\b", re.I)
_NON_TRADEABLE_RE = re.compile(
    r"\b(?:digital\s+(?:download|code|key)|download\s+code|"
    r"activation\s+(?:code|key)|redeem\s+code|gift\s+card|subscription|dlc)\b",
    re.I,
)
_EDITION_TOKENS = frozenset(
    {"pro", "plus", "max", "ultra", "lite", "mini", "oled", "slim", "digital", "disc", "disk"}
)


def _identifiers(pattern: re.Pattern, title: str) -> set[str]:
    return {
        re.sub(r"\s+", "", match.group(0).lower())
        .replace("playstation", "ps")
        .replace("nintendoswitch", "switch")
        .replace("disk", "disc")
        for match in pattern.finditer(title)
    }


def _pack_sizes(title: str) -> set[int]:
    return {int(match.group(1) or match.group(2)) for match in _PACK_RE.finditer(title)}


def _editions(title: str) -> set[str]:
    result = set(_tokens(title)) & _EDITION_TOKENS
    if "disk" in result:
        result.remove("disk")
        result.add("disc")
    return result


def _variants_compatible(amazon_title: str, cex_title: str) -> bool:
    """Reject obvious SKU, platform, pack-size and digital-only mismatches."""
    if _NON_TRADEABLE_RE.search(amazon_title):
        return False

    # A brand alone must never join mouse mats, replacement pads or cases to
    # the main device. These mismatches occurred in production research rows.
    for pattern in (r"\bmouse\s*(?:pad|mat)\b", r"\bear\s*(?:pads?|cushions?)\b",
                    r"\b(?:case|cover)\b", r"\b(?:replacement|compatible with)\b"):
        if bool(re.search(pattern, amazon_title, re.I)) != bool(re.search(pattern, cex_title, re.I)):
            return False
    for pattern in (r"\bg\d{3,4}(?:\s*x)?\b", r"\bwh[ -]?1000xm\d\b"):
        if _identifiers(re.compile(pattern, re.I), amazon_title) != _identifiers(re.compile(pattern, re.I), cex_title):
            return False
    colours = {"black", "white", "grey", "silver", "gold", "blue", "red", "green", "pink", "purple"}
    a_colours = set(_tokens(amazon_title.lower().replace("gray", "grey"))) & colours
    c_colours = set(_tokens(cex_title.lower().replace("gray", "grey"))) & colours
    if a_colours and c_colours and a_colours != c_colours:
        return False

    for pattern in (_CAPACITY_RE, _MODEL_RE, _PLATFORM_RE):
        if _identifiers(pattern, amazon_title) != _identifiers(pattern, cex_title):
            return False

    amazon_years = {re.sub(r"\s+", "", match.group(0).lower()) for match in _GAME_EDITION_RE.finditer(amazon_title)}
    cex_years = {re.sub(r"\s+", "", match.group(0).lower()) for match in _GAME_EDITION_RE.finditer(cex_title)}
    if amazon_years != cex_years:
        return False

    if _pack_sizes(amazon_title) != _pack_sizes(cex_title):
        return False
    return _editions(amazon_title) == _editions(cex_title)


def titles_match_for_trade_in(amazon_title: str | None, cex_box_name: str | None) -> bool:
    """Reject Algolia hits that only share generic words (e.g. Ozeino headset → SteelSeries)."""
    if not amazon_title or not cex_box_name:
        return False
    if not _variants_compatible(amazon_title, cex_box_name):
        return False
    amazon_l = amazon_title.lower()
    cex_l = cex_box_name.lower()
    brand = leading_brand(amazon_title)
    family = {'sony': r'\b(?:sony|playstation|ps[345])',
              'microsoft': r'\b(?:microsoft|xbox)\b',
              'nintendo': r'\b(?:nintendo|switch)\b'}
    if brand and len(brand) >= 4 and brand not in _SKIP_LEADING_BRAND:
        if brand not in cex_l and not (brand in family and re.search(family[brand], cex_l)):
            return False

    amazon_brands = _significant_tokens(amazon_l) & _KNOWN_BRANDS
    cex_brands = _significant_tokens(cex_l) & _KNOWN_BRANDS
    for name, pattern in family.items():
        if re.search(pattern, amazon_l):
            amazon_brands.add(name)
        if re.search(pattern, cex_l):
            cex_brands.add(name)
    if amazon_brands and cex_brands and amazon_brands.isdisjoint(cex_brands):
        return False

    if _identifiers(_MODEL_RE, amazon_title):
        # Model family, capacity, edition and platform already agree above.
        return True
    if _identifiers(_PLATFORM_RE, amazon_title) and re.search(r'\bconsole\b', amazon_l) and re.search(r'\bconsole\b', cex_l):
        return True

    overlap = (_significant_tokens(amazon_l) & _significant_tokens(cex_l)) - _KNOWN_BRANDS
    if brand and _tokens(amazon_l)[0] == brand:
        overlap.discard(brand)
    if not overlap:
        return False
    # One distinctive shared token (model number, product line) is enough once brand checks pass.
    if any(len(t) >= 5 for t in overlap):
        return True
    return len(overlap) >= 2


def pick_best_box_hit(amazon_title: str | None, hits: list[dict]) -> dict | None:
    for hit in hits:
        name = hit.get("boxName") or hit.get("box_name") or ""
        if titles_match_for_trade_in(amazon_title, str(name)):
            return hit
    return None


async def search_boxes(client: httpx.AsyncClient, query: str, *, hits_per_page: int = 3,
                       strict: bool = False, page: int = 0,
                       min_cash_eur: float | None = None) -> list[dict]:
    if not query.strip():
        return []
    url = f"https://search.webuy.io/1/indexes/{ALGOLIA_INDEX}/query"
    body = {
        "query": query,
        "hitsPerPage": hits_per_page,
        "attributesToRetrieve": [
            "boxId",
            "boxName",
            "categoryName",
            "sellPrice",
            "cashBuyPrice",
            "cashPriceCalculated", "popularityScore",
            "boxWebBuyAllowed",
            "ean", "upc", "gtin", "barcode", "barcodes", "modelNumber",
        ],
        "numericFilters": ["boxVisibilityOnWeb=1"],
    }
    if page:
        body["page"] = max(0, int(page))
    if min_cash_eur is not None:
        body["numericFilters"].append([f"cashBuyPrice>{float(min_cash_eur):g}",
                                       f"cashPriceCalculated>{float(min_cash_eur):g}"])
    headers = {
        **CEX_HEADERS,
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_SEARCH_KEY,
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("hits"), list):
            if strict:
                raise ValueError("Unexpected CeX search response; not a confirmed empty result")
            return []
        return payload["hits"]
    except httpx.HTTPError as exc:
        logger.warning("CeX search failed for %r: %s", query[:60], exc)
        if strict:
            raise
        return []


def search_cash_price(hit: dict) -> float:
    """CeX computes most normal buy offers; cashBuyPrice alone is often zero."""
    for key in ("cashPriceCalculated", "cashBuyPrice"):
        try:
            value = float(hit.get(key) or 0)
        except (TypeError, ValueError, OverflowError):
            continue
        if math.isfinite(value) and value > 0:
            return value
    return 0


async def fetch_box_prices(client: httpx.AsyncClient, box_id: str, *, strict: bool = False) -> CexPrices | None:
    url = f"{WSS_BASE}/boxes/{box_id}/detail"
    try:
        resp = await client.get(url, params={"platformId": PLATFORM_ID}, headers=CEX_HEADERS, timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("CeX box detail failed for %s: %s", box_id, exc)
        if strict:
            raise
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get("response"), dict):
        if strict:
            raise ValueError("Unexpected CeX box detail response")
        return None
    if payload["response"].get("ack") != "Success":
        if strict:
            raise ValueError("CeX box details not acknowledged")
        return None
    details = (payload["response"].get("data") or {}).get("boxDetails") or []
    if not details:
        return None
    box = details[0]
    if not box.get("boxWebBuyAllowed", 1):
        return None
    cash = float(box.get("cashPrice") or 0)
    voucher = float(box.get("exchangePrice") or 0)
    retail = float(box.get("sellPrice") or 0)
    if cash <= 0 and voucher <= 0:
        return None
    return CexPrices(
        box_id=str(box.get("boxId") or box_id),
        box_name=str(box.get("boxName") or box_id),
        category_name=box.get("categoryName"),
        cash_eur=cash,
        voucher_eur=voucher,
        retail_eur=retail,
        url=product_url(str(box.get("boxId") or box_id)),
        barcodes=catalog_barcodes(box),
        model_number=cex_model_number(box),
    )
