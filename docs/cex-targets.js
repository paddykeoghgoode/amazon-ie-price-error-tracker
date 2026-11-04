'use strict';
const TargetPage = (() => {
  const finite = n => n !== null && n !== undefined && n !== '' && Number.isFinite(Number(n));
  const money = n => finite(n) ? new Intl.NumberFormat('en-IE', {style:'currency', currency:'EUR'}).format(Number(n)) : '—';
  const date = s => s && Number.isFinite(Date.parse(s)) ? new Intl.DateTimeFormat('en-IE', {dateStyle:'medium',timeStyle:'short',timeZone:'Europe/Dublin'}).format(new Date(s)) : 'Not checked';
  const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  function historyFor(...records) {
    const points = new Map();
    for (const r of records.filter(Boolean)) {
      const observations = [...(r.price_history || [])];
      if (!r.status || ['price_read','profitable','above_buy_ceiling','below_minimum_buy'].includes(r.status)) {
        observations.push({price_eur:r.price_eur,observed_at:r.last_seen_at || r.checked_at,source:r.checked_at ? 'product_page' : 'listing'});
      }
      for (const p of observations) {
        const stamp=Date.parse(p.observed_at || '');
        if (Number.isFinite(stamp) && finite(p.price_eur) && Number(p.price_eur)>0) points.set(stamp,{...p,price_eur:Number(p.price_eur)});
      }
    }
    return [...points].sort((a,b)=>a[0]-b[0]).slice(-120).map(([,p])=>p);
  }
  function renderHistory(points) {
    if (points.length < 2) return `<span class="target-meta">${points.length ? 'One observation; graph builds as prices are checked.' : 'No recorded Amazon price history yet.'}</span>`;
    const prices=points.map(p=>p.price_eur), min=Math.min(...prices), max=Math.max(...prices);
    const pad=Math.max((max-min)*.1,1), low=Math.max(0,min-pad), high=max+pad;
    const first=Date.parse(points[0].observed_at), last=Date.parse(points.at(-1).observed_at);
    const coords=points.map(p=>[44+256*(Date.parse(p.observed_at)-first)/(last-first || 1),18+90*(high-p.price_eur)/(high-low)]);
    const path=coords.map((p,i)=>`${i?'L':'M'}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join(' ');
    const label=`Recorded Amazon prices: lowest ${money(min)}, highest ${money(max)}. ${points.length} observations.`;
    return `<details class="price-history"><summary>Price history · ${points.length} observations</summary><svg viewBox="0 0 320 132" role="img" aria-label="${esc(label)}"><path class="chart-axis" d="M44 12V114H305"/><text x="2" y="22">${esc(money(high))}</text><text x="2" y="110">${esc(money(low))}</text><path class="chart-line" d="${path}"/>${coords.map((xy,i)=>`<circle cx="${xy[0].toFixed(1)}" cy="${xy[1].toFixed(1)}" r="2.5"><title>${esc(money(points[i].price_eur)+' · '+date(points[i].observed_at))}</title></circle>`).join('')}</svg><p class="target-meta">${esc(date(points[0].observed_at))} → ${esc(date(points.at(-1).observed_at))}<br>Low ${money(min)} · High ${money(max)}. Lines connect recorded observations; prices between checks are unknown.</p><details><summary>View observations</summary><ol class="price-observations">${points.map(p=>`<li>${esc(date(p.observed_at))}: ${money(p.price_eur)} <span class="target-meta">${esc((p.source || 'observation').replaceAll('_',' '))}</span></li>`).join('')}</ol></details></details>`;
  }
  const category = q => {
    const s = `${q.category_name || ''} ${q.box_name || ''}`.toLowerCase();
    if (/console|steam deck|vr headset/.test(s)) return 'console';
    if (/games|software/.test((q.category_name || '').toLowerCase())) return 'game';
    if (/headphone|headset|speaker|audio|earbud/.test(s)) return 'audio';
    if (/cpu|graphics|processor|laptop|comput|mouse|keyboard|memory/.test(s)) return 'computing';
    return 'other';
  };
  function rows(catalogue, hotlist, seed = {}, now = Date.now()) {
    const state = catalogue.cex_targets || {};
    const seedAge = now - Date.parse(seed.generated_at || '');
    const freshSeed = seedAge >= 0 && seedAge <= 72*3600000;
    const items = {...(freshSeed ? seed.items : {}), ...state.items};
    const maps = Object.values(state.mappings || {});
    const inventory = Object.values(catalogue.sections || {}).flatMap(s => s.items || []);
    const candidates = inventory.filter(r => r.active !== false && r.identity_status !== 'identity_mismatch');
    const inventoryByAsin = new Map();
    for (const row of inventory) { if (!inventoryByAsin.has(row.asin)) inventoryByAsin.set(row.asin,[]); inventoryByAsin.get(row.asin).push(row); }
    const candidateByBox = new Map();
    for (const row of candidates) if (/^[A-Z0-9]{10}$/.test(row.asin) && row.cex_target_box_id && (!candidateByBox.has(row.cex_target_box_id) || (row.last_seen_at || '') > (candidateByBox.get(row.cex_target_box_id).last_seen_at || ''))) candidateByBox.set(row.cex_target_box_id,row);
    const checks = new Map((hotlist.products || []).map(r => [r.asin, r]));
    return Object.values(items).map(record => {
      let quote = record.quote || {};
      const mapping = maps.find(m => m.cex_box_id === quote.box_id);
      if (mapping?.cex_quote_cache?.quote && Date.parse(mapping.cex_quote_cache.checked_at || '') > Date.parse(record.checked_at || '')) {
        record = mapping.cex_quote_cache; quote = record.quote;
      }
      const candidate = candidateByBox.get(quote.box_id);
      const selected = mapping || candidate;
      const check = selected ? checks.get(selected.asin) : null;
      const history = historyFor(...(inventoryByAsin.get(selected?.asin) || []).filter(r => r.title === selected.title), selected, check);
      const checkedAge = now - Date.parse(check?.checked_at || '');
      const quoteAge = now - Date.parse(check?.cex_quote_checked_at || '');
      const fresh = checkedAge >= 0 && checkedAge <= 30*60000 && quoteAge >= 0 && quoteAge <= 12*3600000 && check?.cex_quote_fresh_for_alert === true;
      const read = !!mapping && fresh && ['profitable','above_buy_ceiling','below_minimum_buy'].includes(check?.status);
      return {record, quote, mapping, candidate, check, history, observed:history.at(-1), fresh:read, group:category(quote),
        profitable:read && check?.status === 'profitable', discovery:state.amazon_discovery?.[quote.box_id]};
    }).filter(r => finite(r.quote.cash_eur) && r.quote.cash_eur > 10 && r.quote.box_id && !/non[ -]working|faulty|spares/i.test(r.quote.box_name || ''))
      .sort((a,b) => Number(b.profitable)-Number(a.profitable) || Number(!!b.mapping)-Number(!!a.mapping) ||
        (['console','game'].includes(b.group) ? 1:0)-(['console','game'].includes(a.group) ? 1:0) || b.quote.cash_eur-a.quote.cash_eur)
      .slice(0,1000);
  }
  function coffee(catalogue) {
    return ((catalogue.sections || {}).coffee?.items || []).filter(r => r.active !== false && r.resale_eligible === true && finite(r.price_eur) && r.price_eur >= 100 && (r.review_count || 0) >= 100)
      .sort((a,b) => b.price_eur-a.price_eur).slice(0,30);
  }
  function renderRow(r) {
    const {quote:q,mapping:m,check:c} = r;
    const cex = `https://ie.webuy.com/product-detail?id=${encodeURIComponent(q.box_id)}`;
    const matched = m && /^[A-Z0-9]{10}$/.test(m.asin);
    const asin = matched ? m.asin : r.candidate?.asin;
    const amazon = asin ? `https://www.amazon.ie/dp/${asin}` : `https://www.amazon.ie/s?k=${encodeURIComponent(q.box_name.replace(/,\s*(?:[ABC]|mint|boxed|unboxed|discounted)\s*$/i, ''))}`;
    const stage = matched ? 'Exact identity mapped' : asin ? 'Possible match · verify model and variant' : r.discovery?.status === 'candidates_found' ? 'Candidate found · needs verification' : 'Needs matching';
    return `<tr><td><a href="${cex}" target="_blank" rel="noopener">${esc(q.box_name)}</a><span class="target-meta">${esc(q.category_name || 'CeX catalogue')} · ${esc(q.box_id)}</span></td><td><span class="target-money">${money(q.cash_eur)}</span><span class="target-meta">${r.record.source === 'catalogue_search' ? 'Index estimate' : 'Detail quote'} · ${esc(date(r.record.checked_at))}</span></td><td><a class="amazon-action" href="${amazon}" target="_blank" rel="noopener">${matched ? 'Open Amazon product' : asin ? 'Check possible Amazon match' : 'Search Amazon ↗'}</a><span class="target-meta">${esc(stage)}</span>${r.observed ? `<span class="target-money">${money(r.observed.price_eur)}</span><span class="target-meta">Last observed ${esc(date(r.observed.observed_at))} · ${matched ? 'mapped product' : 'candidate, unverified'}</span>` : '<span class="target-meta">Amazon price pending</span>'}${renderHistory(r.history || [])}</td><td class="${r.profitable ? 'target-good' : ''}">${r.fresh ? money(c.cex_cash_profit_eur) : 'Not verified'}<span class="target-meta">${r.profitable ? 'Meets alert margin' : r.fresh ? 'Below alert margin' : 'Awaiting a fresh comparable check'}</span></td><td>${esc(date(c?.checked_at || r.discovery?.checked_at))}<span class="target-meta">${esc(c?.status?.replaceAll('_',' ') || r.discovery?.status?.replaceAll('_',' ') || 'Amazon not checked')}</span></td></tr>`;
  }
  return {rows, coffee, renderRow, category, date, money, esc, historyFor, renderHistory};
})();
if (typeof module !== 'undefined') module.exports = TargetPage;
if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  async function read(path, optional=false) {
    try { const response = await fetch(path, {cache:'no-store'}); if (!response.ok) throw Error('HTTP '+response.status); return await response.json(); }
    catch (error) { if (optional) return {}; throw error; }
  }
  async function load() {
    const results = await Promise.allSettled([read('catalogue.json'),read('hotlist.json',true),read('cex-targets-seed.json',true)]);
    const catalogue = results[0].status === 'fulfilled' ? results[0].value : {};
    const hotlist = results[1].status === 'fulfilled' ? results[1].value : {};
    const seed = results[2].status === 'fulfilled' ? results[2].value : {};
    const rows = TargetPage.rows(catalogue,hotlist,seed);
    $('target-count').textContent = rows.length;
    $('mapped-count').textContent = rows.filter(r=>r.mapping).length;
    $('profitable-count').textContent = rows.filter(r=>r.profitable).length;
    $('target-status').textContent = results[0].status === 'rejected' ? 'Live catalogue unavailable. Showing the dated discovery snapshot where available; mapping and profit are not verified.' : `Published ${TargetPage.date(catalogue.generated_at)}. ${rows.length} of up to 1,000 targets available. Matches fill as scans complete.`;
    let page = 0;
    const pageSize = 50;
    function render() {
      const query = $('target-search').value.toLowerCase().trim(), group=$('target-category').value, match=$('target-matching').value;
      const visible=rows.filter(r=>(!query || `${r.quote.box_name} ${r.quote.box_id}`.toLowerCase().includes(query)) && (!group || r.group===group) && (!match || (match==='mapped' ? !!r.mapping : !r.mapping)));
      page = Math.min(page, Math.max(0, Math.ceil(visible.length/pageSize)-1));
      const start=page*pageSize;
      $('target-rows').innerHTML=visible.slice(start,start+pageSize).map(TargetPage.renderRow).join('');
      $('target-page').textContent=`Page ${page+1} of ${Math.max(1,Math.ceil(visible.length/pageSize))}`;
      $('target-prev').disabled=page===0; $('target-next').disabled=start+pageSize>=visible.length;
      $('target-table').hidden=!visible.length; $('target-empty').hidden=!!visible.length;
      $('target-result-count').textContent=`${visible.length} matching targets · showing ${visible.length ? start+1 : 0}–${Math.min(start+pageSize,visible.length)} · cash payouts`;
    }
    ['target-search','target-category','target-matching'].forEach(id=>$(id).addEventListener('input',()=>{page=0;render();}));
    $('target-prev').addEventListener('click',()=>{page--;render();});
    $('target-next').addEventListener('click',()=>{page++;render();});
    render();
    const coffee=TargetPage.coffee(catalogue);
    $('coffee-table').hidden=!coffee.length; $('coffee-empty').hidden=!!coffee.length;
    $('coffee-rows').innerHTML=coffee.filter(r=>/^[A-Z0-9]{10}$/.test(r.asin)).map(r=>`<tr><td><a href="https://www.amazon.ie/dp/${r.asin}" target="_blank" rel="noopener">${TargetPage.esc(r.title)}</a></td><td>${TargetPage.money(r.price_eur)}${TargetPage.renderHistory(TargetPage.historyFor(r))}</td><td>${TargetPage.esc(TargetPage.date(r.last_seen_at))}</td></tr>`).join('');
  }
  load().catch(()=>{ $('target-status').textContent='Could not load target data. Please reload or return to the tracker.'; });
}
