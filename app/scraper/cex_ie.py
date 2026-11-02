from __future__ import annotations

import logging
import re
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


@dataclass(frozen=True)
class CexPrices:
    box_id: str
    box_name: str
    category_name: str | None
    cash_eur: float
    voucher_eur: float
    retail_eur: float
    url: str


def product_url(box_id: str) -> str:
    return f"{SITE_ORIGIN}/product-detail?id={box_id}"


def search_query_from_title(title: str | None) -> str:
    if not title:
        return ""
    text = _TITLE_NOISE.sub(" ", title)
    text = re.sub(r"[^\w\s&+\-/]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:90]


async def search_boxes(client: httpx.AsyncClient, query: str, *, hits_per_page: int = 3) -> list[dict]:
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
            "boxWebBuyAllowed",
        ],
        "numericFilters": ["boxVisibilityOnWeb=1"],
    }
    headers = {
        **CEX_HEADERS,
        "X-Algolia-Application-Id": ALGOLIA_APP_ID,
        "X-Algolia-API-Key": ALGOLIA_SEARCH_KEY,
        "Content-Type": "application/json",
    }
    try:
        resp = await client.post(url, json=body, headers=headers, timeout=20.0)
        resp.raise_for_status()
        return resp.json().get("hits") or []
    except httpx.HTTPError as exc:
        logger.warning("CeX search failed for %r: %s", query[:60], exc)
        return []


async def fetch_box_prices(client: httpx.AsyncClient, box_id: str) -> CexPrices | None:
    url = f"{WSS_BASE}/boxes/{box_id}/detail"
    try:
        resp = await client.get(url, params={"platformId": PLATFORM_ID}, headers=CEX_HEADERS, timeout=20.0)
        resp.raise_for_status()
        payload = resp.json()
    except httpx.HTTPError as exc:
        logger.warning("CeX box detail failed for %s: %s", box_id, exc)
        return None

    if payload.get("response", {}).get("ack") != "Success":
        return None
    details = payload["response"]["data"].get("boxDetails") or []
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
    )
