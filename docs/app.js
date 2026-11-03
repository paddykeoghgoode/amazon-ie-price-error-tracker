const DUBLIN = "Europe/Dublin";
const LIVE_REFRESH_MS = 90_000;

let lastDataAt = "";
let lastDealsAt = "";

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function fmtTime(iso) {
  if (!iso) return "—";
  try {
    return new Date(iso).toLocaleString("en-IE", {
      timeZone: DUBLIN,
      day: "numeric",
      month: "short",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
      timeZoneName: "short",
    });
  } catch {
    return iso;
  }
}

function fmtEuro(value) {
  if (value == null || Number.isNaN(Number(value))) return "—";
  return `€${Number(value).toFixed(2)}`;
}

function statusBadge(product) {
  if (product.status === "price_error") {
    return `<span class="badge error">Price error</span>`;
  }
  if (product.status === "fetch_error") {
    return `<span class="badge fetch">Fetch failed</span>`;
  }
  return `<span class="badge ok">Watching</span>`;
}

async function loadData() {
  const res = await fetch(`data.json?t=${Date.now()}`, { cache: "no-store" });
  if (!res.ok) throw new Error("Could not load data.json");
  return res.json();
}

function render(data) {
  const repo = data.github_repo || "paddykeoghgoode/amazon-ie-price-error-tracker";
  document.getElementById("repo-link").href = `https://github.com/${repo}`;
  document.getElementById("actions-link").href = `https://github.com/${repo}/actions/workflows/pages.yml`;

  const status = document.getElementById("status");
  const errorCount = (data.products || []).filter((p) => p.status === "price_error").length;
  const parts = [];
  if (data.generated_at) parts.push(`Watchlist ${fmtTime(data.generated_at)}`);
  else parts.push("Watchlist: no scan yet — run the Pages workflow");
  parts.push(data.telegram_configured ? "Telegram: ready" : "Telegram: add repo secrets");
  if (data.whatsapp_configured) parts.push("WhatsApp: ready");
  parts.push(`Flag at ≥${data.min_discount_percent || 75}% off`);
  parts.push("Page auto-refreshes every 90s");
  status.textContent = parts.join(" · ");
  status.className = "status " + (errorCount ? "alert" : data.generated_at ? "ok" : "warn");

  const products = data.products || [];
  const table = document.getElementById("products-table");
  const body = document.getElementById("products-body");
  const empty = document.getElementById("products-empty");
  body.innerHTML = "";
  if (!products.length) {
    table.hidden = true;
    empty.hidden = false;
  } else {
    empty.hidden = true;
    table.hidden = false;
    for (const p of products) {
      const tr = document.createElement("tr");
      const title = p.title || p.asin;
      tr.innerHTML = `
        <td><a href="${escapeHtml(p.url)}" target="_blank" rel="noopener">${escapeHtml(title)}</a><div class="hint">${escapeHtml(p.asin)}</div></td>
        <td class="price">${fmtEuro(p.price_eur)}</td>
        <td class="was">${fmtEuro(p.list_price_eur)}</td>
        <td>${statusBadge(p)}${p.note ? `<div class="hint">${escapeHtml(p.note)}</div>` : ""}</td>
        <td>${fmtTime(p.checked_at)}</td>`;
      body.appendChild(tr);
    }
  }

  const alerts = data.alerts || [];
  const list = document.getElementById("alerts-list");
  const alertsEmpty = document.getElementById("alerts-empty");
  list.innerHTML = "";
  if (!alerts.length) {
    list.hidden = true;
    alertsEmpty.hidden = false;
  } else {
    alertsEmpty.hidden = true;
    list.hidden = false;
    for (const a of alerts) {
      const li = document.createElement("li");
      const title = a.product_title || a.asin;
      const telegram = a.notified_telegram ? "Telegram sent" : "Telegram pending";
      li.innerHTML = `
        <div><a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(title)}</a></div>
        <div class="price">${fmtEuro(a.price_eur)}${a.list_price_eur ? ` <span class="was">${fmtEuro(a.list_price_eur)}</span>` : ""}</div>
        <div>${escapeHtml(a.reason || "")}</div>
        <div class="time">${fmtTime(a.created_at)} · ${telegram}</div>`;
      list.appendChild(li);
    }
  }

  const form = document.getElementById("add-form");
  form.onsubmit = (event) => {
    event.preventDefault();
    const url = document.getElementById("product-url").value.trim();
    if (!url) return;
    const issueUrl =
      `https://github.com/${repo}/issues/new` +
      `?labels=watch&title=${encodeURIComponent("Watch: " + url)}&body=${encodeURIComponent(url)}`;
    window.open(issueUrl, "_blank", "noopener");
  };
}

