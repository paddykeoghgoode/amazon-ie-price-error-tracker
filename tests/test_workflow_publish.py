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

    def test_fast_checks_exist_and_preserve_normal_outputs(self):
        hotlist = Path(".github/workflows/hotlist.yml").read_text(encoding="utf-8")
        self.assertIn("scripts.scan_hotlist", hotlist)
        self.assertIn("docs/hotlist.json", hotlist)
        self.assertIn("docs/deals.json", hotlist)
        self.assertIn("docs/data.json", hotlist)


if __name__ == "__main__":
    unittest.main()
