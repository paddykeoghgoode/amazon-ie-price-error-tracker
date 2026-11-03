"""Completion-triggered broad scans are serial, with bounded recovery."""
from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml

from scripts.recover_bestseller_scans import WATCHDOG_DELAY, decide_recovery


NOW = datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)


def broad(
    id: int,
    *,
    minutes_ago: int = 30,
    status: str = "completed",
    conclusion: str = "success",
    event: str = "workflow_dispatch",
    branch: str = "main",
) -> dict:
    stamp = (NOW - timedelta(minutes=minutes_ago)).isoformat()
    return {
        "id": id, "created_at": stamp, "updated_at": stamp,
        "status": status, "conclusion": conclusion, "event": event, "head_branch": branch
    }


class TestRecoveryDecisions(unittest.TestCase):
    def test_empty_repo_bootstraps_broad_discovery(self):
        self.assertTrue(decide_recovery([], NOW)[0])

    def test_watchlist_schedule_runs_do_not_prevent_broad_recovery(self):
        self.assertTrue(decide_recovery([broad(1, event="schedule", status="in_progress")], NOW)[0])

    def test_running_queued_pending_broad_runs_prevent_new_ones(self):
        for status in ("in_progress", "queued", "requested", "pending", "waiting"):
            with self.subTest(status=status):
                self.assertFalse(decide_recovery([broad(1, status=status)], NOW)[0])

    def test_recent_completed_run_does_not_launch_extra_scan(self):
        self.assertFalse(decide_recovery([broad(1, minutes_ago=5)], NOW)[0])
        self.assertFalse(decide_recovery([broad(1, minutes_ago=24)], NOW)[0])
        self.assertTrue(decide_recovery([broad(1, minutes_ago=26)], NOW)[0])
        self.assertEqual(WATCHDOG_DELAY, timedelta(minutes=25))

    def test_failed_recent_run_waits_before_recovery(self):
        self.assertFalse(decide_recovery([broad(1, minutes_ago=5, conclusion="failure")], NOW)[0])
        self.assertTrue(decide_recovery([broad(1, minutes_ago=30, conclusion="failure")], NOW)[0])

    def test_three_consecutive_failures_stop_automatic_dispatch(self):
        runs = [
            broad(3, minutes_ago=30, conclusion="failure"),
            broad(2, minutes_ago=70, conclusion="timed_out"),
            broad(1, minutes_ago=110, conclusion="cancelled"),
        ]
        allowed, reason = decide_recovery(runs, NOW)
        self.assertFalse(allowed)
        self.assertIn("three", reason.replace("3", "three"))
        self.assertTrue(decide_recovery(runs, NOW, force=True)[0])

    def test_success_resets_circuit_breaker(self):
        runs = [
            broad(4, minutes_ago=50, conclusion="failure"),
            broad(3, minutes_ago=60, conclusion="failure"),
            broad(2, minutes_ago=75, conclusion="success"),
            broad(1, minutes_ago=100, conclusion="failure"),
        ]
        self.assertTrue(decide_recovery(runs, NOW)[0])

    def test_force_can_override_delay_but_not_active_scan(self):
        self.assertTrue(decide_recovery([broad(2, minutes_ago=2)], NOW, force=True)[0])
        self.assertFalse(decide_recovery([broad(2, minutes_ago=2, status="in_progress")],
                                         NOW, force=True)[0])

    def test_other_branch_or_invalid_timestamps_dont_force_overlap(self):
        self.assertTrue(decide_recovery([broad(1, branch="feature", status="in_progress")], NOW)[0])
        row = broad(2)
        row["updated_at"] = "not a time"
        row["created_at"] = "not a time"
        allowed, reason = decide_recovery([row], NOW)
        self.assertFalse(allowed)
        self.assertIn("completion time", reason)


class TestRecoveryWorkflow(unittest.TestCase):
    def test_recovery_is_a_lightweight_watchdog_with_safe_gh_dispatch(self):
        yml = yaml.safe_load(Path(".github/workflows/bestseller-recovery.yml").read_text())
        on = yml.get("on", yml.get(True))
        self.assertEqual(on["schedule"][0]["cron"], "13,43 * * * *")
        self.assertIn("workflow_dispatch", on)
        self.assertEqual(yml["permissions"]["actions"], "write")
        self.assertEqual(yml["concurrency"]["queue"], "max")
        job = yml["jobs"]["recover"]
        self.assertIn("PAUSE_AUTOMATIC_SCAN", job["if"])
        assert job["steps"][0]["uses"].startswith("actions/checkout@")
        self.assertIn("scripts.recover_bestseller_scans", job["steps"][1]["run"])
        self.assertIn("steps.check.outputs.dispatch == 'true'", job["steps"][2]["if"])
        self.assertIn("auto_continue=true", job["steps"][2]["run"])
        self.assertIn("crawl_section=rotate", job["steps"][2]["run"])


if __name__ == "__main__":
    unittest.main()
