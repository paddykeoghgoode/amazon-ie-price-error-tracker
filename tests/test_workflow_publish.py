"""Prevent regressions that block publishing otherwise successful scan artifacts."""
from pathlib import Path
import unittest


class TestWorkflowPublishing(unittest.TestCase):
    def test_publish_bestsellers_installs_dependencies(self):
        text = Path(".github/workflows/pages.yml").read_text(encoding="utf-8")
        start = text.index("  publish-bestsellers:\n")
        end = text.index("\n  bestsellers-all:", start)
        section = text[start:end]
        self.assertIn("actions/setup-python@v7", section)
        self.assertIn("pip install -r requirements.txt", section)
        self.assertIn("python -m scripts.merge_deals", section)
        self.assertIn("python -m scripts.scan_summary", section)

    def test_manual_full_scan_publishes_sections_individually(self):
        import yaml

        workflow = yaml.safe_load(Path(".github/workflows/pages.yml").read_text(encoding="utf-8"))
        triggers = workflow.get("on", workflow.get(True, {}))
        inputs = triggers["workflow_dispatch"]["inputs"]
        self.assertEqual(inputs["crawl_section"]["default"], "all")
        jobs = workflow["jobs"]
        full = jobs["bestsellers-all"]
        self.assertEqual(full["strategy"]["max-parallel"], 1)
        self.assertEqual(full["strategy"]["matrix"]["section"], ["gaming", "home", "lifestyle", "general"])
        self.assertIn("matrix.section", full["name"])
        steps = full["steps"]
        crawl = next(step["run"] for step in steps if step.get("name") == "Crawl section and notify")
        self.assertIn("--publish-fragment", crawl)
        self.assertIn("--section", crawl)
        self.assertIn("matrix.section", crawl)
        self.assertIn("site/previous-deals.json", crawl)

        # Each matrix job must restore and then save its own DB checkpoint.
        cache = next(step for step in steps if "actions/cache@" in step.get("uses", ""))
        self.assertIn("matrix.section", cache["with"]["key"])
        self.assertIn("bestsellers-db-v1-", cache["with"]["restore-keys"])

        publish = next(step["run"] for step in steps if step.get("name") == "Assemble and merge section immediately before publish")
        self.assertIn("python -m scripts.merge_deals", publish)
        self.assertIn("docs/deals.previous.json", publish)
        self.assertTrue(any("deploy-tracker-site" in step.get("uses", "") for step in steps))
        self.assertTrue(any("scan_summary" in step.get("run", "") for step in steps))

    def test_fast_checks_exist_and_preserve_normal_outputs(self):
        hotlist = Path(".github/workflows/hotlist.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.scan_hotlist", hotlist)
        self.assertIn("docs/hotlist.json", hotlist)
        self.assertIn("docs/deals.json", hotlist)
        self.assertIn("docs/data.json", hotlist)


if __name__ == "__main__":
    unittest.main()
