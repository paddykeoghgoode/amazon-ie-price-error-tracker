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
                    "general-drops-table", "general-drops-pagination",
                    "drop-min-price", "drop-min-saving", "drop-brands-only", "radar-cards"]) {
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

test("research log shows actual found times and can include unrecognised brands", () => {
  const start = js.indexOf("function researchLogRows(");
  const end = js.indexOf("\nfunction renderDeals(", start);
  const select = vm.runInNewContext(js.slice(start, end) + "\nresearchLogRows;");
  const first = "2026-11-03T10:00:00Z";
  const data = { generated_at:"2026-11-03T12:00:00Z", drops: [
    {asin:"A",found_at:first,resale_eligible:true},
    {asin:"B",found_at:first,resale_eligible:false},
    {asin:"C"},
    {asin:"D",found_at:"invalid"},
  ]};
  const filtered = select(data);
  assert.equal(filtered.length,3);
  assert.equal(filtered[0].found_at, first);
  assert.equal(filtered[1].found_at,null);
  assert.equal(filtered[2].found_at,null);
  assert.equal(select(data,true).length,4);
  const table = html.slice(html.indexOf('id="deals-table"'), html.indexOf('id="deals-body"'));
  assert.ok(table.includes('data-sort-key="found_at"'));
  assert.ok(html.includes('id="research-include-unrecognised"'));
});

test("CeX target view reports funnel, timestamps and stale observations honestly", () => {
  const start = js.indexOf("function cexTargetView(data, now = Date.now()) {");
  const end = js.indexOf("\nfunction renderCexTargets(", start);
  const view = vm.runInNewContext(js.slice(start, end) + "\ncexTargetView;");
  const now = Date.parse("2026-11-03T20:00:00Z");
  const data = {funnel: {catalogue_skus: 250, saved_mappings: 30, alert_ready_mappings: 12, fresh_quotes: 25,
    amazon_attempted: 12, price_read: 10, identity_mismatch: 2, profitable: 1, stale_quotes: 5},
    products: [
      {asin:"new",scan_lane:"cex_target",status:"profitable",checked_at:"2026-11-03T19:55:00Z",found_at:"2026-11-03T19:55:00Z"},
      {asin:"old",scan_lane:"cex_target",status:"profitable",checked_at:"2026-11-02T19:55:00Z"},
      {asin:"drop",scan_lane:"drop",status:"price_read",checked_at:"2026-11-03T19:55:00Z"},
    ]};
  const result = view(data, now);
  assert.equal(result.rows.length, 2);
  assert.match(result.summary, /250 CeX catalogue SKUs/);
  assert.match(result.summary, /30 saved Amazon links/);
  assert.match(result.diagnostics, /12 pages,/);
  assert.equal(result.rows[0].result_label, "Meets cash margin");
  assert.equal(result.rows[1].result_label, "Awaiting a fresh check");
  assert.equal(result.rows[0].found_at, data.products[0].found_at);
  assert.ok(html.includes('id="cex-target-table"'));
});

test("a fast failed check removes an older CeX lead from the overview", () => {
  const start = js.indexOf("function mergeFastCex(deals, hotlist, now = Date.now()) {");
  const end = js.indexOf("\nfunction renderOpportunityRadar(", start);
  const merge = vm.runInNewContext(js.slice(start, end) + "\nmergeFastCex;");
  const now = Date.parse("2026-11-03T20:00:00Z");
  const deals = {generated_at:"2026-11-03T19:00:00Z",cex_arbitrage:[{asin:"A",price_eur:180}]};
  const failed = {products:[{asin:"A",scan_lane:"cex_target",status:"identity_mismatch",checked_at:"2026-11-03T19:55:00Z"}]};
  assert.equal(merge(deals, failed, now).cex_arbitrage.length, 0);
  const good = {products:[{...failed.products[0],status:"profitable",price_eur:150,
    cex_quote_fresh_for_alert:true,cex_quote_checked_at:"2026-11-03T18:00:00Z"}]};
  const fresh = merge(deals, good, now).cex_arbitrage;
  assert.equal(fresh.length, 1);
  assert.equal(fresh[0].price_eur, 150);
  assert.equal(fresh[0].observed_at, "2026-11-03T19:55:00Z");
  good.products[0].cex_quote_checked_at="2026-11-02T18:00:00Z";
  assert.equal(merge(deals, good, now).cex_arbitrage.length, 0);
});

test("expensive branded items remain visible when errors make them cheap, independently of CeX", () => {
  const start = js.indexOf("function selectRadarLeads(deals, now = Date.now()) {");
  const end = js.indexOf("\nfunction mergeFastCex(", start);
  const select = vm.runInNewContext(js.slice(start, end) + "\nselectRadarLeads;");
  const now=Date.parse("2026-11-03T20:00:00Z");
  const good={asin:"SAGE",title:"Sage espresso machine",price_eur:85,reference_eur:800,
    last_scanned_at:"2026-11-03T19:55:00Z",resale_eligible:true};
  const data={general_drops:[good,{...good,asin:"UNKNOWN",resale_eligible:false},
    {...good,asin:"LIST",drop_kind:"list_price"},{...good,asin:"CHEAP",reference_eur:90}],
    cex_arbitrage:[{asin:"CEX",price_eur:15,cex_cash_profit_eur:52,cex_net_roi_percent:346,
      match_confidence:"model_verified",cex_quote_fresh_for_alert:true,observed_at:good.last_scanned_at}]};
  const result=select(data,now);
  assert.equal(result.drops.length,1);
  assert.equal(result.drops[0].asin,"SAGE");
  assert.equal(result.cex.length,1);
  assert.equal(result.cex[0].asin,"CEX");
});
