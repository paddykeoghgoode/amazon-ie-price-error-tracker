import test from 'node:test';
import assert from 'node:assert/strict';
import {createRequire} from 'node:module';
import {readFileSync} from 'node:fs';
const require=createRequire(import.meta.url);
const ui=require('../docs/cex-targets.js');
const now=Date.parse('2026-11-04T12:00:00Z');
const record={checked_at:'2026-11-04T11:00:00Z',source:'catalogue_search',quote:{box_id:'box',box_name:'Sony PS5 Console White, B',cash_eur:250,category_name:'Playstation5 Consoles'}};
test('100-target limit and exclusive cash floor',()=>{
  const items=Object.fromEntries(Array.from({length:120},(_,i)=>[i,{...record,quote:{...record.quote,box_id:String(i),cash_eur:i}}]));
  const rows=ui.rows({cex_targets:{items}}, {}, {}, now);
  assert.equal(rows.length,100);assert.ok(rows.every(r=>r.quote.cash_eur>10));
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
test('seed is dated, has exactly 100 real discovery rows and expires',()=>{
  const seed=JSON.parse(readFileSync(new URL('../docs/cex-targets-seed.json',import.meta.url)));
  assert.equal(Object.keys(seed.items).length,100);
  const at=Date.parse(seed.generated_at)+1000;
  assert.equal(ui.rows({}, {}, seed, at).length,100);
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
  assert.match(ui.renderRow(row),/Possible match/);assert.match(ui.renderRow(row),/Not verified/);
  candidate.identity_status='identity_mismatch';
  assert.match(ui.renderRow(ui.rows(cat,{},{},now)[0]),/Search Amazon/);
});