function renderDeepDrops(deals) {
  const container = document.getElementById("deep-sections");
  if (!container) return;
  container.innerHTML = "";
  const tiers = deals.deep_drop_tiers || [40, 50, 66];
  const grouped = deals.deep_drops || {};
  let any = false;
  for (const tier of tiers) {
    const rows = grouped[String(tier)] || [];
    if (!rows.length) continue;
    any = true;
    const block = document.createElement("div");
    block.className = "deep-tier";
    block.innerHTML = `<h3>≥${tier}% off usual Amazon price</h3>`;
    const table = document.createElement("table");
    table.innerHTML = `<thead><tr><th>Product</th><th>Now</th><th>Usual</th><th>Drop</th></tr></thead>`;
    const body = document.createElement("tbody");
    for (const d of rows.slice(0, 20)) {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.title || d.asin)}</a></td>
        <td class="price">${fmtEuro(d.price_eur)}<div class="hint">Found ${fmtTime(d.found_at)}</div></td>
        <td class="was">${fmtEuro(d.median_eur)}</td>
        <td>${escapeHtml(String(d.drop_percent))}%</td>`;
      body.appendChild(tr);
    }
    table.appendChild(body);
    block.appendChild(table);
    container.appendChild(block);
  }
  const summary = document.getElementById("deep-summary");
  if (summary && any) {
    summary.textContent =
      "Observed drops from historical Amazon prices — tiers at 40%, 50% and 66%. Seller, condition and checkout price NOT checked.";
  }
}

function cexStatusLine(deals) {
  const stats = deals.cex_stats || {};
  const lookups = stats.lookups ?? (deals.cex_lookups || []).length;
  const attempted = Number(stats.attempted || 0);
  const minEur = deals.cex_min_cash_profit_eur ?? 5;
  const minPct = deals.cex_min_cash_profit_percent ?? 10;
  if (!lookups && !stats.deep_candidates && !stats.any_price_candidates) {
    return (
      "No CeX data in this snapshot yet — wait for a bestseller section crawl (one runs every 30 minutes, ~5–8 min each)."
    );
  }
  if (!lookups) {
    return (
      `CeX was queued for ${stats.deep_candidates || 0} sale items and ${stats.any_price_candidates || 0} regular-price items, but no CeX match returned a price (API or title match).`
    );
  }
  return `CeX checked ${attempted} records (cached or live); ${lookups} provisional CeX matches · margin ≥€${minEur} or ≥${minPct}% · ${stats.cash_flips || 0} sale leads · ${stats.arbitrage || 0} other leads. Seller/checkout not verified.`;
}

function renderNearMargin(bodyId, tableId, rows) {
  const body = document.getElementById(bodyId);
  const table = document.getElementById(tableId);
  if (!body || !table) return;
  body.innerHTML = "";
  if (!rows.length) {
    table.hidden = true;
    return;
  }
  table.hidden = false;
  for (const row of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>
        <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.title || row.asin)}</a>
        <div class="hint"><a href="${escapeHtml(row.cex_url)}" target="_blank" rel="noopener">CeX listing</a> · need €${Number(row.cex_min_profit_eur).toFixed(0)}</div>
      </td>
      <td class="price">${fmtEuro(row.price_eur)}<div class="hint">Found ${fmtTime(row.found_at || row.verified_at)}</div></td>
      <td class="price">${fmtEuro(row.cex_cash_eur)}</td>
      <td class="price">€${Number(row.cex_cash_profit_eur).toFixed(2)}<div class="hint">Max buy ${fmtEuro(row.cex_max_buy_eur)} · ${escapeHtml(row.match_confidence || "title_only")}</div></td>`;
    body.appendChild(tr);
  }
}

