"""Full Amazon catalogue: no hidden keyword/top-25 ceiling and safe resumable checks."""
import csv
import tempfile
import unittest
from pathlib import Path

from app.scraper.bestsellers import BestsellerItem
from app.services.bestsellers import CrawlResult
from app.services.catalogue import (
    apply_batch_results, catalogue_progress, make_section_catalogue,
    merge_catalogue, select_check_batch, write_catalogue_csv,
)


def crawl(prices):
    return CrawlResult(items={
        asin: (
            BestsellerItem(
                asin=asin, title=("Sony Headphones " + asin),
                url=f"https://www.amazon.ie/dp/{asin}", price_eur=price, rank=i,
            ),
            "Electronics",
        )
        for i, (asin, price) in enumerate(prices.items(), 1)
    })


class TestCatalogue(unittest.TestCase):
    def test_every_discovered_asin_is_preserved(self):
        data = make_section_catalogue(
            crawl({"A000000001": 40, "A000000002": 200, "A000000003": None}),
            None, section_id="gaming", section_name="Gaming", observed_at="2026-11-02T21:00:00+00:00",
        )
        self.assertEqual(len(data["items"]), 3)
        self.assertEqual(catalogue_progress(data)["priced"], 2)
        self.assertEqual(catalogue_progress(data)["unpriced"], 1)
        self.assertEqual(catalogue_progress(data)["remaining"], 2)

    def test_batch_eventually_covers_entire_catalogue_and_never_restarts(self):
        prices = {f"A{i:09d}": 10.0 + i for i in range(105)}
        data = make_section_catalogue(crawl(prices), None, section_id="all", section_name="All")
        seen = []
        for batch_no in range(3):
            batch = select_check_batch(data, limit=40)
            seen.extend(row["asin"] for row in batch)
            done = {
                row["asin"]: {"status": "no_match", "checked_at": f"2026-11-02T{batch_no:02d}:00:00+00:00"}
                for row in batch
            }
            apply_batch_results(data, batch, [], {}, [], cex_enabled=True,
                                quote_cache=done, checked_at=f"2026-11-02T{batch_no:02d}:00:00+00:00")
        self.assertEqual(len(set(seen)), 105)
        self.assertEqual(catalogue_progress(data)["remaining"], 0)
        self.assertEqual(catalogue_progress(data)["checked"], 105)

    def test_urgent_lane_does_not_starve_uncharted_items(self):
        data = make_section_catalogue(
            crawl({f"A{i:09d}": 50 + i for i in range(12)}),
            None, section_id="gaming", section_name="Gaming",
        )
        chosen = []
        for i in range(6):
            batch = select_check_batch(data, limit=3, priority_asins=["A000000000"], urgent_limit=1)
            chosen.extend(row["asin"] for row in batch)
            done = {
                row["asin"]: {"status": "no_match", "checked_at": f"2026-11-02T0{i}:00:00+00:00"}
                for row in batch
            }
            apply_batch_results(data, batch, [], {}, [], cex_enabled=True,
                                quote_cache=done, checked_at=f"2026-11-02T0{i}:00:00+00:00")
        self.assertEqual(catalogue_progress(data)["remaining"], 0)
        self.assertEqual(len(set(chosen)), 12)

    def test_matching_result_never_asserts_seller_evidence(self):
        section = make_section_catalogue(
            crawl({"A000000001": 40}), None, section_id="gaming", section_name="Gaming"
        )
        selected = select_check_batch(section, limit=10)
        verified = [{
            **selected[0], "seller_name": "Amazon", "seller_type": "amazon_retail",
            "fulfilment_status": "amazon",
        }]
        lookups = [{
            "asin": "A000000001", "cex_title": "Sony Headphones", "cex_url": "https://ie.webuy.com/product-detail?id=123",
            "cex_cash_eur": 65, "cex_cash_profit_eur": 17, "cex_max_buy_eur": 52,
            "match_confidence": "model_verified",
        }]
        apply_batch_results(section, selected, verified, {"A000000001": "verified"},
                            lookups, cex_enabled=True)
        row = section["items"][0]
        self.assertEqual(row["cex_status"], "matched_unverified")
        self.assertEqual(row["verification_status"], "disabled")
        self.assertNotIn("seller_name", row)
        self.assertEqual(row["cex_net_profit_eur"], 17)
        self.assertEqual(row["match_confidence"], "model_verified")
        self.assertEqual(catalogue_progress(section)["cex_attempted"], 1)
        self.assertEqual(catalogue_progress(section)["cex_matched"], 1)

    def test_unverified_amazon_listing_can_have_provisional_cex_research(self):
        section = make_section_catalogue(
            crawl({"A000000001": 60}), None, section_id="gaming", section_name="Gaming"
        )
        batch = select_check_batch(section, limit=20)
        speculative = [{
            "asin": "A000000001", "cex_title": "Sony Headphones",
            "cex_url": "https://ie.webuy.com/product-detail?id=123",
            "cex_cash_eur": 85, "cex_cash_profit_eur": 18,
            "cex_max_buy_eur": 70, "match_confidence": "title_only",
        }]
        apply_batch_results(
            section, batch, [], {"A000000001": "seller_not_detected"},
            speculative, cex_enabled=True,
        )
        item = section["items"][0]
        self.assertEqual(item["verification_status"], "disabled")
        self.assertEqual(item["cex_status"], "matched_unverified")
        self.assertEqual(catalogue_progress(section)["cex_attempted"], 1)
        self.assertEqual(catalogue_progress(section)["cex_matched"], 1)
        self.assertEqual(catalogue_progress(section)["cex_eligible_matched"], 0)

    def test_price_change_invalidates_prior_seller_and_cex_verification(self):
        first = make_section_catalogue(
            crawl({"A000000001": 40}), None, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T10:00:00+00:00",
        )
        first["items"][0].update(
            verification_status="verified", last_checked_at="2026-11-02T10:01:00+00:00",
            seller_name="Amazon", cex_status="matched", cex_cash_eur=70
        )
        still = make_section_catalogue(
            crawl({"A000000001": 40}), first, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T11:00:00+00:00",
        )
        self.assertEqual(still["items"][0]["cex_cash_eur"], 70)
        new = make_section_catalogue(
            crawl({"A000000001": 49}), still, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T12:00:00+00:00",
        )
        self.assertEqual(new["items"][0]["first_seen_at"], first["items"][0]["first_seen_at"])
        self.assertIsNone(new["items"][0]["last_checked_at"])
        self.assertNotIn("cex_cash_eur", new["items"][0])
        self.assertNotIn("seller_name", new["items"][0])
        self.assertEqual(new["items"][0]["verification_status"], "disabled")

    def test_merge_preserves_other_sections_and_csv_formula_safety(self):
        a = make_section_catalogue(
            crawl({"A000000001": 10}), None, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T10:00:00+00:00",
        )
        b = make_section_catalogue(
            crawl({"A000000002": 25}), None, section_id="home", section_name="Home",
            observed_at="2026-11-02T11:00:00+00:00",
        )
        b["items"][0]["title"] = "=HYPERLINK(\"https://bad.invalid\")"
        merged = merge_catalogue(merge_catalogue({}, a), b)
        self.assertEqual(set(merged["sections"]), {"gaming", "home"})
        self.assertEqual(merged["last_section_id"], "home")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "all.csv"
            write_catalogue_csv(merged, path)
            with path.open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 2)
            self.assertTrue(rows[1]["title"].startswith("'=HYPERLINK"))
            self.assertTrue(rows[0]["url"].startswith("https://www.amazon.ie/dp/"))

    def test_old_listings_are_archived_not_lost_or_rechecked(self):
        old = make_section_catalogue(
            crawl({"A000000001": 10, "A000000002": 12}), None,
            section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T10:00:00+00:00",
        )
        updated = make_section_catalogue(
            crawl({"A000000002": 15}), old,
            section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T11:00:00+00:00",
        )
        self.assertEqual(len(updated["items"]), 2)
        self.assertEqual(catalogue_progress(updated)["active"], 1)
        self.assertEqual(catalogue_progress(updated)["archived"], 1)
        self.assertEqual([r["asin"] for r in select_check_batch(updated, limit=20)], ["A000000002"])

    def test_rejects_stale_publish_from_previous_run(self):
        fresh = make_section_catalogue(
            crawl({"A000000001": 10}), None, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T15:00:00+00:00",
        )
        older = make_section_catalogue(
            crawl({"A000000001": 30}), None, section_id="gaming", section_name="Gaming",
            observed_at="2026-11-02T12:00:00+00:00",
        )
        snapshot = merge_catalogue({}, fresh)
        result = merge_catalogue(snapshot, older)
        self.assertEqual(result["sections"]["gaming"]["items"][0]["price_eur"], 10)


if __name__ == "__main__":
    unittest.main()
