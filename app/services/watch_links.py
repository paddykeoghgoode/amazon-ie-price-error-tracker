"""Resolve Amazon short links safely for GitHub-issue watch requests.

A short URL does not contain the product's ASIN. Only known Amazon hosts are
eligible throughout redirects; never forward a GitHub token to a merchant.
"""
from __future__ import annotations

import re
from urllib.parse import urljoin, urlsplit

import httpx

from app.scraper.amazon_ie import DEFAULT_HEADERS, extract_asin

SHORT_HOSTS = frozenset({"amzn.eu", "www.amzn.eu", "amzn.to", "www.amzn.to"})
AMAZON_TLDS = frozenset({
    "ie", "co.uk", "com", "de", "fr", "it", "es", "nl", "be", "pl", "se",
    "ca", "com.au", "co.jp", "com.mx", "com.br",
})
AMAZON_HOSTS = frozenset(
    f"{prefix}amazon.{tld}"
    for prefix in ("", "www.", "smile.", "m.")
    for tld in AMAZON_TLDS
)
MAX_REDIRECTS = 6
LINK_RE = re.compile(r"https?://[^\s<>\"']+", re.I)


def allowed_link(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname is not None
            and parsed.hostname.lower() in (SHORT_HOSTS | AMAZON_HOSTS)
            and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443)
        )
    except ValueError:
        return False


def shortlinks_in_text(*parts: str | None) -> list[str]:
    found = []
    seen = set()
    for part in parts:
        for match in LINK_RE.findall(part or ""):
            url = match.rstrip(".,;)>]}")
            try:
                host = urlsplit(url).hostname
            except ValueError:
                continue
            if host and host.lower() in SHORT_HOSTS and allowed_link(url) and url not in seen:
                seen.add(url)
                found.append(url)
    return found


async def _redirect(client: httpx.AsyncClient, url: str, method: str) -> tuple[int, str | None]:
    if method == "HEAD":
        response = await client.head(url, follow_redirects=False)
        return response.status_code, response.headers.get("location")
    # Do not download the entire Amazon product page merely to find its ASIN.
    async with client.stream("GET", url, follow_redirects=False) as response:
        return response.status_code, response.headers.get("location")


async def resolve_amazon_shortlink(
    client: httpx.AsyncClient, original: str,
) -> str | None:
    """Follow at most six trusted HTTPS redirect hops and return the ASIN.

    Return None on unavailable or unrecognised destinations. Reject arbitrary
    destinations before sending *any* request (SSRF and token-leak protection).
    The caller should use a separate unauthenticated client.
    """
    if not allowed_link(original):
        return None
    host = urlsplit(original).hostname or ""
    if host.lower() not in SHORT_HOSTS:
        return None
    url = original
    visited: set[str] = set()
    for _ in range(MAX_REDIRECTS):
        if url in visited or not allowed_link(url):
            return None
        visited.add(url)
        hostname = (urlsplit(url).hostname or "").lower()
        if hostname in AMAZON_HOSTS:
            asin = extract_asin(url)
            if asin:
                return asin
        try:
            status, location = await _redirect(client, url, "HEAD")
            if status in (200, 403, 405, 501) and not location:
                # Some shorteners do not support HEAD; GET streamed only.
                status, location = await _redirect(client, url, "GET")
        except httpx.HTTPError:
            return None
        if status not in (301, 302, 303, 307, 308) or not location:
            return None
        following = urljoin(url, location)
        if not allowed_link(following):
            return None
        url = following
    # Handle a final Amazon URL without requesting the entire page.
    if allowed_link(url) and (urlsplit(url).hostname or "").lower() in AMAZON_HOSTS:
        return extract_asin(url)
    return None


def public_issue_url(repo: str, number: int) -> str:
    return f"https://github.com/{repo}/issues/{int(number)}"


def safe_amazon_url(asin: str) -> str:
    return f"https://www.amazon.ie/dp/{asin}"