function renderCex(deals) {
  if (!deals || !deals.generated_at) return;
  const stats = deals.cex_stats || {};
  const lookups = stats.lookups ?? (deals.cex_lookups || []).length;
  const summary = document.getElementById("cex-summary");
  if (summary) {
    summary.textContent = cexStatusLine(deals);
  }
  const flips = deals.cex_flips || [];
  const table = document.getElementById("cex-table");
  const body = document.getElementById("cex-body");
  const empty = document.getElementById("cex-empty");
  if (!body) return;
  body.innerHTML = "";
  if (!flips.length) {
    if (table) table.hidden = true;
    if (empty) {
      empty.hidden = false;
      empty.textContent =
        lookups > 0
          ? "No on-sale CeX flips met your margin this crawl (see close matches below if any)."
          : "No confirmed CeX cash flips in this batch. See the full listing catalogue for cumulative progress.";
    }
  } else {
    if (empty) empty.hidden = true;
    if (table) table.hidden = false;
    for (const row of flips) {
      appendCexRow(body, row, true);
    }
  }
  const near = (deals.cex_near_margin || []).filter((r) => r.drop_tier != null);
  renderNearMargin("cex-near-body", "cex-near-table", near);
}

function appendCexRow(body, row, showDrop) {
  const tr = document.createElement("tr");
  const sellerEvidence = "Seller/checkout not checked — research lead";
  const matchEvidence = row.match_confidence
    ? ` · CeX: ${row.match_confidence.replaceAll("_", " ")}`
    : "";
  const dropCell = showDrop && row.drop_percent != null
    ? `<td>${escapeHtml(String(row.drop_percent))}%</td>`
    : "";
  tr.innerHTML = `
      <td>
        <a href="${escapeHtml(row.url)}" target="_blank" rel="noopener">${escapeHtml(row.title || row.asin)}</a>
        <div class="hint"><a href="${escapeHtml(row.cex_url)}" target="_blank" rel="noopener">CeX listing</a></div>
        <div class="hint">${escapeHtml(sellerEvidence + matchEvidence)}</div>
      </td>
      <td class="price">${fmtEuro(row.price_eur)}</td>
      <td class="price">${fmtEuro(row.cex_cash_eur)}</td>
      <td class="price">€${Number(row.cex_cash_profit_eur).toFixed(2)}</td>
      ${showDrop ? dropCell : `<td class="was">${fmtEuro(row.cex_retail_eur)}</td>`}`;
  body.appendChild(tr);
}

function renderCexArbitrage(deals) {
  if (!deals || !deals.generated_at) return;
  const rows = deals.cex_arbitrage || [];
  const summary = document.getElementById("cex-arb-summary");
  if (summary) {
    summary.textContent =
      "Every priced listing enters the CeX research queue. Seller and checkout price are NOT verified; all matches are research leads. " +
      cexStatusLine(deals);
  }
  const table = document.getElementById("cex-arb-table");
  const body = document.getElementById("cex-arb-body");
  const empty = document.getElementById("cex-arb-empty");
  if (!body) return;
  body.innerHTML = "";
  if (!rows.length) {
    if (table) table.hidden = true;
    if (empty) empty.hidden = false;
  } else {
    if (empty) empty.hidden = true;
    if (table) table.hidden = false;
    for (const row of rows) {
      appendCexRow(body, row, false);
    }
  }
  const near = (deals.cex_near_margin || []).filter((r) => r.drop_tier == null);
  renderNearMargin("cex-arb-near-body", "cex-arb-near-table", near);
}

function sectionAgeHours(iso) {
  if (!iso) return null;
  try {
    return (Date.now() - new Date(iso).getTime()) / (3600 * 1000);
  } catch {
    return null;
  }
}

function sectionFreshness(deals) {
  const sections = deals.sections || {};
  const parts = Object.entries(sections).map(([, s]) => {
    const name = s.section_name || s.section_id || "Section";
    const age = sectionAgeHours(s.generated_at);
    const stale = age != null && age > 2.5 ? " (stale)" : "";
    return `${name} ${fmtTime(s.generated_at)}${stale}`;
  });
  if (parts.length) return parts.join(" · ");
  if (deals.last_section_name && deals.last_section_at) {
    return `Last section: ${deals.last_section_name} · ${fmtTime(deals.last_section_at)}`;
  }
  return "";
}

