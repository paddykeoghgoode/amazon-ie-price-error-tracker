from dataclasses import dataclass
import re

import httpx
from bs4 import BeautifulSoup

AMAZON_IE_BASE = "https://www.amazon.ie"
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IE,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@dataclass
class ProductSnapshot:
    asin: str
    url: str
    title: str | None
    price_eur: float | None
    list_price_eur: float | None
    currency: str | None
    availability: str | None


ASIN_RE = re.compile(r"/(?:dp|gp/product)/([A-Z0-9]{10})", re.I)
ASIN_QUERY_RE = re.compile(r"[?&]asin=([A-Z0-9]{10})", re.I)


def extract_asin(url_or_asin: str) -> str | None:
    text = url_or_asin.strip()
    if re.fullmatch(r"[A-Z0-9]{10}", text, re.I):
        return text.upper()
    m = ASIN_RE.search(text)
    if m:
        return m.group(1).upper()
    m = ASIN_QUERY_RE.search(text)
    if m:
        return m.group(1).upper()
    return None


def normalize_product_url(asin: str) -> str:
    return f"{AMAZON_IE_BASE}/dp/{asin}"


def _parse_price(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"\d[\d,.\s\xa0]*", text)
    if not m:
        return None
    number = re.sub(r"[\s\xa0]", "", m.group()).rstrip(",.")
    if "," in number and "." in number:
        # Whichever separator comes last is the decimal point: 1,299.99 or 1.299,99
        if number.rfind(",") > number.rfind("."):
            number = number.replace(".", "").replace(",", ".")
        else:
            number = number.replace(",", "")
    elif "," in number:
        number = number.replace(",", ".") if re.search(r",\d{1,2}$", number) else number.replace(",", "")
    elif number.count(".") > 1 or re.search(r"\.\d{3}$", number):
        number = number.replace(".", "")
    try:
        return float(number)
    except ValueError:
        return None


def _first_price(soup: BeautifulSoup, selectors: list[str]) -> float | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            val = _parse_price(el.get_text(" ", strip=True))
            if val is not None:
                return val
            val = _parse_price(el.get("content"))
            if val is not None:
                return val
    return None


def parse_product_html(html: str, asin: str, url: str) -> ProductSnapshot:
    soup = BeautifulSoup(html, "lxml")

    title_el = soup.select_one("#productTitle") or soup.select_one("span#title")
    title = title_el.get_text(strip=True) if title_el else None

    price = _first_price(
        soup,
        [
            "#corePriceDisplay_desktop_feature_div span.a-offscreen",
            "#corePrice_feature_div span.a-offscreen",
            "#priceblock_ourprice",
            "#priceblock_dealprice",
            "span.a-price[data-a-color='price'] span.a-offscreen",
            "span.a-price span.a-offscreen",
        ],
    )

    list_price = _first_price(
        soup,
        [
            "span.a-price.a-text-price span.a-offscreen",
            "#basisPriceLegalMessage ~ span.a-price span.a-offscreen",
            ".a-text-strike span.a-offscreen",
            "#listPrice",
            "span[data-a-strike='true'] span.a-offscreen",
        ],
    )

    if list_price and price and list_price <= price:
        list_price = None

    avail_el = soup.select_one("#availability") or soup.select_one("#outOfStock")
    availability = avail_el.get_text(" ", strip=True) if avail_el else None

    return ProductSnapshot(
        asin=asin,
        url=url,
        title=title,
        price_eur=price,
        list_price_eur=list_price,
        currency="EUR" if price is not None else None,
        availability=availability,
    )


async def fetch_product(client: httpx.AsyncClient, url_or_asin: str) -> ProductSnapshot:
    asin = extract_asin(url_or_asin)
    if not asin:
        raise ValueError("Could not parse ASIN from URL or input")

    url = normalize_product_url(asin)
    resp = await client.get(url, follow_redirects=True)
    resp.raise_for_status()
    return parse_product_html(resp.text, asin, str(resp.url))
