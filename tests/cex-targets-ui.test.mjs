import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const ui=require('../docs/cex-targets.js');
const now=Date.parse('2026-11-04T12:00:00Z');
const record={checked_at:'2026-11-04T11:00:00Z',source:'catalogue_search',quote:{box_id:'box',box_name:'Sony PS5 Console White, B',cash_eur:250,category_name:'Playstation5 Consoles'}};
test('5,000-target limit and exclusive cash floor',()=>{
  const items=Object.fromEntries(Array.from({length:5200},(_,i)=>[i,{...record,quote:{...record.quote,box_id:String(i),cash_eur:i+11}}]));
  const rows=ui.rows({cex_targets:{items}}, {}, {}, now);
  assert.equal(rows.length,5000);assert.ok(rows.every(r=>r.quote.cash_eur>10));
});
test('colour and grade variants collapse into one grouped row', () => {
  const black = {...record, quote: {...record.quote, box_id: 'b1', box_name: 'Sony PS5 Pro 2TB - Black, B', cash_eur: 420}};
  const white = {...record, quote: {...record.quote, box_id: 'b2', box_name: 'Sony PS5 Pro 2TB - White, A', cash_eur: 450}};
  const groups = ui.groupTargets(ui.rows({cex_targets:{items:{b1:black,b2:white}}},{},{},now));
  assert.equal(groups.length, 1);
  assert.equal(groups[0].variants.length, 2);
  assert.match(ui.renderRow(groups[0].variants[0], groups[0]), /target-variant/);
});
test('search links cannot masquerade as mapped products or profit',()=>{
  const row=ui.rows({cex_targets:{items:{box:record}}},{},{},now)[0];
  const html=ui.renderRow(row);
  assert.match(html,/Search Amazon/);assert.match(html,/Not verified/);assert.doesNotMatch(html,/Open Amazon product/);
});
test('only fresh successful checks contribute profitable counts',()=>{
  const mapping={asin:'B000000001',cex_box_id:'box'};
  const cat={cex_targets:{items:{box:record},mappings:{B000000001:mapping}}};
  const check={asin:mapping.asin,status:'profitable',checked_at:'2026-11-04T11:50:00Z',cex_quote_checked_at:'2026-11-04T11:00:00Z',cex_quote_fresh_for_alert:true,cex_cash_profit_eur:50,price_eur:170};
  assert.equal(ui.rows(cat,{products:[check]},{},now)[0].profitable,true);
  for (const change of [{status:'price_hidden'},{checked_at:'2026-11-04T10:00:00Z'},{cex_quote_checked_at:'2026-11-02T11:00:00Z'},{cex_quote_fresh_for_alert:false}]) {
    const row=ui.rows(cat,{products:[{...check,...change}]},{},now)[0];assert.equal(row.profitable,false);assert.match(ui.renderRow(row),/Not verified/);
  }
});
test('untrusted product names and IDs cannot inject HTML or external destinations',()=>{
  const row=ui.rows({cex_targets:{items:{box:{...record,quote:{...record.quote,box_name:'<img src=x onerror=alert(1)>',box_id:'x" onclick="bad'}}}}},{},{},now)[0];
  const html=ui.renderRow(row);assert.doesNotMatch(html,/<img/);assert.match(html,/&lt;img/);assert.match(html,/https:\/\/ie.webuy.com/);
});
test('seed is dated, has exactly 1,000 real discovery rows and expires',()=>{
  const seed=JSON.parse(readFileSync(new URL('../docs/cex-targets-seed.json',import.meta.url)));
  assert.equal(Object.keys(seed.items).length,1000);
  const at=Date.parse(seed.generated_at)+1000;
  assert.equal(ui.rows({}, {}, seed, at).length,1000);
  assert.equal(ui.rows({}, {}, seed, at+73*3600000).length,0);
  assert.ok(Object.values(seed.items).every(r=>r.quote.cash_eur>10 && r.source==='catalogue_search'));
});
test('coffee stays Amazon-only and respects branded review and price criteria',()=>{
  const rows=ui.coffee({sections:{coffee:{items:[{resale_eligible:true,active:true,price_eur:250,review_count:200},{resale_eligible:false,price_eur:500,review_count:500}]}}});
  assert.equal(rows.length,1);
  const html=readFileSync(new URL('../docs/cex-targets.html',import.meta.url),'utf8');
  assert.match(html,/CeX acceptance is not established/);
});
test('discovered ASINs link directly without becoming verified mappings',()=>{
  const candidate={asin:'B000000123',active:true,cex_target_box_id:'box',identity_status:'identifiers_missing'};
  const cat={cex_targets:{items:{box:record}},sections:{tech:{items:[candidate]}}};
  const row=ui.rows(cat,{},{},now)[0];
  assert.equal(row.mapping,undefined);assert.equal(row.profitable,false);
  assert.match(ui.renderRow(row),/amazon.ie\/dp\/B000000123/);
  assert.match(ui.renderRow(row),/Suggested match|Possible match/);assert.match(ui.renderRow(row),/Not verified/);
  candidate.identity_status='identity_mismatch';
  assert.match(ui.renderRow(ui.rows(cat,{},{},now)[0]),/Search Amazon/);
});
test('history shows real rises and falls, ordered by timestamp, without inventing points',()=>{
  const points=ui.historyFor({price_history:[{price_eur:100,observed_at:'2026-11-01T12:00:00Z'},{price_eur:150,observed_at:'2026-11-02T12:00:00Z'},{price_eur:80,observed_at:'2026-11-03T12:00:00Z'},{price_eur:null,observed_at:'2026-11-04T12:00:00Z'}]});
  assert.deepEqual(points.map(p=>p.price_eur),[100,150,80]);
  assert.match(ui.renderHistory(points),/<svg/); assert.match(ui.renderHistory(points),/prices between checks are unknown/);
  assert.doesNotMatch(ui.renderHistory(points.slice(0,1)),/<svg/);
});
test('candidate Amazon prices are dated and never establish profit',()=>{
  const candidate={asin:'B000000123',title:'Sony PS5',active:true,cex_target_box_id:'box',price_eur:200,last_seen_at:'2026-11-04T11:00:00Z'};
  const row=ui.rows({cex_targets:{items:{box:record}},sections:{tech:{items:[candidate]}}},{},{},now)[0];
  assert.equal(row.observed.price_eur,200);assert.equal(row.profitable,false);
  assert.match(ui.renderRow(row),/Last observed/);assert.match(ui.renderRow(row),/candidate, unverified/);
});
test('pagination markup preserves phone controls for a thousand targets',()=>{
  const html=readFileSync(new URL('../docs/cex-targets.html',import.meta.url),'utf8');
  assert.match(html,/id="target-prev"/);assert.match(html,/id="target-next"/);
});

test('persisted candidates survive section replacement without implying profit',()=>{
  const candidate={asin:'B000000123',title:'Sony PS5',price_eur:null,last_seen_at:'2026-11-04T11:00:00Z'};
  const cat={cex_targets:{items:{box:record},amazon_discovery:{box:{candidate_products:[candidate]}}}};
  let row=ui.rows(cat,{},{},now)[0];
  assert.match(ui.renderRow(row),/amazon.ie\/dp\/B000000123/);
  assert.match(ui.renderRow(row),/Amazon price pending/);
  assert.equal(row.profitable,false);assert.equal(row.mapping,undefined);
  cat.sections={x:{items:[{...candidate,identity_status:'identity_mismatch'}]}};
  assert.match(ui.renderRow(ui.rows(cat,{},{},now)[0]),/Search Amazon/);
});
