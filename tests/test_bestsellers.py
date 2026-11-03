import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from app import database as db
from app.config import settings
from app.scraper.amazon_ie import _parse_price
from app.scraper.bestsellers import normalize_category_path, parse_bestseller_page
from app.services import bestsellers as svc
from app.services.bestsellers import CategoryRoot, CrawlConfig, crawl, process_crawl, send_alerts


def item_html(asin: str, rank: int, title: str, *prices: str) -> str:
    price_spans = "".join(f'<span class="_cDEzb_p13n-sc-price_3mJ9Z">{p}</span>' for p in prices)
    return f"""
    <div data-asin="{asin}">
      <span class="zg-bdg-text">#{rank}</span>
      <a href="/x/dp/{asin}/ref=zg_bs_g_home"><div class="_cDEzb_p13n-sc-css-line-clamp-3_g3dy1">{title}</div></a>
      {price_spans}
    </div>"""


def page_html(category: str, items: list[str], children: list[str] = (), parents: list[str] = ()) -> str:
    links = "".join(
        f'<div role="treeitem"><a href="{p}/ref=zg_bs_unv_home_1">up</a></div>' for p in parents
    ) + "".join(f'<div role="treeitem"><a href="{c}/ref=zg_bs_nav_home_2">down</a></div>' for c in children)
    return f"""<html><head><title>Amazon.ie Best Sellers: The most popular items in {category}</title></head>
    <body><div role="group">{links}</div>{''.join(items)}</body></html>"""


class TestParsePrice(unittest.TestCase):
    def test_amazon_ie_formats(self):
        self.assertEqual(_parse_price("€27.99"), 27.99)
        self.assertEqual(_parse_price("€1,299.99"), 1299.99)
        self.assertEqual(_parse_price("1.299,99 €"), 1299.99)
        self.assertEqual(_parse_price("€12,50"), 12.5)
        self.assertEqual(_parse_price("€1,299"), 1299.0)
        self.assertIsNone(_parse_price("Currently unavailable"))


class TestParsePage(unittest.TestCase):
    def test_items_children_and_ranges(self):
        html = page_html(
            "Coffee Machines",
            [
                item_html("B000000001", 1, "Espresso maker", "€1,099.00"),
                item_html("B000000002", 2, "Capsule machine", "€49.45"),
                item_html("B000000003", 3, "Variant mug", "€9.99", "€24.99"),
            ],
            children=["/gp/bestsellers/home/111"],
            parents=["/gp/bestsellers/home"],
        )
        page = parse_bestseller_page(html)
        self.assertFalse(page.blocked)
        self.assertEqual(page.category, "Coffee Machines")
        self.assertEqual([i.asin for i in page.items], ["B000000001", "B000000002", "B000000003"])
        self.assertEqual(page.items[0].price_eur, 1099.0)
        self.assertEqual(page.items[0].rank, 1)
        self.assertEqual(page.items[0].title, "Espresso maker")
        self.assertIsNone(page.items[2].price_eur)
        self.assertEqual(page.child_paths, ["/gp/bestsellers/home/111"])

    def test_review_count_from_card(self):
        html = page_html(
            "Coffee Machines",
            [
                item_html("B000000001", 1, "Espresso maker", "€1,099.00").replace(
                    "</div>",
                    '<a aria-label="4.6 out of 5 stars, 1,284 ratings"></a></div>',
                    1,
                ),
            ],
        )
        page = parse_bestseller_page(html)
        self.assertEqual(page.items[0].review_count, 1284)

    def test_captcha_is_blocked(self):
        page = parse_bestseller_page('<form action="/errors/validateCaptcha"></form>')
        self.assertTrue(page.blocked)

    def test_normalize_category_path(self):
        self.assertEqual(normalize_category_path("home"), "/gp/bestsellers/home")
        self.assertEqual(
            normalize_category_path("https://www.amazon.ie/gp/bestsellers/home/94816616031/ref=zg?x=1"),
            "/gp/bestsellers/home/94816616031",
        )


class FakeAmazon:
    def __init__(self, pages: dict[str, str]):
        self.pages = pages
        self.requested: list[str] = []

    async def __call__(self, url: str) -> tuple[int, str]:
        path = url.replace("https://www.amazon.ie", "")
        self.requested.append(path)
        if path in self.pages:
            return 200, self.pages[path]
        return 404, ""


