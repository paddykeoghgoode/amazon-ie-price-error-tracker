"""Exact identifiers from item detail fields, never arbitrary page text."""
from __future__ import annotations

import json
import re
from bs4 import BeautifulSoup

_BARCODE_LABELS = {"ean", "upc", "gtin", "barcode", "product ean"}
_MODEL_LABELS = {"item model number", "model number", "model", "manufacturer reference",
                 "manufacturer part number", "part number"}
_DETAIL_SELECTORS = (
    "#productDetails_techSpec_section_1",
    "#productDetails_detailBullets_sections1",
    "#detailBullets_feature_div",
    "#productOverview_feature_div",
)


def normalise_barcode(value: object) -> str | None:
    if value is None:
        return None
    raw = re.sub(r"[\s-]", "", str(value).strip())
    if not raw.isdigit() or len(raw) not in (8, 12, 13, 14):
        return None
    total = 0
    for index, digit in enumerate(reversed(raw[:-1])):
        total += int(digit) * (3 if index % 2 == 0 else 1)
    if (10 - total % 10) % 10 != int(raw[-1]):
        return None
    return raw.zfill(14)


def normalise_model(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    model = re.sub(r"[^a-z0-9]", "", value.lower())
    if 4 <= len(model) <= 40 and any(c.isdigit() for c in model):
        return model
    return None


def catalog_barcodes(mapping: dict) -> tuple[str, ...]:
    """Explicit GTIN fields only; a CeX box ID is NOT assumed to be an EAN."""
    collected = set()
    for key in ("ean", "eans", "upc", "gtin", "barcode", "barcodes", "productBarcode"):
        raw = mapping.get(key)
        values = raw if isinstance(raw, (list, tuple)) else [raw]
        for value in values:
            code = normalise_barcode(value)
            if code:
                collected.add(code)
    return tuple(sorted(collected))


def _label_values(soup: BeautifulSoup):
    for selector in _DETAIL_SELECTORS:
        for container in soup.select(selector):
            for tr in container.select("tr"):
                cells = tr.find_all(["th", "td"], recursive=False)
                if len(cells) >= 2:
                    yield cells[0].get_text(" ", strip=True), cells[1].get_text(" ", strip=True)
            for li in container.select("li"):
                label = li.select_one(".a-text-bold")
                if label:
                    text = li.get_text(" ", strip=True)
                    name = label.get_text(" ", strip=True)
                    yield name, text[len(name):].lstrip(" :")


def amazon_identifiers(soup: BeautifulSoup, asin: str) -> tuple[str | None, str | None]:
    """Return (GTIN, model). Never use identifiers in recommendations/reviews."""
    barcode = None
    model = None
    for label, value in _label_values(soup):
        clean = label.lower().strip(" :\u200e")
        if clean in _BARCODE_LABELS and not barcode:
            barcode = normalise_barcode(value)
        if clean in _MODEL_LABELS and not model:
            model = normalise_model(value)

    # Structured data is accepted only when it explicitly identifies this ASIN.
    for tag in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(tag.string or "")
        except (ValueError, TypeError):
            continue
        nodes = data if isinstance(data, list) else [data]
        for node in nodes:
            if not isinstance(node, dict):
                continue
            if node.get("sku", "").upper() != asin.upper() or node.get("@type") != "Product":
                continue
            if not barcode:
                for key in ("gtin14", "gtin13", "gtin12", "gtin8", "gtin"):
                    barcode = normalise_barcode(node.get(key))
                    if barcode:
                        break
            if not model:
                model = normalise_model(node.get("mpn") or node.get("model"))
    return barcode, model
