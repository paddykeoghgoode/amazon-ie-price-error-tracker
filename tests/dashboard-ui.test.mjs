import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";
import { test } from "node:test";

const js = fs.readFileSync("docs/app.js", "utf8");
const html = fs.readFileSync("docs/index.html", "utf8");
const start = js.indexOf("function extractGeneralDrops(deals) {");
const end = js.indexOf("\nfunction renderGeneralDropsPage()", start);
assert.ok(start >= 0 && end > start, "General Amazon drop normalizer must exist");
const normalize = vm.runInNewContext(js.slice(start, end) + "\nextractGeneralDrops;");

test("five accessible tabs and all Amazon drop controls exist", () => {
  const tabs = ["overview", "amazon-drops", "cex", "listings", "watchlist"];
  for (const id of tabs) {
    assert.ok(html.includes('id="tab-' + id + '"'));
    assert.ok(html.includes('id="panel-' + id + '"'));
    assert.ok(html.includes('aria-controls="panel-' + id + '"'));
    assert.ok(html.includes('aria-labelledby="tab-' + id + '"'));
  }
  for (const id of ["drop-min", "drop-section", "drop-search", "drop-sort",
                    "general-drops-table", "general-drops-pagination"]) {
    assert.ok(html.includes('id="' + id + '"'));
  }
});

test("percent and euro saving are computed from historical reference", () => {
  const data = normalize({ generated_at: "2026-11-02T13:00:00Z",
    sections: { gaming: {
      section_name: "Gaming & tech", generated_at: "2026-11-02T13:00:00Z",
      general_drops: [
        { asin: "A1", title: "Headphones", price_eur: 68, reference_eur: 100,
          found_at: "2026-11-02T12:00:00Z" },
        { asin: "A2", title: "Bad price", price_eur: 100, reference_eur: 80 },
      ],
    }}
  });
  assert.equal(data.rows.length, 1);
  assert.equal(data.rows[0].savings_eur, 32);
  assert.equal(data.rows[0].drop_percent, 32);
  assert.equal(data.rows[0].section_name, "Gaming & tech");
  assert.equal(data.rows[0].found_at, "2026-11-02T12:00:00Z");
  assert.equal(data.coverage.updated, 1);
});

test("legacy sections remain visible until their next scan publishes", () => {
  const data = normalize({ generated_at: "2026-11-02T14:00:00Z",
    sections: {
      old: { generated_at: "2026-11-02T10:00:00Z",
             drops: [{ asin: "B1", price_eur: 50, median_eur: 100 }] },
      modern: { generated_at: "2026-11-02T14:00:00Z",
                general_drops: [{ asin: "B2", price_eur: 85, reference_eur: 100 }] },
    }
  });
  assert.equal(data.rows.length, 2);
  assert.equal(data.coverage.updated, 1);
  assert.equal(data.coverage.total, 2);
});

test("cross-section duplicate uses the freshest observation", () => {
  const data = normalize({ generated_at: "2026-11-02T13:00:00Z",
    sections: {
      newest: { generated_at: "2026-11-02T13:00:00Z",
                general_drops: [{ asin: "B1", price_eur: 80, reference_eur: 100 }] },
      older: { generated_at: "2026-11-02T10:00:00Z",
               general_drops: [{ asin: "B1", price_eur: 30, reference_eur: 100 }] },
    }
  });
  assert.equal(data.rows.length, 1);
  assert.equal(data.rows[0].price_eur, 80);
  assert.equal(data.rows[0].drop_percent, 20);
});