function renderSectionProgress(deals) {
  const sections = deals.sections || {};
  const table = document.getElementById("section-table");
  const body = document.getElementById("section-body");
  const empty = document.getElementById("section-empty");
  if (!table || !body) return;
  body.innerHTML = "";
  const rows = Object.values(sections);
  table.hidden = !rows.length;
  if (empty) empty.hidden = !!rows.length;
  for (const section of rows) {
    const cex = section.cex_stats || {};
    const catalogue = section.catalogue_stats || {};
    const progress = catalogue.priced != null
      ? `${Number(catalogue.checked || 0)} / ${Number(catalogue.priced || 0)}`
      : "Not started";
    const pending = catalogue.priced != null
      ? `<div class="hint">${Number(catalogue.remaining || 0)} awaiting CeX research</div>` : "";
    const tr = document.createElement("tr");
    const status = section.scan_status || (section.blocked ? "blocked" : "completed");
    const finds = Number(section.new_alerts || 0) + Number(cex.cash_flips || 0) + Number(cex.arbitrage || 0);
    tr.innerHTML = `
      <td>${escapeHtml(section.section_name || section.section_id || "Section")}</td>
      <td>${fmtTime(section.generated_at)}</td>
      <td>${escapeHtml(status)}</td>
      <td>${Number(section.items_seen || 0).toLocaleString("en-IE")}</td>
      <td>Disabled</td>
      <td>${progress}${pending}</td>
      <td>${Number(cex.live_lookups || 0)} live / ${Number(cex.cache_hits || 0)} cached</td>
      <td>${finds} (${Number(section.new_alerts || 0)} price leads, ${Number(cex.cash_flips || 0) + Number(cex.arbitrage || 0)} CeX)</td>`;
    body.appendChild(tr);
  }
}

let catalogueRows = [];
let cataloguePage = 0;
const CATALOGUE_PAGE_SIZE = 50;

function renderCataloguePage() {
  const body = document.getElementById("catalogue-body");
  const table = document.getElementById("catalogue-table");
  const info = document.getElementById("catalogue-info");
  if (!body || !table) return;
  const query = (document.getElementById("catalogue-search")?.value || "").toLowerCase().trim();
  const section = document.getElementById("catalogue-section-filter")?.value || "";
  const status = document.getElementById("catalogue-status-filter")?.value || "";
  const activity = document.getElementById("catalogue-activity-filter")?.value || "active";
  const filtered = catalogueRows.filter((row) => {
    if (section && row.section_id !== section) return false;
    if (activity === "active" && row.active === false) return false;
    if (activity === "archived" && row.active !== false) return false;
    if (status === "matched" && !["matched", "matched_unverified", "matched_stale_quote"].includes(row.cex_status)) return false;
    if (status === "pending" && row.last_checked_at) return false;
    if (status === "no_match" && row.cex_status !== "no_confirmed_match") return false;
    const target = [row.title, row.asin, row.category, row.seller_name].join(" ").toLowerCase();
    return !query || target.includes(query);
  });
  const maxPage = Math.max(0, Math.ceil(filtered.length / CATALOGUE_PAGE_SIZE) - 1);
  cataloguePage = Math.min(cataloguePage, maxPage);
  const subset = filtered.slice(cataloguePage * CATALOGUE_PAGE_SIZE, (cataloguePage + 1) * CATALOGUE_PAGE_SIZE);
  body.innerHTML = "";
  for (const row of subset) {
    const tr = document.createElement("tr");
    const seller = row.active === false
      ? `Archived listing — last seen ${fmtTime(row.last_seen_at)}`
      : "Not checked (seller verification disabled)";
    let cex = String(row.cex_status || "not checked").replaceAll("_", " ");
    if (row.cex_status === "matched") {
      cex = `RESEARCH ONLY — estimated ${fmtEuro(row.cex_net_profit_eur)} net, CeX cash ${fmtEuro(row.cex_cash_eur)}; Amazon offer not checked`;
    } else if (row.cex_status === "matched_stale_quote") {
      cex = `RESEARCH ONLY — CeX quote too old for an alert. Last quote: ${fmtTime(row.cex_quote_checked_at)}`;
    } else if (row.cex_status === "matched_unverified") {
      cex = `RESEARCH ONLY — CeX cash quote: ${fmtEuro(row.cex_cash_eur)}; verify Amazon seller, condition and checkout.`;
    }
    if (row.cex_quote_checked_at) {
      cex += ` · CeX quoted ${fmtTime(row.cex_quote_checked_at)}`;
    }
    const cexLink = row.cex_url ? `<div class="hint"><a href="${escapeHtml(row.cex_url)}" target="_blank" rel="noopener">CeX item</a></div>` : "";
    tr.innerHTML = `
      <td><a href="${escapeHtml(row.url)}" rel="noopener" target="_blank">${escapeHtml(row.title || row.asin)}</a>
        <div class="hint">${escapeHtml(row.asin)}</div></td>
      <td>${escapeHtml(row.section_name || row.section_id || "")}</td>
      <td class="price">${fmtEuro(row.price_eur)}</td>
      <td>${escapeHtml(seller)}</td>
      <td>${escapeHtml(cex)}${cexLink}</td>
      <td>${fmtTime(row.last_checked_at)}</td>`;
    body.appendChild(tr);
  }
  table.hidden = !catalogueRows.length;
  const start = filtered.length ? cataloguePage * CATALOGUE_PAGE_SIZE + 1 : 0;
  const end = Math.min(filtered.length, (cataloguePage + 1) * CATALOGUE_PAGE_SIZE);
  if (info) info.textContent = `Showing ${start}–${end} of ${filtered.length.toLocaleString("en-IE")} listings. Product prices are observed, not guaranteed checkout offers.`;
  document.getElementById("catalogue-prev").disabled = cataloguePage === 0;
  document.getElementById("catalogue-next").disabled = cataloguePage >= maxPage;
}

