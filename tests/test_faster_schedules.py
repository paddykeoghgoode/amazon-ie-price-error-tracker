"""A faster schedule must not starve sections or roll back published watchlists."""
from __future__ import annotations

import argparse
import json
import tempfile
import unittest
from pathlib import Path

import yaml

from app.bestseller_sections import load_sections, rotate_section_id
from scripts.crawl_bestsellers import resolve_section_id
from scripts.publish_watchlist import latest_watchlist


class TestFasterSchedules(unittest.TestCase):
    def setUp(self):
        self.pages = yaml.safe_load(Path(".github/workflows/pages.yml").read_text(encoding="utf-8"))
        self.hotlist = yaml.safe_load(Path(".github/workflows/hotlist.yml").read_text(encoding="utf-8"))

    def test_watchlist_runs_10m_and_broad_discovery_chains_only_after_publish(self):
        triggers = self.pages.get("on", self.pages.get(True))
        crons = [x["cron"] for x in triggers["schedule"]]
        self.assertEqual(crons, ["5,15,25,35,45,55 * * * *"])
        minutes = sorted(int(i) for c in crons for i in c.split()[0].split(","))
        self.assertEqual(minutes, [5, 15, 25, 35, 45, 55])
        self.assertIn("auto_continue", triggers["workflow_dispatch"]["inputs"])
        self.assertFalse(triggers["workflow_dispatch"]["inputs"]["auto_continue"]["default"])
        jobs = self.pages["jobs"]
        self.assertNotIn("schedule", jobs["bestsellers"]["if"])
        self.assertIn("github.event_name == 'push'", jobs["bestsellers"]["if"])
        self.assertIn("inputs.auto_continue != true", jobs["watchlist"]["if"])
        followup = jobs["queue-next-bestseller"]
        self.assertIn("publish-bestsellers", followup["needs"])
        self.assertIn("publish-watchlist", followup["needs"])
        self.assertIn("always()", followup["if"])
        self.assertIn("needs.publish-bestsellers.result == 'success'", followup["if"])
        self.assertIn("inputs.auto_continue == true", followup["if"])
        self.assertIn("PAUSE_AUTOMATIC_SCAN", followup["if"])
        self.assertIn("auto_continue=true", followup["steps"][0]["run"])
        self.assertIn("crawl_section=rotate", followup["steps"][0]["run"])
        self.assertEqual(followup["permissions"]["actions"], "write")
        self.assertEqual(self.pages["concurrency"]["queue"], "max")
        self.assertEqual(jobs["watchlist"]["concurrency"]["queue"], "max")
        self.assertIn("tracker-watchlist-scan", jobs["watchlist"]["concurrency"]["group"])
        self.assertIn("tracker-", self.pages["concurrency"]["group"])

    def test_hotlist_runs_at_10m_and_cex_live_budget_stays_bounded(self):
        crons = [x["cron"] for x in self.hotlist.get("on", self.hotlist.get(True))["schedule"]]
        self.assertEqual(crons, ["2,12,22,32,42,52 * * * *"])
        config = json.loads(Path("bestsellers.json").read_text(encoding="utf-8"))
        self.assertEqual(config["cex_live_lookups_per_run"], 10)
        self.assertLessEqual(config["catalogue_time_budget_seconds"], 360)
        self.assertGreaterEqual(config["cex_quote_ttl_hours"], 24)

    def test_section_rotation_uses_last_published_result_not_run_number(self):
        sections = load_sections(Path("bestsellers.json"))
        self.assertEqual([s.id for s in sections], ["gaming", "home", "lifestyle", "general"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deals.json"
            for previous_id, expected in (
                ("gaming", "home"), ("home", "lifestyle"),
                ("lifestyle", "general"), ("general", "gaming")
            ):
                path.write_text(json.dumps({"last_section_id": previous_id}), encoding="utf-8")
                args = argparse.Namespace(section="rotate", merge_previous=path, run_number=99)
                self.assertEqual(resolve_section_id(args, sections), expected)

    def test_rotation_falls_back_to_latest_section_or_existing_run_index(self):
        sections = load_sections(Path("bestsellers.json"))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "deals.json"
            args = argparse.Namespace(section="rotate", merge_previous=path, run_number=11)
            self.assertEqual(resolve_section_id(args, sections), rotate_section_id(sections, 11))
            path.write_text(json.dumps({
                "sections": {
                    "home": {"generated_at": "2026-11-02T10:00:00+00:00"},
                    "lifestyle": {"generated_at": "2026-11-02T11:00:00+00:00"},
                }
            }), encoding="utf-8")
            self.assertEqual(resolve_section_id(args, sections), "general")

    def test_manual_all_sections_and_watchlist_issue_events_are_unchanged(self):
        jobs = self.pages["jobs"]
        matrix = jobs["bestsellers-all"]
        self.assertEqual(matrix["strategy"]["max-parallel"], 1)
        self.assertEqual(matrix["strategy"]["matrix"]["section"],
                         ["gaming", "home", "lifestyle", "general"])
        self.assertIn("github.event_name == 'issues'",
                      self.pages["concurrency"]["group"])
        self.assertEqual(self.pages["permissions"]["issues"], "write")
        publish = "\n".join(x.get("run", "") for x in jobs["publish-watchlist"]["steps"])
        self.assertIn("python3 -m scripts.publish_watchlist", publish)
        self.assertIn("docs/data.previous.json", publish)


class TestWatchlistPublishFreshness(unittest.TestCase):
    def test_delay_cannot_roll_back_fresher_watchlist_price_or_issue_status(self):
        previous = {
            "generated_at": "2026-11-03T12:10:00Z",
            "products": [{"asin": "ABC1234567", "price_eur": 10}],
            "watch_submissions": [{"status": "tracking"}],
        }
        incoming = {
            "generated_at": "2026-11-03T12:00:00+00:00",
            "products": [{"asin": "ABC1234567", "price_eur": 15}],
            "watch_submissions": [{"status": "pending_scan"}],
        }
        self.assertIs(latest_watchlist(previous, incoming), previous)

    def test_new_watchlist_update_is_published_and_missing_previous_is_safe(self):
        old = {"generated_at": "2026-11-03T12:00:00+00:00"}
        new = {"generated_at": "2026-11-03T12:10:00+00:00", "products": []}
        self.assertIs(latest_watchlist(old, new), new)
        self.assertIs(latest_watchlist({}, new), new)
        self.assertIs(latest_watchlist(old, {}), old)


if __name__ == "__main__":
    unittest.main()
