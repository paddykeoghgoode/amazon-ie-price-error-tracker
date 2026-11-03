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

test("top deals are substantial, recent and never cheap games/title-only CeX", () => {
  const start = js.indexOf("function selectRadarLeads(deals, now = Date.now()) {");
  const end = js.indexOf("\nfunction renderOpportunityRadar(", start);
  assert.ok(start >= 0 && end > start, "Top-deals selector must exist");
  const select = vm.runInNewContext(js.slice(start, end) + "\nselectRadarLeads;");
  const now = Date.parse("2026-11-03T12:00:00Z");
  const data = {
    generated_at: "2026-11-03T11:50:00Z",
    general_drops: [
      {asin:"B000000001",title:"Expensive camera",price_eur:200,
       reference_eur:600,last_scanned_at:"2026-11-03T11:50:00Z"},
      {asin:"B000000002",title:"Cheap game",price_eur:4,
       reference_eur:90,last_scanned_at:"2026-11-03T11:50:00Z"},
      {asin:"B000000003",title:"Old deal",price_eur:200,
       reference_eur:700,last_scanned_at:"2026-10-30T11:50:00Z"},
    ],
    cex_flips: [
      {asin:"B000000004",title:"Matched laptop",price_eur:140,
       cex_cash_profit_eur:80,cex_net_roi_percent:50,
       match_confidence:"model_verified",cex_quote_fresh_for_alert:true,
       observed_at:"2026-11-03T11:40:00Z"},
      {asin:"B000000005",title:"Title-only game",price_eur:140,
       cex_cash_profit_eur:100,cex_net_roi_percent:60,
       match_confidence:"title_only",cex_quote_fresh_for_alert:true,
       observed_at:"2026-11-03T11:40:00Z"},
    ],
  };
  const result = select(data, now);
  assert.equal(result.drops.length, 1);
  assert.equal(result.cex.length, 1);
  assert.equal(result.items.length, 2);
  assert.equal(result.items[0].asin, "B000000004");
  assert.equal(result.items[1].asin, "B000000001");
});

test("resale-first UI includes shortlist metrics and avoids a redundant seller column", () => {
  for (const id of ["radar-title","radar-drop-count","radar-cex-count","radar-last-published","radar-cards"]) {
    assert.ok(html.includes('id="' + id + '"'));
  }
  assert.ok(html.includes('data-tab="overview">Top Deals</button>'));
  assert.ok(!html.includes("<th>Seller checks</th>"));
});