async function loadCatalogue() {
  const info = document.getElementById("catalogue-info");
  if (info) info.textContent = "Loading complete listing inventory…";
  try {
    const res = await fetch(`catalogue.json?t=${Date.now()}`, {cache: "no-store"});
    if (!res.ok) throw new Error("No published catalogue yet");
    const data = await res.json();
    const sections = Object.entries(data.sections || {});
    const options = document.getElementById("catalogue-section-filter");
    options.innerHTML = '<option value="">All sections</option>';
    catalogueRows = [];
    for (const [id, section] of sections) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = section.section_name || id;
      options.appendChild(option);
      for (const row of section.items || []) {
        catalogueRows.push({ ...row, section_id: id, section_name: section.section_name || id });
      }
    }
    cataloguePage = 0;
    renderCataloguePage();
  } catch (err) {
    if (info) info.textContent = `Catalogue not available yet (${err.message}). It is generated by the next new section scan.`;
  }
}

document.getElementById("catalogue-load")?.addEventListener("click", loadCatalogue);
for (const id of ["catalogue-search", "catalogue-section-filter", "catalogue-status-filter", "catalogue-activity-filter"]) {
  document.getElementById(id)?.addEventListener("input", () => {
    cataloguePage = 0;
    renderCataloguePage();
  });
}
document.getElementById("catalogue-prev")?.addEventListener("click", () => {
  cataloguePage = Math.max(0, cataloguePage - 1);
  renderCataloguePage();
});
document.getElementById("catalogue-next")?.addEventListener("click", () => {
  cataloguePage++;
  renderCataloguePage();
});

