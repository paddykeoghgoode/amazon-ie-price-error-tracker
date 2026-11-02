"""Watch issue intake: amzn.eu redirects, status, URL safety and mobile issues."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.scraper.amazon_ie import extract_asin
from app.services.watch_links import (
    allowed_link, resolve_amazon_shortlink, shortlinks_in_text,
)
from scripts import scan


SHORT = "https://amzn.eu/d/0ihPesHK"
CANONICAL = "https://www.amazon.co.uk/gp/aw/d/B0CX1234AB?th=1"


class TestSafeAmazonShortlinks(unittest.TestCase):
    def test_resolves_amzn_eu_redirect_without_downloading_product_page(self):
        requests = []
        def handler(request):
            requests.append((request.method, str(request.url)))
            if str(request.url) == SHORT:
                return httpx.Response(302, headers={"Location": CANONICAL})
            return httpx.Response(500)
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await resolve_amazon_shortlink(client, SHORT)
        self.assertEqual(asyncio.run(check()), "B0CX1234AB")
        self.assertEqual(requests, [("HEAD", SHORT)])
        self.assertEqual(extract_asin(CANONICAL), "B0CX1234AB")

    def test_retries_with_streamed_get_when_head_is_not_supported(self):
        seen = []
        def handler(request):
            seen.append(request.method)
            if request.method == "HEAD":
                return httpx.Response(405)
            return httpx.Response(301, headers={
                "Location": "https://www.amazon.ie/dp/B0CX1234AB"
            })
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await resolve_amazon_shortlink(client, SHORT)
        self.assertEqual(asyncio.run(check()), "B0CX1234AB")
        self.assertEqual(seen, ["HEAD", "GET"])

    def test_blocks_unsafe_redirect_host_and_never_requests_it(self):
        calls = []
        def handler(request):
            calls.append(str(request.url))
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
                return await resolve_amazon_shortlink(client, SHORT)
        self.assertIsNone(asyncio.run(check()))
        self.assertEqual(calls, [SHORT])
        for bad in (
            "https://evil.invalid/d/0ihPesHK",
            "http://amzn.eu/d/0ihPesHK",
            "https://amzn.eu.evil.invalid/d/0ihPesHK",
            "https://foo@amzn.eu/d/0ihPesHK",
            "https://amzn.eu:1234/d/0ihPesHK",
            "https://127.0.0.1/d/0ihPesHK",
        ):
            self.assertFalse(allowed_link(bad))

    def test_extracts_only_trusted_short_links_once(self):
        text = f"Watch: {SHORT} and {SHORT}\nhttps://evil.invalid/d/short"
        self.assertEqual(shortlinks_in_text(text), [SHORT])


class TestWatchIssueIntake(unittest.TestCase):
    def test_title_without_label_is_watch_issue_and_shortlink_is_resolved(self):
        issue = {"number": 25, "title": f"Watch: {SHORT}", "body": SHORT, "labels": []}
        self.assertTrue(scan._is_watch_issue(issue))
        requests = []

        def handler(request):
            requests.append((str(request.url), request.headers.get("authorization")))
            if request.url.host == "api.github.com":
                self.assertEqual(request.headers.get("authorization"), "Bearer test-github-token")
                return httpx.Response(200, json=[issue])
            self.assertIsNone(request.headers.get("authorization"),
                              "Never forward GitHub token to Amazon")
            return httpx.Response(302, headers={
                "Location": "https://www.amazon.ie/dp/B0CX1234AB"
            })

        constructor = httpx.AsyncClient
        def client_factory(*args, **kwargs):
            return constructor(*args, **kwargs, transport=httpx.MockTransport(handler))

        async def check(previous=None):
            statuses = []
            asins = await scan.fetch_watch_issue_urls(
                "paddykeoghgoode/amazon-ie-price-error-tracker", "test-github-token",
                previous_submissions=previous, submissions=statuses,
            )
            return asins, statuses

        with patch("scripts.scan.httpx.AsyncClient", side_effect=client_factory):
            asins, statuses = asyncio.run(check())
            self.assertEqual(asins, ["B0CX1234AB"])
            self.assertEqual(statuses[0]["issue_number"], 25)
            self.assertEqual(statuses[0]["status"], "pending_scan")
            self.assertEqual(statuses[0]["asin"], "B0CX1234AB")
            prior = statuses
            requests.clear()
            asins, statuses = asyncio.run(check(prior))
            self.assertEqual(asins, ["B0CX1234AB"])
            self.assertEqual(len(requests), 1, "Repeat scans use cached ASIN")
            self.assertIn("api.github.com", requests[0][0])

    def test_invalid_shortlink_is_reported_not_silently_dropped(self):
        issue = {"number": 25, "title": f"Watch: {SHORT}", "body": SHORT, "labels": []}
        constructor = httpx.AsyncClient
        def handler(request):
            if request.url.host == "api.github.com":
                return httpx.Response(200, json=[issue])
            return httpx.Response(403)
        def factory(*args, **kwargs):
            return constructor(*args, **kwargs, transport=httpx.MockTransport(handler))
        async def check():
            statuses = []
            asins = await scan.fetch_watch_issue_urls(
                "paddykeoghgoode/amazon-ie-price-error-tracker", "",
                submissions=statuses,
            )
            return asins, statuses
        with patch("scripts.scan.httpx.AsyncClient", side_effect=factory):
            urls, statuses = asyncio.run(check())
        self.assertEqual(urls, [])
        self.assertEqual(statuses[0]["status"], "link_unresolved")
        self.assertIsNone(statuses[0]["asin"])

    def test_report_statuses_for_success_and_amazon_fetch_error(self):
        items = [
            {"issue_number": 25, "asin": "B0CX1234AB", "status": "pending_scan"},
            {"issue_number": 26, "asin": "B0CX1234AC", "status": "pending_scan"},
            {"issue_number": 27, "asin": None, "status": "link_unresolved"},
        ]
        snapshot = {"products": [
            {"asin": "B0CX1234AB", "price_eur": 45.5, "status": "ok", "checked_at": "2026-11-02T23:30:00Z"},
            {"asin": "B0CX1234AC", "price_eur": None, "status": "fetch_error", "checked_at": "2026-11-02T23:30:00Z"},
        ]}
        output = scan.annotate_watch_submissions(items, snapshot)
        self.assertEqual([x["status"] for x in output],
                         ["tracking", "fetch_error", "link_unresolved"])

    def test_amazon_shortlink_in_local_watchlist_json(self):
        def handler(request):
            return httpx.Response(302, headers={
                "location": "https://www.amazon.ie/dp/B0CX1234AB"
            })
        constructor = httpx.AsyncClient
        def factory(*args, **kwargs):
            return constructor(*args, **kwargs, transport=httpx.MockTransport(handler))
        with patch("scripts.scan.httpx.AsyncClient", side_effect=factory):
            got = asyncio.run(scan.resolve_watchlist_entries([
                SHORT, "B0CX1234AB", "https://www.amazon.ie/dp/B0CX1234AB"
            ]))
        self.assertEqual(got, ["B0CX1234AB"])


if __name__ == "__main__":
    unittest.main()
