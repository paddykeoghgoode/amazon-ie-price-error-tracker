"""Conservative model-verified Irish retailer comparison regression tests."""
from __future__ import annotations

import asyncio
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import httpx

from app.services.retail_compare import (
    _make_summary,
    compare_watchlist,
    fetch_retail_offer,
    parse_retail_html,
    parse_retail_spec,
    retailer_from_url,
    trusted_retailer_url,
)
from scripts.scan import combine_retail_specs, parse_issue_retail_spec

MERCHANT = "https://www.petermurphyelectrical.ie/products/sage-ses875btr2guk1"
MPN = "SES875BTR2GUK1"
EAN = "9312432032056"


def product_html(*, model=MPN, barcode=EAN, price="499.00", availability="InStock",
                 offers=None, name=None):
    obj = {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": name or f"Sage Barista Express Black Truffle {model}",
        "mpn": model,
        "gtin13": barcode,
        "offers": offers or {
            "@type": "Offer", "price": price, "priceCurrency": "EUR",
            "availability": "https://schema.org/" + availability,
        },
    }
    return '<html><script type="application/ld+json">' + json.dumps(obj) + "</script></html>"


class TestRetailerParser(unittest.TestCase):
    def test_exact_model_offer_eur_and_in_stock(self):
        row = parse_retail_html(product_html(), expected_model=MPN, expected_barcode=None)
        self.assertEqual(row["status"], "matched")
        self.assertEqual(row["identity"], "model_confirmed")
        self.assertEqual(row["price_eur"], 499.0)

    def test_ean_matches_even_if_model_not_given(self):
        row = parse_retail_html(product_html(), expected_model=None, expected_barcode=EAN)
        self.assertEqual(row["status"], "matched")
        self.assertEqual(row["identity"], "barcode_confirmed")

    def test_model_mismatch_and_barcode_mismatch_both_excluded(self):
        row = parse_retail_html(product_html(model="SES875BSS4GUK1"),
                                expected_model=MPN, expected_barcode=None)
        self.assertEqual(row["status"], "model_mismatch")
        row = parse_retail_html(product_html(barcode="9312432024686"),
                                expected_model=None, expected_barcode=EAN)
        self.assertEqual(row["status"], "barcode_mismatch")
        conflict = parse_retail_html(product_html(model="SES875BSS4GUK1"),
                                     expected_model=MPN, expected_barcode=EAN)
        self.assertEqual(conflict["status"], "model_mismatch")

    def test_missing_identity_does_not_make_a_buy_signal(self):
        row = parse_retail_html(product_html(), expected_model=None, expected_barcode=None)
        self.assertEqual(row["status"], "identity_required")

    def test_multiple_variants_cannot_produce_lowest_price(self):
        offers = [
            {"@type": "Offer", "price": "499", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
            {"@type": "Offer", "price": "799", "priceCurrency": "EUR", "availability": "https://schema.org/InStock"},
        ]
        row = parse_retail_html(product_html(offers=offers),
                                expected_model=MPN, expected_barcode=EAN)
        self.assertEqual(row["status"], "ambiguous_variants")

    def test_wrong_currency_and_sold_out_not_actionable(self):
        row = parse_retail_html(product_html(availability="OutOfStock"),
                                expected_model=MPN, expected_barcode=EAN)
        self.assertEqual(row["status"], "out_of_stock")
        html = product_html(offers={
            "@type": "Offer", "price": "499", "priceCurrency": "GBP",
            "availability": "https://schema.org/InStock",
        })
        self.assertEqual(
            parse_retail_html(html, expected_model=MPN, expected_barcode=EAN)["status"],
            "price_unavailable",
        )

    def test_aggregate_lowprice_is_not_a_current_offer(self):
        html = product_html(offers={
            "@type": "AggregateOffer", "lowPrice": "10", "priceCurrency": "EUR"
        })
        self.assertEqual(
            parse_retail_html(html, expected_model=MPN, expected_barcode=None)["status"],
            "price_unavailable",
        )

    def test_rejects_unknown_domains_and_unsafe_urls(self):
        self.assertEqual(retailer_from_url(MERCHANT), "Peter Murphy Electrical")
        for link in (
            "http://www.currys.ie/p/1",
            "https://currys.ie.evil.example/item",
            "https://127.0.0.1/item",
            "https://user:pass@currys.ie/item",
            "https://currys.ie:8080/item",
            "https://example.com/product",
        ):
            self.assertFalse(trusted_retailer_url(link), link)

    def test_manual_rrp_not_inferred_from_advertised_was_price(self):
        s = parse_retail_spec({"model": MPN, "retailer_urls": [MERCHANT, MERCHANT]})
        self.assertIsNone(s["rrp_eur"])
        self.assertEqual(s["retailer_urls"], [MERCHANT])
        supplied = parse_retail_spec({"rrp_eur": "599.99", "rrp_source": "https://www.sageappliances.com/eu/product"})
        self.assertEqual(supplied["rrp_eur"], 599.99)
        self.assertEqual(supplied["rrp_source"], "https://www.sageappliances.com/eu/product")
        self.assertIsNone(parse_retail_spec({"rrp_eur": -50})["rrp_eur"])
        self.assertEqual(parse_retail_spec({"rrp_eur": "€599,99"})["rrp_eur"], 599.99)
        self.assertEqual(parse_retail_spec({"rrp_eur": "€1.299,99"})["rrp_eur"], 1299.99)
        self.assertEqual(parse_retail_spec({"rrp_eur": "€1,299.99"})["rrp_eur"], 1299.99)
        self.assertIsNone(parse_retail_spec({"rrp_source": "https://["})["rrp_source"])

    def test_compare_issue_parses_direct_links_and_preserves_user_supplied_rrp(self):
        body = ("https://www.amazon.ie/dp/B0AAA11111\n"
                "Model: SES875BTR2GUK1\nEAN: 9312432032056\n"
                "RRP: €599.99\nRRP source: https://www.sageappliances.com/eu/example\n"
                f"Compare: {MERCHANT}\nCompare: https://evil.example/item")
        spec = parse_issue_retail_spec(body)
        self.assertEqual(spec["model"], MPN)
        self.assertEqual(spec["barcode"], EAN)
        self.assertEqual(spec["rrp_eur"], 599.99)
        self.assertEqual(spec["retailer_urls"], [MERCHANT])
        combined = combine_retail_specs({}, [{"asin": "B0AAA11111", "retail_spec": spec}])
        self.assertEqual(combined["B0AAA11111"]["model"], MPN)

    def test_price_lead_not_claimed_as_best_delivered_price(self):
        spec = parse_retail_spec({"model": MPN, "rrp_eur": 599.99})
        offers = [
            {"status": "matched", "retailer": "DID", "price_eur": 499.99},
            {"status": "out_of_stock", "retailer": "Currys", "price_eur": 399},
            {"status": "model_mismatch", "retailer": "Harvey Norman", "price_eur": 250},
        ]
        d = _make_summary(spec, offers, 636.49, "2026-11-03T00:00:00+00:00")
        self.assertEqual(d["matched_offer_count"], 1)
        self.assertEqual(d["lowest_checked_retailer_price_eur"], 499.99)
        self.assertEqual(d["amazon_minus_lowest_retail_eur"], 136.50)
        self.assertIn("delivery", d["basis"])


class TestRetailerRequests(unittest.TestCase):
    def test_redirect_to_disallowed_host_never_fetched(self):
        calls = []
        def respond(req):
            calls.append(str(req.url))
            return httpx.Response(302, headers={"Location": "https://127.0.0.1/private"})
        async def check():
            async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
                return await fetch_retail_offer(
                    client, MERCHANT, expected_model=MPN, expected_barcode=None
                )
        row = asyncio.run(check())
        self.assertEqual(row["status"], "unsafe_redirect")
        self.assertEqual(calls, [MERCHANT])

    def test_structured_product_can_be_fetched_and_cached(self):
        calls = []
        def respond(req):
            calls.append(str(req.url))
            return httpx.Response(200, text=product_html(), headers={"Content-Type": "text/html"})
        async def once(prev=None, model=MPN):
            products = [{"asin": "B0AAA11111", "price_eur": 636.49, "model_number": model}]
            specs = {"B0AAA11111": {"model": model, "retailer_urls": [MERCHANT], "rrp_eur": 599}}
            constructor = httpx.AsyncClient
            with patch("app.services.retail_compare.httpx.AsyncClient") as make_client, patch(
                "app.services.retail_compare.asyncio.sleep", new_callable=AsyncMock
            ):
                make_client.return_value.__aenter__.return_value = constructor(
                    transport=httpx.MockTransport(respond))
                await compare_watchlist(products, specs, prev)
            return products[0]
        first = asyncio.run(once())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["retail_comparison"]["matched_offer_count"], 1)
        self.assertEqual(first["retail_comparison"]["amazon_minus_lowest_retail_eur"], 137.49)
        second = asyncio.run(once({"products": [first]}))
        self.assertEqual(len(calls), 1, "Frequent watchlist ticks reuse a fresh quote")
        self.assertEqual(second["retail_comparison"]["offers"][0]["price_eur"], 499)

    def test_no_manual_links_means_no_retailer_requests(self):
        async def once():
            products = [{"asin": "B0AAA11111", "price_eur": 300}]
            with patch("app.services.retail_compare.fetch_retail_offer", new_callable=AsyncMock) as fetch:
                await compare_watchlist(products, {}, None)
                fetch.assert_not_awaited()
            self.assertEqual(products[0]["retail_comparison"]["matched_offer_count"], 0)
        asyncio.run(once())


if __name__ == "__main__":
    unittest.main()