function renderPremiumDrops(deals) {
  const rows = [...(deals.premium_drops || []), ...(deals.rapid_drops || [])].sort(
    (a, b) => (b.drop_percent || 0) - (a.drop_percent || 0)
  );
  const table = document.getElementById("premium-table");
  const body = document.getElementById("premium-body");
  const empty = document.getElementById("premium-empty");
  if (!body) return;
  body.innerHTML = "";
  if (!rows.length) {
    if (table) table.hidden = true;
    if (empty) empty.hidden = false;
    return;
  }
  if (empty) empty.hidden = true;
  if (table) table.hidden = false;
  for (const d of rows.slice(0, 25)) {
    const kind =
      d.drop_kind === "rapid_scan"
        ? "Rapid (since last scan)"
        : d.drop_kind === "premium_history"
          ? "Premium vs reference"
          : d.drop_kind || "Drop";
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.title || d.asin)}</a><div class="hint">${escapeHtml(d.category || "")}</div></td>
      <td class="price">${fmtEuro(d.price_eur)}</td>
      <td class="was">${fmtEuro(d.reference_eur || d.median_eur)}</td>
      <td>${escapeHtml(String(d.drop_percent))}%</td>
      <td>${escapeHtml(kind)}</td>`;
    body.appendChild(tr);
  }
}

function renderDeals(deals) {
  if (!deals || !deals.generated_at) return;
  const summary = document.getElementById("deals-summary");
  const fresh = sectionFreshness(deals);
  summary.textContent =
    `Bestsellers ${fmtTime(deals.generated_at)} · ${Number(deals.items_seen || 0).toLocaleString("en-IE")} item-slots tracked across sections · ${Number(deals.items_with_history || 0).toLocaleString("en-IE")} with enough history` +
    (deals.last_section_name ? ` · latest: ${deals.last_section_name}` : "") +
    (deals.blocked ? " · Amazon blocked part of a recent crawl" : "") +
    (fresh ? ` · ${fresh}` : "");

  const alerts = deals.alerts || [];
  const list = document.getElementById("deals-alerts");
  list.innerHTML = "";
  list.hidden = !alerts.length;
  for (const a of alerts) {
    const li = document.createElement("li");
    li.innerHTML = `
      <div><a href="${escapeHtml(a.url)}" target="_blank" rel="noopener">${escapeHtml(a.product_title || a.asin)}</a></div>
      <div class="price">${fmtEuro(a.price_eur)}</div>
      <div>${escapeHtml(a.reason || "")}</div>
      <div class="time">${fmtTime(a.created_at)} · ${a.notified_telegram ? "Telegram sent" : "not sent"}</div>`;
    list.appendChild(li);
  }

  const drops = deals.drops || [];
  const table = document.getElementById("deals-table");
  const body = document.getElementById("deals-body");
  body.innerHTML = "";
  table.hidden = !drops.length;
  for (const d of drops) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><a href="${escapeHtml(d.url)}" target="_blank" rel="noopener">${escapeHtml(d.title || d.asin)}</a><div class="hint">${escapeHtml(d.category || "")}</div></td>
      <td class="price">${fmtEuro(d.price_eur)}</td>
      <td class="was">${fmtEuro(d.median_eur)}</td>
      <td>${escapeHtml(String(d.drop_percent))}%</td>`;
    body.appendChild(tr);
  }
  renderSectionProgress(deals);
  renderPremiumDrops(deals);
  const overview = document.getElementById("catalogue-overview");
  const progress = deals.catalogue_stats || {};
  if (overview && progress.discovered != null) {
    overview.textContent =
      `Published Amazon listings: ${Number(progress.discovered || 0).toLocaleString("en-IE")} across scanned sections · ` +
      `${Number(progress.active ?? progress.discovered ?? 0).toLocaleString("en-IE")} currently listed, ` +
      `${Number(progress.checked || 0).toLocaleString("en-IE")} CeX-researched, ` +
      `${Number(progress.remaining || 0).toLocaleString("en-IE")} awaiting first check, ` +
      `${Number(progress.cex_attempted || 0).toLocaleString("en-IE")} CeX attempts, ` +
      `${Number(progress.cex_matched || 0).toLocaleString("en-IE")} confirmed CeX matches.`;
  }
  renderDeepDrops(deals);
  renderCex(deals);
  renderCexArbitrage(deals);
}

fetch(`deals.json?t=${Date.now()}`, { cache: "no-store" })
  .then((res) => (res.ok ? res.json() : null))
  .then((deals) => {
    if (deals) {
      lastDealsAt = deals.generated_at || "";
      renderDeals(deals);
    }
  })
  .catch(() => {});

function refreshIfChanged() {
  Promise.all([
    fetch(`data.json?t=${Date.now()}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null),
    fetch(`deals.json?t=${Date.now()}`, { cache: "no-store" })
      .then((res) => (res.ok ? res.json() : null))
      .catch(() => null),
  ]).then(([data, deals]) => {
    if (data && (data.generated_at || "") !== lastDataAt) {
      lastDataAt = data.generated_at || "";
      render(data);
    }
    if (deals && (deals.generated_at || "") !== lastDealsAt) {
      lastDealsAt = deals.generated_at || "";
      renderDeals(deals);
    }
  });
}

loadData()
  .then((data) => {
    lastDataAt = data.generated_at || "";
    render(data);
  })
  .catch((err) => {
    const status = document.getElementById("status");
    status.textContent = `Could not load tracker data (${err.message}).`;
    status.className = "status warn";
  });

setInterval(refreshIfChanged, LIVE_REFRESH_MS);
