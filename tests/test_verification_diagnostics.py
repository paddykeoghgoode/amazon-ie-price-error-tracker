"""Verification diagnostics keep unsafe offers excluded but make failures explainable."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.scraper.amazon_ie import ProductSnapshot
from app.services.bestsellers import (
    verification_rejection_reason,
    verify_candidates,
)


def offer(
    *,
    price=49.99,
    seller="Amazon",
    seller_status="amazon",
    fulfilment="amazon",
    availability="In stock",
    title="Controller",
):
    return ProductSnapshot(
        asin="B000000001",
        url="https://www.amazon.ie/dp/B000000001",
        title=title,
        price_eur=price,
        list_price_eur=79.99,
        currency="EUR" if price is not None else None,
        availability=availability,
        seller_name=seller,
        seller_status=seller_status,
        fulfilment_status=fulfilment,
    )


class TestReasons(unittest.TestCase):
    def test_verified_retail_and_fba(self):
        self.assertIsNone(verification_rejection_reason(offer(), 49.99))
        self.assertIsNone(verification_rejection_reason(offer(
            seller="Shop LTD", seller_status="third_party", fulfilment="amazon"
        ), 49.99))

    def test_missing_price(self):
        self.assertEqual(
            verification_rejection_reason(offer(price=None), 49.99),
            "product_page_price_missing",
        )

    def test_changed_price(self):
        self.assertEqual(
            verification_rejection_reason(offer(price=69.99), 49.99),
            "price_changed_or_variant",
        )

    def test_unknown_seller(self):
        self.assertEqual(
            verification_rejection_reason(offer(seller=None, seller_status="unknown"), 49.99),
            "seller_not_detected",
        )

    def test_third_party_not_fulfilled_by_amazon(self):
        self.assertEqual(
            verification_rejection_reason(offer(
                seller="Shop LTD", seller_status="third_party", fulfilment="unknown"
            ), 49.99),
            "third_party_fulfilment_not_confirmed",
        )

    def test_out_of_stock(self):
        self.assertEqual(
            verification_rejection_reason(offer(availability="Currently unavailable"), 49.99),
            "unavailable",
        )

    def test_used_title(self):
        self.assertEqual(
            verification_rejection_reason(offer(title="Used controller"), 49.99),
            "used_or_refurbished",
        )


class TestVerificationStats(unittest.TestCase):
    def test_rejection_breakdown_and_samples(self):
        candidates = [
            {"asin": "B000000001", "price_eur": 49.99},
            {"asin": "B000000002", "price_eur": 49.99},
            {"asin": "B000000003", "price_eur": 49.99},
        ]
        snaps = [
            offer(),
            offer(seller=None, seller_status="unknown"),
            offer(price=69.99),
        ]

        async def exercise():
            with patch("app.services.bestsellers.fetch_product", new_callable=AsyncMock) as fetch:
                fetch.side_effect = snaps
                return await verify_candidates(None, candidates, limit=3)

        passed, stats = asyncio.run(exercise())
        self.assertEqual(len(passed), 1)
        self.assertEqual(stats["attempted"], 3)
        self.assertEqual(stats["amazon_verified"], 1)
        self.assertEqual(stats["rejected_or_unknown"], 2)
        self.assertEqual(
            stats["rejection_reasons"],
            {"seller_not_detected": 1, "price_changed_or_variant": 1},
        )
        self.assertEqual(len(stats["sample_failures"]), 2)
        self.assertNotIn("page_html", str(stats))

    def test_http_status_is_visible(self):
        req = httpx.Request("GET", "https://www.amazon.ie/dp/B000000001")
        res = httpx.Response(429, request=req)
        error = httpx.HTTPStatusError("Too Many Requests", request=req, response=res)

        async def exercise():
            with patch("app.services.bestsellers.fetch_product", new_callable=AsyncMock) as fetch:
                fetch.side_effect = error
                return await verify_candidates(None, [{"asin": "B000000001", "price_eur": 49.99}], limit=1)

        passed, stats = asyncio.run(exercise())
        self.assertEqual(passed, [])
        self.assertEqual(stats["rejection_reasons"], {"amazon_http_429": 1})


if __name__ == "__main__":
    unittest.main()
