import assert from "node:assert/strict";
import { createRequire } from "node:module";
import { test } from "node:test";
import fs from "node:fs";

const require = createRequire(import.meta.url);
const {
  trackerWorkflowRun, trackerWorkflowName, trackerStatus,
  trackerStepProgress, selectedActivityRuns, trackerSafeRunUrl,
} = require("../docs/scan-activity.js");

test("only tracker and fast priority runs are shown, not test suite or arbitrary workflows", () => {
  assert.equal(trackerWorkflowRun({ name: "Tests", path: ".github/workflows/test.yml" }), false);
  assert.equal(trackerWorkflowRun({ name: "Scan and publish tracker",
    path: ".github/workflows/pages.yml" }), true);
  assert.equal(trackerWorkflowRun({ name: "Fast checks for price-drop leads",
    path: ".github/workflows/hotlist.yml" }), true);
  assert.equal(trackerWorkflowName({ path: ".github/workflows/hotlist.yml", event: "schedule" }),
    "Priority price-drop checks");
  assert.equal(trackerWorkflowName({ path: ".github/workflows/pages.yml", event: "issues" }),
    "Watchlist change");
});

test("run selection prefers active and the last finished of each scan lane", () => {
  const runs = [
    { id: 1, name: "Tests", path: ".github/workflows/test.yml", status: "completed",
      created_at: "2026-11-03T12:06:00Z" },
    { id: 2, name: "Scan and publish tracker", path: ".github/workflows/pages.yml",
      status: "completed", conclusion: "success", created_at: "2026-11-03T12:05:00Z" },
    { id: 3, name: "Fast checks for price-drop leads", path: ".github/workflows/hotlist.yml",
      status: "in_progress", created_at: "2026-11-03T12:10:00Z" },
    { id: 4, name: "Scan and publish tracker", path: ".github/workflows/pages.yml",
      status: "queued", created_at: "2026-11-03T12:11:00Z" },
    { id: 5, name: "Fast checks for price-drop leads", path: ".github/workflows/hotlist.yml",
      status: "completed", conclusion: "success", created_at: "2026-11-03T12:00:00Z" },
  ];
  assert.deepEqual(selectedActivityRuns(runs).map(x => x.id), [4, 3, 2, 5]);
});

test("step percentage is based only on reported Actions steps, never product counts", () => {
  const jobs = [
    { name: "watchlist", status: "completed", conclusion: "success",
      steps: [{ name: "Install", status: "completed" },
              { name: "Scan watchlist", status: "completed" }] },
    { name: "bestsellers", status: "in_progress",
      steps: [{ name: "Restore price history", status: "completed" },
              { name: "Crawl bestseller section", status: "in_progress" },
              { name: "Upload result", status: "queued" }] },
    { name: "bestsellers-all", status: "completed", conclusion: "skipped" },
  ];
  const p = trackerStepProgress(jobs, "in_progress");
  assert.equal(p.percentage, 60);
  assert.equal(p.completedSteps, 3);
  assert.equal(p.totalSteps, 5);
  assert.equal(p.completedJobs, 1);
  assert.equal(p.knownJobs, 2);
  assert.equal(p.currentStage, "bestsellers: Crawl bestseller section");
});

test("unknown and queued jobs do not become fictitious 100 percent completions", () => {
  assert.equal(trackerStepProgress([], "in_progress"), null);
  const p = trackerStepProgress([{ name: "unknown", status: "queued" }], "queued");
  assert.equal(p.percentage, 0);
  assert.equal(p.totalSteps, 1);
  const stillActive = trackerStepProgress([{ status: "in_progress",
    steps: [{ name: "Step", status: "completed" }] }], "in_progress");
  assert.equal(stillActive.percentage, 99);
});

test("completed and failed statuses remain distinct", () => {
  assert.deepEqual(trackerStatus({ status: "completed", conclusion: "success" }),
    { text: "Completed", css: "ok" });
  assert.deepEqual(trackerStatus({ status: "completed", conclusion: "failure" }),
    { text: "Failed", css: "failed" });
  assert.deepEqual(trackerStatus({ status: "in_progress" }),
    { text: "Running", css: "running" });
  assert.deepEqual(trackerStatus({ status: "queued" }),
    { text: "Queued", css: "queued" });
});

test("run links only accept numeric ids, never HTML API URLs", () => {
  assert.equal(trackerSafeRunUrl({ id: 123 }),
    "https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions/runs/123");
  assert.equal(trackerSafeRunUrl({ id: "javascript:alert(1)" }),
    "https://github.com/paddykeoghgoode/amazon-ie-price-error-tracker/actions");
});

test("overview contains the activity panel, disclaimer and JS loader", () => {
  const html = fs.readFileSync("docs/index.html", "utf8");
  assert.match(html, /id="scan-activity-runs"/);
  assert.match(html, /id="scan-activity-summary"/);
  assert.match(html, /percentage of Amazon products scanned/);
  assert.match(html, /<script src="scan-activity.js"><\/script>/);
});