def tree(prices: dict[str, str] | None = None) -> dict[str, str]:
    prices = prices or {}

    def p(asin, default):
        return prices.get(asin, default)

    return {
        "/gp/bestsellers/root": page_html(
            "Root",
            [item_html("B0ROOT0001", 1, "Root item", p("B0ROOT0001", "€100.00"))],
            children=["/gp/bestsellers/root/1", "/gp/bestsellers/root/2"],
        ),
        "/gp/bestsellers/root?pg=2": page_html(
            "Root", [item_html("B0ROOT0002", 31, "Root item 2", p("B0ROOT0002", "€80.00"))]
        ),
        "/gp/bestsellers/root/1": page_html(
            "Child one",
            [item_html("B0CHILD001", 1, "Child item", p("B0CHILD001", "€60.00"))],
            children=["/gp/bestsellers/root/1/deep"],
            parents=["/gp/bestsellers/root"],
        ),
        "/gp/bestsellers/root/2": page_html(
            "Child two", [item_html("B0CHILD002", 1, "Other child", p("B0CHILD002", "€40.00"))]
        ),
    }


class TestCrawl(unittest.TestCase):
    def config(self, **kw) -> CrawlConfig:
        base = dict(roots=[CategoryRoot("/gp/bestsellers/root", depth=1)], request_delay_seconds=0)
        base.update(kw)
        return CrawlConfig(**base)

    def test_depth_limits_and_parents_not_revisited(self):
        fake = FakeAmazon(tree())
        result = asyncio.run(crawl(fake, self.config()))
        self.assertEqual(set(result.items), {"B0ROOT0001", "B0ROOT0002", "B0CHILD001", "B0CHILD002"})
        self.assertNotIn("/gp/bestsellers/root/1/deep", fake.requested)
        self.assertEqual(fake.requested.count("/gp/bestsellers/root"), 1)
        self.assertFalse(result.blocked)

    def test_max_items_stops_crawl(self):
        fake = FakeAmazon(tree())
        result = asyncio.run(crawl(fake, self.config(max_items=2)))
        self.assertEqual(len(result.items), 2)
        self.assertNotIn("/gp/bestsellers/root/1", fake.requested)

    def test_exclude(self):
        fake = FakeAmazon(tree())
        result = asyncio.run(crawl(fake, self.config(exclude={"/gp/bestsellers/root/2"})))
        self.assertNotIn("B0CHILD002", result.items)

    def test_captcha_stops_crawl(self):
        pages = tree()
        pages["/gp/bestsellers/root/1"] = '<form action="/errors/validateCaptcha"></form>'
        result = asyncio.run(crawl(FakeAmazon(pages), self.config()))
        self.assertTrue(result.blocked)
        self.assertNotIn("B0CHILD002", result.items)


class TestDetection(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_db = settings.database_path
        settings.database_path = Path(self._tmp.name) / "bestsellers.db"
        db.init_db()
        self.config = CrawlConfig(roots=[CategoryRoot("/gp/bestsellers/root", depth=1)], request_delay_seconds=0)

    def tearDown(self):
        settings.database_path = self._old_db
        self._tmp.cleanup()

    def run_crawl(self, prices=None):
        result = asyncio.run(crawl(FakeAmazon(tree(prices)), self.config))
        return process_crawl(result, self.config)

    def test_flags_drop_only_after_history_and_only_once(self):
        first = self.run_crawl()
        self.assertEqual(first.alerts, [])
        self.assertEqual(first.with_history, 0)
        self.run_crawl()
        self.run_crawl()

        crash = self.run_crawl({"B0ROOT0001": "€9.99"})
        self.assertEqual(crash.alerts, [], "Bestseller-only prices must never alert without seller verification")
        self.assertEqual(crash.drops[0]["asin"], "B0ROOT0001")
        self.assertGreaterEqual(crash.drops[0]["drop_percent"], 75)

        again = self.run_crawl({"B0ROOT0001": "€9.99"})
        self.assertEqual(again.alerts, [], "same price within the cooldown must not re-alert")

    def test_price_crossing_1000_is_not_an_error(self):
        self.run_crawl({"B0ROOT0001": "€999.00"})
        self.run_crawl({"B0ROOT0001": "€999.00"})
        processed = self.run_crawl({"B0ROOT0001": "€1,049.00"})
        self.assertEqual(processed.alerts, [])

    def test_history_is_pruned(self):
        self.config.history_per_product = 2
        for _ in range(4):
            self.run_crawl()
        pid = next(p["id"] for p in db.list_products() if p["asin"] == "B0ROOT0001")
        self.assertEqual(len(db.price_history(pid, limit=50)), 2)

    def test_send_alerts_caps_messages(self):
        alerts = [svc.DetectedAlert(i, f"B00000000{i}", 80 + i, f"msg {i}") for i in range(5)]
        self.config.max_alerts_per_run = 2
        with mock.patch.object(svc, "notify_alert", new=mock.AsyncMock()) as notify, mock.patch.object(
            svc, "send_notification", new=mock.AsyncMock(return_value=(True, False))
        ) as summary:
            sent = asyncio.run(send_alerts(alerts, self.config, details_url="https://example/DEALS.md"))
        self.assertEqual(sent, 2)
        self.assertEqual([c.args[0].asin for c in notify.call_args_list], ["B000000004", "B000000003"])
        self.assertIn("3 more", summary.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
