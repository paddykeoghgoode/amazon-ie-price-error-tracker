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
  const USER_LINKS_STORAGE = 'cexUserLinks.v1';
  const REJECTED_STORAGE = 'cexUserRejected.v1';
  const CEX_TARGET_LIMIT = 5000;
  const asinOk = asin => /^[A-Z0-9]{10}$/.test(asin || '');
  function readLocalUserLinks() {
    if (typeof localStorage === 'undefined') return {};
    try { return JSON.parse(localStorage.getItem(USER_LINKS_STORAGE) || '{}'); } catch { return {}; }
  }
  function readRejected() {
    if (typeof localStorage === 'undefined') return {};
    try { return JSON.parse(localStorage.getItem(REJECTED_STORAGE) || '{}'); } catch { return {}; }
  }
  function exportUserLinksPayload(fileLinks = {}, localLinks = readLocalUserLinks()) {
    const links = {};
    for (const [boxId, entry] of Object.entries({...fileLinks, ...localLinks})) {
      if (!entry || !asinOk(entry.asin)) continue;
      links[boxId] = {asin: entry.asin, amazon_title: entry.amazon_title || entry.title || '', confirmed_at: entry.confirmed_at || new Date().toISOString(), source: entry.source || 'user'};
    }
    return {_comment: 'Commit as docs/cex-user-links.json so scans reuse these links.', links};
  }
  function rows(catalogue, hotlist, seed = {}, now = Date.now(), userLinksFile = {}, localUserLinks = readLocalUserLinks()) {
    const state = catalogue.cex_targets || {};
    const userLinks = {...userLinksFile, ...localUserLinks};
    const seedAge = now - Date.parse(seed.generated_at || '');
    const freshSeed = seedAge >= 0 && seedAge <= 72*3600000;
    const items = {...(freshSeed ? seed.items : {}), ...state.items};
    const maps = Object.values(state.mappings || {});
    const inventory = Object.values(catalogue.sections || {}).flatMap(s => s.items || []);
    const candidates = inventory.filter(r => r.active !== false && r.identity_status !== 'identity_mismatch');
    const inventoryByAsin = new Map();
    for (const row of inventory) { if (!inventoryByAsin.has(row.asin)) inventoryByAsin.set(row.asin,[]); inventoryByAsin.get(row.asin).push(row); }
    const candidateByBox = new Map();
    for (const row of candidates) if (asinOk(row.asin) && row.cex_target_box_id && (!candidateByBox.has(row.cex_target_box_id) || (row.last_seen_at || '') > (candidateByBox.get(row.cex_target_box_id).last_seen_at || ''))) candidateByBox.set(row.cex_target_box_id,row);
    const checks = new Map((hotlist.products || []).map(r => [r.asin, r]));
    const rejected = readRejected();
    return Object.values(items).map(record => {
      let quote = record.quote || {};
      const boxId = quote.box_id;
      const userLink = userLinks[boxId];
      const proposed = (state.proposed_links || {})[boxId];
      const mapping = maps.find(m => m.cex_box_id === quote.box_id);
      if (mapping?.cex_quote_cache?.quote && Date.parse(mapping.cex_quote_cache.checked_at || '') > Date.parse(record.checked_at || '')) {
        record = mapping.cex_quote_cache; quote = record.quote;
      }
      const saved = state.amazon_discovery?.[quote.box_id]?.candidate_products || [];
      let candidate = candidateByBox.get(quote.box_id);
      if (!candidate && proposed && asinOk(proposed.asin)) {
        candidate = {asin: proposed.asin, title: proposed.amazon_title, match_percent: proposed.match_percent, last_seen_at: proposed.proposed_at};
      }
      if (!candidate) candidate = saved.find(r =>
        asinOk(r.asin) &&
        !(inventoryByAsin.get(r.asin) || []).some(x => x.identity_status === 'identity_mismatch') &&
        now - Date.parse(r.last_seen_at || '') >= 0 && now - Date.parse(r.last_seen_at || '') <= 30*86400000);
      const userMapping = userLink && asinOk(userLink.asin) ? {asin: userLink.asin, cex_box_id: boxId, match_confidence: 'user_confirmed'} : null;
      const selected = mapping || userMapping || candidate;
      const check = selected ? checks.get(selected.asin) : null;
      const history = historyFor(...(inventoryByAsin.get(selected?.asin) || []).filter(r => r.title === selected.title), selected, check);
      const checkedAge = now - Date.parse(check?.checked_at || '');
      const quoteAge = now - Date.parse(check?.cex_quote_checked_at || '');
      const fresh = checkedAge >= 0 && checkedAge <= 30*60000 && quoteAge >= 0 && quoteAge <= 12*3600000 && check?.cex_quote_fresh_for_alert === true;
      const read = !!mapping && fresh && ['profitable','above_buy_ceiling','below_minimum_buy'].includes(check?.status);
      const matchPercent = candidate?.match_percent ?? proposed?.match_percent ?? state.amazon_discovery?.[quote.box_id]?.best_match_percent;
      return {record, quote, mapping, userMapping, userLink, proposed, candidate, check, history, observed:history.at(-1), fresh:read, group:category(quote),
        matchPercent, profitable:read && check?.status === 'profitable', discovery:state.amazon_discovery?.[quote.box_id], boxId};
    }).filter(r => finite(r.quote.cash_eur) && r.quote.cash_eur > 10 && r.quote.box_id && !/non[ -]working|faulty|spares/i.test(r.quote.box_name || '') && !rejected[r.boxId])
      .sort((a,b) => Number(b.profitable)-Number(a.profitable) || Number(!!b.userMapping)-Number(!!a.userMapping) || Number(!!b.mapping)-Number(!!a.mapping) ||
        (Number(b.matchPercent)||0)-(Number(a.matchPercent)||0) ||
        (['console','game'].includes(b.group) ? 1:0)-(['console','game'].includes(a.group) ? 1:0) || b.quote.cash_eur-a.quote.cash_eur)
      .slice(0, CEX_TARGET_LIMIT);
  }
  function familyKey(boxName) {
    let t = String(boxName || '');
    t = t.replace(/,\s*(?:[ABC]|boxed|unboxed|discounted|mint)\s*$/i, '');
    t = t.replace(/\s*-\s*(?:black|white|silver|gold|blue|red|green|pink|purple|grey|gray|midnight blue|cosmic red|volcanic red|glacier white|starlight|graphite|charcoal|navy|cream|natural|obsidian|frost|platinum|titanium|smoke|amber|copper|bronze|beige|lavender|indigo|orange|yellow|lime|teal|sand|snow|carbon|pebble|stone|ash|mocha|espresso|champagne|rose gold|violet|burgundy|maroon|cyan|ivory|pearl|chrome|gunmetal|slate|forest|emerald|ruby|sapphire|lilac|blush|taupe|sandstone|honey|sage|moss|mint|olive|mustard|wine|coral|teal|azure|indigo|magenta|peach|nude|clear|transparent)\b.*$/gi, '');
    t = t.replace(/\s*\([^)]*\)\s*$/g, '');
    return t.replace(/\s+/g, ' ').trim().toLowerCase();
  }
  function groupLabel(boxName) {
    return String(boxName || '').replace(/,\s*(?:[ABC]|boxed|unboxed|discounted|mint)\s*$/i, '')
      .replace(/\s*-\s*(?:black|white|silver|gold|blue|red|green|pink|purple|grey|gray|midnight blue|cosmic red|volcanic red|glacier white|starlight|graphite|charcoal|navy|cream|natural|obsidian|frost|platinum|titanium|smoke|amber|copper|bronze|beige|lavender|indigo|orange|yellow|lime|teal|sand|snow|carbon|pebble|stone|ash|mocha|espresso|champagne|rose gold|violet|burgundy|maroon|cyan|ivory|pearl|chrome|gunmetal|slate|forest|emerald|ruby|sapphire|lilac|blush|taupe|sandstone|honey|sage|moss|mint|olive|mustard|wine|coral|teal|azure|indigo|magenta|peach|nude|clear|transparent)\b.*$/gi, '')
      .replace(/\s+/g, ' ').trim();
  }
  function variantLabel(boxName) {
    const full = String(boxName || '');
    const base = groupLabel(full);
    const tail = full.slice(base.length).replace(/^[\s,-]+/, '').replace(/,\s*[ABC]\s*$/i, '').trim();
    return tail || full.replace(/,\s*[ABC]\s*$/i, '').trim();
  }
  function groupTargets(flat) {
    const groups = new Map();
    for (const row of flat) {
      const key = familyKey(row.quote.box_name);
      const bucket = groups.get(key) || {key, label: groupLabel(row.quote.box_name), variants: []};
      bucket.variants.push(row);
      groups.set(key, bucket);
    }
    return [...groups.values()].map((group) => {
      group.variants.sort((a, b) => b.quote.cash_eur - a.quote.cash_eur || a.boxId.localeCompare(b.boxId));
      return group;
    }).sort((a, b) => {
      const best = (g) => Math.max(...g.variants.map(v => v.quote.cash_eur));
      return best(b) - best(a) || a.label.localeCompare(b.label);
    });
  }
  function coffee(catalogue) {
    return ((catalogue.sections || {}).coffee?.items || []).filter(r => r.active !== false && r.resale_eligible === true && finite(r.price_eur) && r.price_eur >= 100 && (r.review_count || 0) >= 100)
      .sort((a,b) => b.price_eur-a.price_eur).slice(0,30);
  }
  function renderRow(r, group) {
    const {quote:q,mapping:m,check:c,userMapping:u,userLink:ul,proposed:p,candidate:can} = r;
    const cex = `https://ie.webuy.com/product-detail?id=${encodeURIComponent(q.box_id)}`;
    const matched = m && asinOk(m.asin);
    const userConfirmed = u && asinOk(u.asin);
    const asin = matched ? m.asin : userConfirmed ? u.asin : can?.asin;
    const amazon = asin ? `https://www.amazon.ie/dp/${asin}` : `https://www.amazon.ie/s?k=${encodeURIComponent(q.box_name.replace(/,\s*(?:[ABC]|mint|boxed|unboxed|discounted)\s*$/i, ''))}`;
    const pct = finite(r.matchPercent) ? `${Math.round(Number(r.matchPercent))}% title match` : '';
    let stage = 'Needs matching · waiting for Amazon search';
    if (matched) stage = 'Scanner-verified identity map';
    else if (userConfirmed) stage = 'Your confirmed link · scans will reuse it';
    else if (asin && pct) stage = `Suggested match · ${pct}`;
    else if (asin) stage = 'Possible match · verify model and variant';
    else if (r.discovery?.status === 'candidates_found') stage = 'Search ran · no strong hit yet';
    const actions = (!matched && !userConfirmed && asinOk(can?.asin)) ? `<div class="target-actions"><button type="button" class="target-confirm" data-box="${esc(r.boxId)}" data-asin="${esc(can.asin)}" data-title="${esc(can.title || '')}">Link this match</button><button type="button" class="target-reject" data-box="${esc(r.boxId)}">Not this product</button></div>` : '';
    const variants = group && group.variants.length > 1 ? `<div class="target-variants" role="group" aria-label="CeX grades and colours">${group.variants.map(v => {
      const active = v.boxId === r.boxId ? ' target-variant-active' : '';
      return `<button type="button" class="target-variant${active}" data-group="${esc(group.key)}" data-box="${esc(v.boxId)}" title="${esc(v.quote.box_name)}">${esc(variantLabel(v.quote.box_name) || v.quote.box_id)} · ${money(v.quote.cash_eur)}</button>`;
    }).join('')}</div>` : '';
    const linkLabel = matched ? 'Open Amazon product' : userConfirmed ? 'Open your linked product' : asin ? 'Review suggested Amazon match' : 'Search Amazon ↗';
    return `<tr data-box-id="${esc(r.boxId)}" data-group-key="${esc(group?.key || r.boxId)}"><td><a href="${cex}" target="_blank" rel="noopener">${esc(group?.label || q.box_name)}</a><span class="target-meta">${esc(q.category_name || 'CeX catalogue')} · ${group && group.variants.length > 1 ? `${group.variants.length} CeX listings` : esc(q.box_id)}</span>${variants}</td><td><span class="target-money">${money(q.cash_eur)}</span><span class="target-meta">${r.record.source === 'catalogue_search' ? 'Index estimate' : 'Detail quote'} · ${esc(date(r.record.checked_at))}</span></td><td><a class="amazon-action" href="${amazon}" target="_blank" rel="noopener">${linkLabel}</a><span class="target-meta">${esc(stage)}</span>${pct && !userConfirmed ? `<span class="target-meta">${esc(pct)} (estimate only)</span>` : ''}${actions}${r.observed ? `<span class="target-money">${money(r.observed.price_eur)}</span><span class="target-meta">Last observed ${esc(date(r.observed.observed_at))} · ${matched ? 'mapped product' : userConfirmed ? 'your link' : 'candidate, unverified'}</span>` : '<span class="target-meta">Amazon price pending</span>'}${renderHistory(r.history || [])}</td><td class="${r.profitable ? 'target-good' : ''}">${r.fresh ? money(c.cex_cash_profit_eur) : 'Not verified'}<span class="target-meta">${r.profitable ? 'Meets alert margin' : r.fresh ? 'Below alert margin' : 'Awaiting a fresh comparable check'}</span></td><td>${esc(date(c?.checked_at || r.discovery?.checked_at || p?.proposed_at))}<span class="target-meta">${esc(c?.status?.replaceAll('_',' ') || r.discovery?.status?.replaceAll('_',' ') || (p ? 'proposal saved' : 'Amazon not checked'))}</span></td></tr>`;
  }
  return {rows, groupTargets, coffee, renderRow, category, date, money, esc, historyFor, renderHistory,
    readLocalUserLinks, exportUserLinksPayload, USER_LINKS_STORAGE, REJECTED_STORAGE, asinOk, CEX_TARGET_LIMIT,
    familyKey, variantLabel};
})();
if (typeof module !== 'undefined') module.exports = TargetPage;
if (typeof document !== 'undefined') {
  const $ = id => document.getElementById(id);
  async function read(path, optional=false) {
    try { const response = await fetch(path, {cache:'no-store'}); if (!response.ok) throw Error('HTTP '+response.status); return await response.json(); }
    catch (error) { if (optional) return {}; throw error; }
  }
  async function load() {
    const results = await Promise.allSettled([read('catalogue.json'),read('hotlist.json',true),read('cex-targets-seed.json',true),read('cex-user-links.json',true)]);
    const catalogue = results[0].status === 'fulfilled' ? results[0].value : {};
    const hotlist = results[1].status === 'fulfilled' ? results[1].value : {};
    const seed = results[2].status === 'fulfilled' ? results[2].value : {};
    const userFile = results[3].status === 'fulfilled' ? (results[3].value.links || {}) : {};
    let flatRows = TargetPage.rows(catalogue,hotlist,seed,Date.now(),userFile);
    let groups = TargetPage.groupTargets(flatRows);
    const variantPick = new Map();
    const activeRow = (group) => {
      const pick = variantPick.get(group.key);
      return group.variants.find(v => v.boxId === pick) || group.variants[0];
    };
    $('target-count').textContent = flatRows.length;
    $('mapped-count').textContent = flatRows.filter(r=>r.mapping || r.userMapping).length;
    $('profitable-count').textContent = flatRows.filter(r=>r.profitable).length;
    $('target-status').textContent = results[0].status === 'rejected' ? 'Live catalogue unavailable. Showing the dated discovery snapshot where available; mapping and profit are not verified.' : `Published ${TargetPage.date(catalogue.generated_at)}. Scans search Amazon from CeX titles and store a match %. Use Link this match, then export JSON into docs/cex-user-links.json so Actions reuse your links.`;
    let page = 0;
    const pageSize = 50;
    function render() {
      const visible=groups.filter(g=>{
        const row=activeRow(g);
        const query = $('target-search').value.toLowerCase().trim(), groupCat=$('target-category').value, match=$('target-matching').value;
        return (!query || `${g.label} ${row.quote.box_id}`.toLowerCase().includes(query)) && (!groupCat || row.group===groupCat) && (!match || (match==='mapped' ? !!(row.mapping || row.userMapping) : !(row.mapping || row.userMapping)));
      });
      page = Math.min(page, Math.max(0, Math.ceil(visible.length/pageSize)-1));
      const start=page*pageSize;
      $('target-rows').innerHTML=visible.slice(start,start+pageSize).map(g=>TargetPage.renderRow(activeRow(g), g)).join('');
      $('target-page').textContent=`Page ${page+1} of ${Math.max(1,Math.ceil(visible.length/pageSize))}`;
      $('target-prev').disabled=page===0; $('target-next').disabled=start+pageSize>=visible.length;
      $('target-table').hidden=!visible.length; $('target-empty').hidden=!!visible.length;
      $('target-result-count').textContent=`${visible.length} matching targets · showing ${visible.length ? start+1 : 0}–${Math.min(start+pageSize,visible.length)} · cash payouts`;
    }
    ['target-search','target-category','target-matching'].forEach(id=>$(id).addEventListener('input',()=>{page=0;render();}));
    $('target-prev').addEventListener('click',()=>{page--;render();});
    $('target-next').addEventListener('click',()=>{page++;render();});
    $('target-rows').addEventListener('click', event => {
      const variant = event.target.closest('.target-variant');
      if (variant) {
        variantPick.set(variant.dataset.group, variant.dataset.box);
        render();
        return;
      }
      const confirm = event.target.closest('.target-confirm');
      const reject = event.target.closest('.target-reject');
      if (!confirm && !reject) return;
      const boxId = (confirm || reject).dataset.box;
      if (reject) {
        const rejected = JSON.parse(localStorage.getItem(TargetPage.REJECTED_STORAGE) || '{}');
        rejected[boxId] = true;
        localStorage.setItem(TargetPage.REJECTED_STORAGE, JSON.stringify(rejected));
        flatRows = TargetPage.rows(catalogue, hotlist, seed, Date.now(), userFile);
        groups = TargetPage.groupTargets(flatRows);
        render();
        return;
      }
      const links = TargetPage.readLocalUserLinks();
      links[boxId] = {asin: confirm.dataset.asin, amazon_title: confirm.dataset.title, confirmed_at: new Date().toISOString(), source: 'browser'};
      localStorage.setItem(TargetPage.USER_LINKS_STORAGE, JSON.stringify(links));
      flatRows = TargetPage.rows(catalogue, hotlist, seed, Date.now(), userFile, links);
      groups = TargetPage.groupTargets(flatRows);
      render();
    });
    const exportBtn = $('target-export-links');
    if (exportBtn) exportBtn.addEventListener('click', () => {
      const payload = TargetPage.exportUserLinksPayload(userFile, TargetPage.readLocalUserLinks());
      const blob = new Blob([JSON.stringify(payload, null, 2)], {type: 'application/json'});
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = 'cex-user-links.json'; a.click();
      URL.revokeObjectURL(url);
    });
    render();
    const coffee=TargetPage.coffee(catalogue);
    $('coffee-table').hidden=!coffee.length; $('coffee-empty').hidden=!!coffee.length;
    $('coffee-rows').innerHTML=coffee.filter(r=>/^[A-Z0-9]{10}$/.test(r.asin)).map(r=>`<tr><td><a href="https://www.amazon.ie/dp/${r.asin}" target="_blank" rel="noopener">${TargetPage.esc(r.title)}</a></td><td>${TargetPage.money(r.price_eur)}${TargetPage.renderHistory(TargetPage.historyFor(r))}</td><td>${TargetPage.esc(TargetPage.date(r.last_seen_at))}</td></tr>`).join('');
  }
  load().catch(()=>{ $('target-status').textContent='Could not load target data. Please reload or return to the tracker.'; });
}
