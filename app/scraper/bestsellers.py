import re
from dataclasses import dataclass, field

from bs4 import BeautifulSoup

from app.scraper.amazon_ie import AMAZON_IE_BASE, _parse_price, normalize_product_url

BLOCK_MARKERS = ("/errors/validateCaptcha", "api-services-support@amazon.com", "Type the characters you see")


@dataclass
class BestsellerItem:
    asin: str
    title: str | None
    price_eur: float | None
    rank: int | None
    url: str


@dataclass
class BestsellerPage:
    category: str | None
    items: list[BestsellerItem] = field(default_factory=list)
    child_paths: list[str] = field(default_factory=list)
    blocked: bool = False


def normalize_category_path(value: str) -> str:
    """Accept 'home', '/gp/bestsellers/home/123', or a full amazon.ie URL."""
    text = value.strip()
    if text.startswith(AMAZON_IE_BASE):
        text = text[len(AMAZON_IE_BASE):]
    text = text.split("?")[0].split("/ref=")[0].rstrip("/")
    if not text.startswith("/"):
        text = f"/gp/bestsellers/{text}" if text else "/gp/bestsellers"
    return text


def _category_name(soup: BeautifulSoup) -> str | None:
    title = soup.title.get_text(strip=True) if soup.title else ""
    marker = "most popular items in "
    if marker in title:
        name = title.split(marker, 1)[1].strip()
        if name and name != "undefined":
            return name
    return None


def _item_from_block(block) -> BestsellerItem | None:
    asin = (block.get("data-asin") or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{10}", asin):
        return None

    title_el = block.select_one('div[class*="line-clamp"]') or block.select_one("img[alt]")
    if title_el is None:
        title = None
    elif title_el.name == "img":
        title = title_el.get("alt") or None
    else:
        title = title_el.get_text(" ", strip=True) or None

    # Items with several prices are variation ranges ("€9.99 - €24.99"); their
    # low end jumps around between variants, which would look like a price drop.
    prices = block.select('span[class*="p13n-sc-price"]')
    price = _parse_price(prices[0].get_text(strip=True)) if len(prices) == 1 else None

    rank_el = block.select_one("span.zg-bdg-text")
    rank = None
    if rank_el:
        m = re.search(r"\d+", rank_el.get_text())
        rank = int(m.group()) if m else None

    return BestsellerItem(asin=asin, title=title, price_eur=price, rank=rank, url=normalize_product_url(asin))


def _child_paths(soup: BeautifulSoup) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for a in soup.select('div[role="group"] a[href*="/gp/bestsellers"]'):
        href = a.get("href") or ""
        # Children use ref=zg_bs_nav_*; parents up the tree use ref=zg_bs_unv_*.
        if "zg_bs_nav" not in href:
            continue
        path = normalize_category_path(href)
        if path not in seen:
            seen.add(path)
            out.append(path)
    return out


def parse_bestseller_page(html: str) -> BestsellerPage:
    if any(marker in html for marker in BLOCK_MARKERS):
        return BestsellerPage(category=None, blocked=True)

    soup = BeautifulSoup(html, "lxml")
    items: list[BestsellerItem] = []
    seen: set[str] = set()
    for block in soup.select("div[data-asin]"):
        item = _item_from_block(block)
        if item and item.asin not in seen:
            seen.add(item.asin)
            items.append(item)
    return BestsellerPage(category=_category_name(soup), items=items, child_paths=_child_paths(soup))
