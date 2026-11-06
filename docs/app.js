const DUBLIN = "Europe/Dublin";
const LIVE_REFRESH_MS = 90_000;

let lastDataAt = "";
let lastDealsAt = "";
let lastDealsSnapshot = {};
let lastTargetSnapshot = {};

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

// Tab navigation is client-only; all scan data continues updating every 90s.
const TRACKER_TAB_IDS = ["overview", "amazon-drops", "cex", "listings", "watchlist"];

function activateDashboardTab(id, { updateHash = false, focus = false } = {}) {
  const selected = TRACKER_TAB_IDS.includes(id) ? id : "overview";
  for (const tabId of TRACKER_TAB_IDS) {
    const tab = document.getElementById("tab-" + tabId);
    const panel = document.getElementById("panel-" + tabId);
    if (!tab || !panel) continue;
    const active = tabId === selected;
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
    panel.hidden = !active;
    if (active && focus) tab.focus();
  }
  if (updateHash && window.location.hash !== "#" + selected) {
    window.location.hash = selected;
  }
}

function setupDashboardTabs() {
  for (const [index, id] of TRACKER_TAB_IDS.entries()) {
    const tab = document.getElementById("tab-" + id);
    if (!tab) continue;
    tab.addEventListener("click", () => activateDashboardTab(id, { updateHash: true }));
    tab.addEventListener("keydown", (event) => {
      let next = null;
      if (event.key === "ArrowRight") next = TRACKER_TAB_IDS[(index + 1) % TRACKER_TAB_IDS.length];
      if (event.key === "ArrowLeft") next = TRACKER_TAB_IDS[(index + TRACKER_TAB_IDS.length - 1) % TRACKER_TAB_IDS.length];
      if (event.key === "Home") next = TRACKER_TAB_IDS[0];
      if (event.key === "End") next = TRACKER_TAB_IDS[TRACKER_TAB_IDS.length - 1];
      if (next != null) {
        event.preventDefault();
        activateDashboardTab(next, { updateHash: true, focus: true });
      }
    });
  }
  const fromHash = () => activateDashboardTab(window.location.hash.slice(1));
  window.addEventListener("hashchange", fromHash);
  fromHash();
}

const GENERAL_DROPS_PER_PAGE = 40;
let generalDropRows = [];
let generalDropPage = 0;
let generalDropCoverage = { total: 0, updated: 0 };
let generalDropSort = { key: "savings_eur", dir: "desc" };

const GENERAL_DROP_SORT_PRESETS = {
  savings: { key: "savings_eur", dir: "desc" },
  percentage: { key: "drop_percent", dir: "desc" },
  recent: { key: "found_at", dir: "desc" },
};

function compareGeneralDropRows(a, b, key) {
  if (key === "title") return String(a.title || a.asin || "").localeCompare(String(b.title || b.asin || ""), "en", { sensitivity: "base" });
  if (key === "section") return String(a.section_name || a.section_id || "").localeCompare(String(b.section_name || b.section_id || ""), "en", { sensitivity: "base" });
  if (key === "found_at") return String(a.found_at || a.last_scanned_at || "").localeCompare(String(b.found_at || b.last_scanned_at || ""));
  return Number(a[key] || 0) - Number(b[key] || 0);
}

function sortGeneralDropSelection(selected, sortMode) {
  if (sortMode === "both") {
    selected.sort((a, b) => b.savings_eur - a.savings_eur || b.drop_percent - a.drop_percent || String(b.found_at || "").localeCompare(String(a.found_at || "")));
    return;
  }
  const preset = GENERAL_DROP_SORT_PRESETS[sortMode];
  if (preset) generalDropSort = { ...preset };
  const dir = generalDropSort.dir === "asc" ? 1 : -1;
  selected.sort((a, b) => {
    const cmp = compareGeneralDropRows(a, b, generalDropSort.key);
    return dir * cmp || String(a.asin || "").localeCompare(String(b.asin || ""));
  });
}

function updateGeneralDropSortHeaders() {
  document.querySelectorAll("#general-drops-table .th-sort").forEach((btn) => {
    const active = btn.dataset.sortKey === generalDropSort.key;
    btn.setAttribute("aria-sort", active ? (generalDropSort.dir === "asc" ? "ascending" : "descending") : "none");
  });
}

function extractGeneralDrops(deals) {
  // Each section publishes independently. Old sections can still display the
  // legacy >=30% drops until they complete a new >=10% scan.
  const sections = Object.entries(deals.sections || {});
  const rows = [];
  let updated = 0;
  if (sections.length) {
    for (const [sectionId, section] of sections) {
      const isNew = Array.isArray(section.general_drops);
      if (isNew) updated++;
      for (const drop of (isNew ? section.general_drops : section.drops) || []) {
        rows.push({
          ...drop, section_id: sectionId,
          section_name: section.section_name || sectionId,
          last_scanned_at: section.generated_at || deals.generated_at,
          legacy_only: !isNew,
        });
      }
    }
  } else {
    const modern = Array.isArray(deals.general_drops);
    for (const drop of (modern ? deals.general_drops : deals.drops) || []) {
      rows.push({
        ...drop, section_id: drop.section_id || "default",
        section_name: drop.section_name || "All sections",
        last_scanned_at: deals.generated_at,
        legacy_only: !modern,
      });
    }
    updated = modern ? 1 : 0;
  }

  const latest = new Map();
  for (const row of rows) {
    const reference = Number(row.reference_eur ?? row.median_eur);
    const price = Number(row.price_eur);
    const key = String(row.asin || "");
    if (!key || !Number.isFinite(price) || !Number.isFinite(reference) ||
        price <= 0 || reference <= price) continue;
    const pct = Math.round((100 * (reference - price) / reference) * 10) / 10;
    if (pct <= 0) continue;
    const normalized = {
      ...row,
      reference_eur: reference, price_eur: price,
      savings_eur: Math.round((reference - price) * 100) / 100,
      drop_percent: pct,
      found_at: row.found_at || row.last_scanned_at,
    };
    const previous = latest.get(key);
    if (!previous || String(normalized.last_scanned_at || "") > String(previous.last_scanned_at || "")) {
      latest.set(key, normalized);
    }
  }
  return { rows: [...latest.values()], coverage: { total: sections.length || 1, updated } };
}

function renderGeneralDropsPage() {
  const table = document.getElementById("general-drops-table");
  const body = document.getElementById("general-drops-body");
  const summary = document.getElementById("general-drops-summary");
  const empty = document.getElementById("general-drops-empty");
  const pagination = document.getElementById("general-drops-pagination");
  if (!table || !body || !summary) return;
  const min = Number(document.getElementById("drop-min")?.value || 10);
  const minPrice = Number(document.getElementById("drop-min-price")?.value || 0);
  const minSaving = Number(document.getElementById("drop-min-saving")?.value || 0);
  const section = document.getElementById("drop-section")?.value || "";
  const sort = document.getElementById("drop-sort")?.value || "savings";
  const q = (document.getElementById("drop-search")?.value || "").toLowerCase().trim();
  const brandsOnly = document.getElementById("drop-brands-only")?.checked !== false;
  const selected = generalDropRows.filter((r) => {
    if (brandsOnly && r.resale_eligible === false) return false;
    if (r.drop_percent < min || r.price_eur < minPrice || r.savings_eur < minSaving ||
        (section && r.section_id !== section)) return false;
    if (!q) return true;
    const hay = [r.title, r.asin, r.category, r.section_name, r.savings_eur, r.drop_percent]
      .map((v) => String(v ?? "")).join(" ").toLowerCase();
    if (hay.includes(q)) return true;
    const euroQuery = q.match(/€?\s*(\d+(?:\.\d+)?)/);
    if (euroQuery && Number(r.savings_eur) >= Number(euroQuery[1])) return true;
    const pctQuery = q.match(/(\d+(?:\.\d+)?)\s*%/);
    if (pctQuery && Number(r.drop_percent) >= Number(pctQuery[1])) return true;
    return false;
  });
  sortGeneralDropSelection(selected, sort);
  updateGeneralDropSortHeaders();
  const pages = Math.max(1, Math.ceil(selected.length / GENERAL_DROPS_PER_PAGE));
  generalDropPage = Math.max(0, Math.min(generalDropPage, pages - 1));
  const start = generalDropPage * GENERAL_DROPS_PER_PAGE;
  const visible = selected.slice(start, start + GENERAL_DROPS_PER_PAGE);
  body.innerHTML = "";
  for (const drop of visible) {
    const tr = document.createElement("tr");
    const stale = sectionAgeHours(drop.last_scanned_at);
    const staleText = stale != null && stale > 3 ? " · section overdue" : "";
    const url = /^https:\/\/(?:www\.)?amazon\.ie\//i.test(String(drop.url || ""))
      ? String(drop.url) : "https://www.amazon.ie/";
    tr.innerHTML = `
      <td><a href="${escapeHtml(url)}" target="_blank" rel="noopener">${escapeHtml(drop.title || drop.asin)}</a>
        <div class="hint">${escapeHtml(drop.asin)} · ${escapeHtml(drop.category || "")}</div></td>
      <td>${escapeHtml(drop.section_name || drop.section_id || "")}</td>
      <td class="was">${fmtEuro(drop.reference_eur)}</td>
      <td class="price">${fmtEuro(drop.price_eur)}</td>
      <td class="saving-eur">−${fmtEuro(drop.savings_eur)}</td>
      <td><span class="drop-percent">−${escapeHtml(drop.drop_percent.toFixed(1))}%</span></td>
      <td><span class="time">${fmtTime(drop.found_at)}</span>
        <div class="hint">Scanned ${fmtTime(drop.last_scanned_at)}${escapeHtml(staleText)}</div></td>`;
    body.appendChild(tr);
  }
  table.hidden = !selected.length;
  empty.hidden = Boolean(selected.length);
  if (!selected.length) {
    empty.textContent = generalDropRows.length
      ? "No Amazon price drops match those filters. Try another percentage, section or search."
      : "No observed discounts with established history yet. The next section scans will populate this view.";
  }
  const legacy = generalDropCoverage.updated < generalDropCoverage.total;
  const migration = legacy
    ? ` · ${generalDropCoverage.total - generalDropCoverage.updated} section(s) still publishing older ≥30% results`
    : "";
  summary.textContent =
    `${selected.length.toLocaleString("en-IE")} products with ≥${min}% observed reduction · ` +
    `showing ${selected.length ? start + 1 : 0}–${start + visible.length}${migration}. ` +
    "These figures are based on the tracker’s historical reference, not an advertised RRP.";
  const count = document.getElementById("general-drops-count");
  if (count) count.textContent = `${selected.length.toLocaleString("en-IE")} matches`;
  if (pagination) pagination.hidden = selected.length <= GENERAL_DROPS_PER_PAGE;
  const pageText = document.getElementById("general-drops-page");
  if (pageText) pageText.textContent = `Page ${generalDropPage + 1} of ${pages}`;
  document.getElementById("general-drops-prev").disabled = generalDropPage === 0;
  document.getElementById("general-drops-next").disabled = generalDropPage >= pages - 1;
}

function renderGeneralDrops(deals) {
  if (!deals || !deals.generated_at) return;
  const data = extractGeneralDrops(deals);
  generalDropRows = data.rows;
  generalDropCoverage = data.coverage;
  const filter = document.getElementById("drop-section");
  if (filter) {
    const previouslySelected = filter.value;
    const sections = Object.entries(deals.sections || {});
    filter.replaceChildren(new Option("All sections", ""));
    for (const [id, section] of sections) {
      filter.add(new Option(section.section_name || id, id));
    }
    if ([...filter.options].some((option) => option.value === previouslySelected)) {
      filter.value = previouslySelected;
    }
  }
  renderGeneralDropsPage();
}

function setupGeneralDropControls() {
  document.getElementById("general-drops-table")?.querySelector("thead")?.addEventListener("click", (event) => {
    const btn = event.target.closest(".th-sort");
    if (!btn?.dataset.sortKey) return;
    const key = btn.dataset.sortKey;
    const numeric = key !== "title" && key !== "section";
    if (generalDropSort.key === key) {
      generalDropSort.dir = generalDropSort.dir === "asc" ? "desc" : "asc";
    } else {
      generalDropSort = { key, dir: numeric ? "desc" : "asc" };
    }
    const select = document.getElementById("drop-sort");
    if (select) {
      const preset = Object.entries(GENERAL_DROP_SORT_PRESETS).find(
        ([, v]) => v.key === key && v.dir === generalDropSort.dir,
      );
      const columnOption = select.querySelector('option[value="column"]');
      if (preset) {
        select.value = preset[0];
        if (columnOption) columnOption.hidden = true;
      } else {
        if (columnOption) columnOption.hidden = false;
        select.value = "column";
      }
    }
    generalDropPage = 0;
    renderGeneralDropsPage();
  });
  for (const id of ["drop-min", "drop-min-price", "drop-min-saving", "drop-section", "drop-sort", "drop-search", "drop-brands-only"]) {
    document.getElementById(id)?.addEventListener("input", () => {
      if (id === "drop-sort") {
        const select = document.getElementById("drop-sort");
        const preset = GENERAL_DROP_SORT_PRESETS[select?.value];
        if (preset) {
          generalDropSort = { ...preset };
          select?.querySelector('option[value="column"]')?.setAttribute("hidden", "");
        }
      }
      generalDropPage = 0;
      renderGeneralDropsPage();
    });
  }
  document.getElementById("general-drops-prev")?.addEventListener("click", () => {
    generalDropPage = Math.max(0, generalDropPage - 1);
    renderGeneralDropsPage();
  });
  document.getElementById("general-drops-next")?.addEventListener("click", () => {
    generalDropPage++;
    renderGeneralDropsPage();
  });
}

setupDashboardTabs();
setupGeneralDropControls();

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

function renderRetailComparisons(products) {
  const root = document.getElementById("retail-comparison-results");
  if (!root) return;
  root.innerHTML = "";
  if (!products.length) {
    root.innerHTML = '<p class="empty">Add an Amazon product to the Watchlist first.</p>';
    return;
  }
  const statusLabels = {
    matched: "Exact model/barcode · in stock",
    stock_unknown: "Exact model/barcode · stock unconfirmed",
    out_of_stock: "Exact model/barcode · out of stock",
    price_unavailable: "Price not available", ambiguous_variants: "Multiple variants/prices",
    barcode_mismatch: "Wrong barcode — excluded", model_mismatch: "Wrong model — excluded",
    model_not_confirmed: "Model not confirmed — excluded",
    identity_required: "Model or barcode required",
    no_product_metadata: "Retailer does not expose usable product data",
    rate_limited: "Shop blocked automated request",
    fetch_error: "Shop could not be reached", http_error: "Page unavailable",
    pending_refresh: "Awaiting next comparison scan", unsafe_redirect: "Unsafe redirect rejected",
    page_too_large: "Page too large", too_many_redirects: "Too many redirects",
    not_html: "Not a product HTML page",
  };
  for (const product of products) {
    const cmp = product.retail_comparison || {};
    const offers = cmp.offers || [];
    const section = document.createElement("section");
    section.className = "retail-product";
    const amazonPrice = product.price_eur;
    const suppliedRrp = cmp.rrp_eur;
    const source = typeof cmp.rrp_source === "string" && /^https:\/\//i.test(cmp.rrp_source)
      ? ' · <a href="' + escapeHtml(cmp.rrp_source) + '" target="_blank" rel="noopener">RRP source</a>' : "";
    const savings = suppliedRrp != null && amazonPrice != null && Number(amazonPrice) < Number(suppliedRrp)
      ? Math.round((Number(suppliedRrp) - Number(amazonPrice)) * 100) / 100 : null;
    const discount = savings != null && Number(suppliedRrp) > 0
      ? (savings / Number(suppliedRrp) * 100).toFixed(1) : null;
    const amazonLink = /^https:\/\/www\.amazon\.ie\/dp\/[A-Z0-9]{10}$/i.test(product.url || "")
      ? product.url : "https://www.amazon.ie/";
    section.innerHTML = '<h3><a target="_blank" rel="noopener" href="' + escapeHtml(amazonLink) + '">' +
      escapeHtml(product.title || product.asin || "Watched product") + '</a></h3>' +
      '<p class="hint">ASIN: ' + escapeHtml(product.asin || "") +
      ' · Model: ' + escapeHtml(cmp.model || product.model_number || "not confirmed") +
      ' · EAN: ' + escapeHtml(cmp.barcode || "not available") + '</p>' +
      '<p><strong>Amazon observed: ' + fmtEuro(amazonPrice) + '</strong>' +
      ' · Lowest checked in-stock retailer: <strong>' +
      fmtEuro(cmp.lowest_checked_retailer_price_eur) + '</strong>' +
      (cmp.lowest_checked_retailer ? ' (' + escapeHtml(cmp.lowest_checked_retailer) + ')' : '') + '</p>' +
      '<p class="hint">Manufacturer RRP (user supplied): ' + fmtEuro(suppliedRrp) + source +
      (savings != null ? ' · Amazon vs supplied RRP: −' + fmtEuro(savings) +
        ' (−' + discount + '%)' : '') +
      ' · Amazon advertised “was”: ' + fmtEuro(product.list_price_eur) +
      ' (not treated as manufacturer RRP).</p>';
    const exactModel = cmp.model || product.model_number || "";
    if (exactModel) {
      const find = document.createElement("p");
      find.className = "hint";
      const search = "https://www.google.com/search?q=" +
        encodeURIComponent('"' + exactModel + '" site:.ie price');
      find.innerHTML = '<a target="_blank" rel="noopener" href="' +
        escapeHtml(search) + '">Find more Irish retailer product links for this exact model</a>' +
        ' · Copy specific shop URLs into your existing Watch issue.';
      section.appendChild(find);
    }
    const note = document.createElement("p");
    note.className = "hint retail-status-note";
    const delta = cmp.amazon_minus_lowest_retail_eur;
    if (delta != null && amazonPrice != null) {
      note.textContent = Number(delta) > 0
        ? "Amazon's observed item price is " + fmtEuro(delta) +
          " MORE than the lowest checked in-stock retailer price."
        : Number(delta) < 0
          ? "Amazon's observed item price is " + fmtEuro(-Number(delta)) +
            " LESS than the lowest checked in-stock retailer price."
          : "Amazon and the lowest checked retailer have the same observed item price.";
    } else if (offers.length) {
      note.textContent = "No exactly matched, in-stock competitor has a usable price yet.";
    } else {
      note.textContent = "Add Model: and Compare: retailer product URLs to the existing Watch issue to enable automatic cross-shop research.";
    }
    section.appendChild(note);
    if (offers.length) {
      const wrap = document.createElement("div");
      wrap.className = "table-wrap";
      const table = document.createElement("table");
      table.className = "retail-offer-table";
      table.innerHTML = "<thead><tr><th>Retailer</th><th>Observed item price</th>" +
                        "<th>Match / availability</th><th>Checked</th></tr></thead>";
      const tbody = document.createElement("tbody");
      for (const offer of offers) {
        const row = document.createElement("tr");
        const href = typeof offer.url === "string" && /^https:\/\//i.test(offer.url)
          ? offer.url : null;
        const label = escapeHtml(offer.retailer || "Retailer");
        row.innerHTML = '<td>' +
          (href ? '<a href="' + escapeHtml(href) + '" target="_blank" rel="noopener">' +
                  label + '</a>' : label) +
          '</td><td class="price">' + fmtEuro(offer.price_eur) +
          '</td><td>' + escapeHtml(statusLabels[offer.status] || offer.status || "Unknown") +
          '</td><td class="time">' + fmtTime(offer.checked_at) + '</td>';
        tbody.appendChild(row);
      }
      table.appendChild(tbody);
      wrap.appendChild(table);
      section.appendChild(wrap);
    }
    const warning = document.createElement("p");
    warning.className = "hint";
    warning.textContent = "Item prices only: delivery, vouchers, final checkout and stock may differ. These are sampled retailers, not all shops in Ireland.";
    section.appendChild(warning);
    root.appendChild(section);
  }
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

  renderRetailComparisons(products);

  // The site itself is static. This table makes the GitHub issue -> ASIN ->
  // watchlist workflow visible without requiring anyone to inspect Actions logs.
  const requests = (data.watch_submissions || []).slice().sort(
    (a, b) => Number(b.issue_number || 0) - Number(a.issue_number || 0)
  );
  const requestsTable = document.getElementById("watch-requests-table");
  const requestsBody = document.getElementById("watch-requests-body");
  const requestsEmpty = document.getElementById("watch-requests-empty");
  if (requestsTable && requestsBody && requestsEmpty) {
    requestsBody.innerHTML = "";
    requestsTable.hidden = !requests.length;
    requestsEmpty.hidden = Boolean(requests.length);
    const labels = {
      tracking: ["Tracking", "ok"],
      fetch_error: ["Amazon fetch failed — tracking continues", "fetch"],
      price_unavailable: ["Added — price unavailable", "fetch"],
      pending_scan: ["Accepted — awaiting scan", "fetch"],
      link_unresolved: ["Link not resolved — edit issue", "error"],
    };
    for (const request of requests) {
      const tr = document.createElement("tr");
      const number = Number(request.issue_number || 0);
      const safeNumber = Number.isSafeInteger(number) && number > 0 ? number : null;
      const issueLink = safeNumber
        ? `<a href="https://github.com/${encodeURI(repo)}/issues/${safeNumber}" target="_blank" rel="noopener">Issue #${safeNumber}</a>`
        : "GitHub request";
      const asin = String(request.asin || "");
      const productLink = /^[A-Z0-9]{10}$/i.test(asin)
        ? `<a href="https://www.amazon.ie/dp/${escapeHtml(asin)}" target="_blank" rel="noopener">${escapeHtml(asin)}</a>`
        : "Awaiting valid Amazon URL/ASIN";
      const [label, variant] = labels[request.status] || ["Awaiting scan", "fetch"];
      tr.innerHTML = `
        <td>${issueLink}</td>
        <td>${productLink}</td>
        <td><span class="badge ${variant}">${escapeHtml(label)}</span>
          ${request.status === "link_unresolved" ? '<div class="hint">Add a full Amazon.ie product link to the existing issue.</div>' : ""}</td>
        <td>${fmtTime(request.checked_at)}</td>`;
      requestsBody.appendChild(tr);
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
    const input = document.getElementById("product-url");
    const url = input.value.trim();
    if (!url) return;
    const looksLikeAsin = /^[A-Z0-9]{10}$/i.test(url);
    if (!looksLikeAsin) {
      let parsed;
      try { parsed = new URL(url); } catch { /* Invalid link */ }
      const host = parsed?.hostname.toLowerCase();
      const trusted = host === "amzn.eu" || host === "www.amzn.eu" ||
        host === "amzn.to" || host === "www.amzn.to" ||
        /^((www|m|smile)\.)?amazon\.(ie|co\.uk|com|de|fr|es|it|nl|be|pl|se)$/.test(host || "");
      if (!parsed || parsed.protocol !== "https:" || !trusted) {
        input.setCustomValidity("Enter an HTTPS Amazon product link, amzn.eu short link or 10-character ASIN.");
        input.reportValidity();
        return;
      }
    }
    input.setCustomValidity("");
    const model = (document.getElementById("watch-model")?.value || "").trim().toUpperCase();
    const barcode = (document.getElementById("watch-ean")?.value || "").trim();
    const rrp = (document.getElementById("watch-rrp")?.value || "").trim();
    const source = (document.getElementById("watch-rrp-source")?.value || "").trim();
    const links = (document.getElementById("watch-retailers")?.value || "")
      .split(/\r?\n/).map((x) => x.trim()).filter(Boolean);
    if (model && !/^[A-Z0-9][A-Z0-9._-]{3,55}$/.test(model)) {
      alert("Model/MPN must be a code of 4–56 letters or digits.");
      return;
    }
    if (barcode && !/^\d{8,14}$/.test(barcode)) {
      alert("EAN must be 8–14 digits.");
      return;
    }
    if (rrp && !(Number(rrp) > 0)) {
      alert("RRP must be a positive euro amount.");
      return;
    }
    if (source && !/^https:\/\//i.test(source)) {
      alert("Use an HTTPS link for the RRP source.");
      return;
    }
    const approved = new Set([
      "currys.ie", "smythstoys.com", "harveynorman.ie", "did.ie", "electrocity.ie",
      "petermurphyelectrical.ie", "euronics.ie", "expert.ie", "joyces.ie",
      "arnotts.ie", "keanscm.ie", "irwinelectrical.ie",
    ]);
    if (links.length > 8) {
      alert("Please add at most 8 direct retailer product links per item.");
      return;
    }
    for (const link of links) {
      let parsed;
      try { parsed = new URL(link); } catch { /* Invalid input */ }
      const host = (parsed?.hostname || "").toLowerCase().replace(/^www\./, "");
      if (!parsed || parsed.protocol !== "https:" || parsed.username ||
          parsed.password || !approved.has(host)) {
        alert("Only direct HTTPS product pages from supported Irish shops are accepted.");
        return;
      }
    }
    const body = [url];
    if (model) body.push("Model: " + model);
    if (barcode) body.push("EAN: " + barcode);
    if (rrp) body.push("RRP: " + Number(rrp).toFixed(2));
    if (source) body.push("RRP source: " + source);
    for (const link of links) body.push("Compare: " + link);
    const issueUrl = "https://github.com/" + repo + "/issues/new" +
      "?labels=watch&title=" + encodeURIComponent("Watch: " + url) +
      "&body=" + encodeURIComponent(body.join("\n"));
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
      "No CeX data in this snapshot yet — the next bestseller section queues after the previous section publishes (usually several minutes per section)."
    );
  }
  if (!lookups) {
    return (
      `CeX was queued for ${stats.deep_candidates || 0} sale items and ${stats.any_price_candidates || 0} regular-price items, but no CeX match returned a price (API or title match).`
    );
  }
  return `CeX checked ${attempted} records (cached or live); ${lookups} provisional CeX matches · margin ≥€${minEur} or ≥${minPct}% · ${stats.cash_flips || 0} sale leads · ${stats.arbitrage || 0} other leads. Seller/checkout not verified.`;
}

function cexTargetView(data, now = Date.now()) {
  const stats = data.funnel || {};
  const labels = {
    profitable: "Meets cash margin", above_buy_ceiling: "Waiting for a lower price",
    identity_mismatch: "Variant or identifier not confirmed", price_not_read: "Price not read",
    below_minimum_buy: "Below configured buy-price minimum",
  };
  const rows = (data.products || []).filter((row) => row.scan_lane === "cex_target")
    .sort((a, b) => String(b.checked_at || "").localeCompare(String(a.checked_at || "")))
    .slice(0, 24).map((row) => {
      const age = now - Date.parse(row.checked_at);
      const old = !Number.isFinite(age) || age < 0 || age > 30 * 60 * 1000;
      return {...row, result_label: old ? "Awaiting a fresh check" : (labels[row.status] || "Research only")};
    });
  return {rows,
    summary: `${Number(stats.catalogue_skus || 0)} CeX catalogue SKUs · ${Number(stats.saved_mappings ?? stats.exact_mappings ?? 0)} saved Amazon links · ${Number(stats.alert_ready_mappings ?? 0)} alert-ready · ${Number(stats.fresh_quotes || 0)} fresh CeX quotes`,
    diagnostics: `CeX SKUs are trade-in candidates only until an Amazon ASIN is linked with model/EAN evidence. Mapping is gradual (about one Amazon search per priority scan, six identity checks per section, twelve hotlist pages every ten minutes). Last fast scan: ${Number(stats.amazon_attempted || 0)} pages, ${Number(stats.price_read || 0)} prices read, ${Number(stats.identity_mismatch || 0)} identity failures, ${Number(stats.profitable || 0)} qualifying margins. ${Number(stats.stale_quotes || 0)} quotes need refresh.`,
  };
}

function renderCexTargets(data) {
  const summary = document.getElementById("cex-target-summary");
  const diagnostics = document.getElementById("cex-target-diagnostics");
  const body = document.getElementById("cex-target-body");
  const table = document.getElementById("cex-target-table");
  if (!summary || !body || !data.funnel) return;
  lastTargetSnapshot = data;
  renderOpportunityRadar(lastDealsSnapshot);
  const view = cexTargetView(data);
  summary.textContent = view.summary;
  diagnostics.textContent = view.diagnostics + (data.blocked_until ? ` Provider cooldown until ${fmtTime(data.blocked_until)}.` : "");
  body.innerHTML = "";
  table.hidden = !view.rows.length;
  for (const row of view.rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><a href="https://www.amazon.ie/dp/${encodeURIComponent(row.asin)}" target="_blank" rel="noopener">${escapeHtml(row.title || row.asin)}</a><div class="hint">${escapeHtml(row.cex_title || "")}</div></td>
      <td class="price">${fmtEuro(row.price_eur)}</td><td class="price">${fmtEuro(row.cex_max_buy_eur)}</td>
      <td class="price">${fmtEuro(row.cex_cash_profit_eur)}</td><td class="time">${fmtTime(row.checked_at)}<div class="hint">Quote ${fmtTime(row.cex_quote_checked_at)}</div></td>
      <td>${escapeHtml(row.result_label)}${row.found_at ? `<div class="hint">Found ${fmtTime(row.found_at)}</div>` : ""}</td>`;
    body.appendChild(tr);
  }
}

function loadCexTargets() {
  return fetch(`hotlist.json?t=${Date.now()}`, {cache: "no-store"})
    .then((res) => res.ok ? res.json() : null)
    .then((data) => { if (data) renderCexTargets(data); })
    .catch(() => {});
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
      <td>${progress}${pending}</td>
      <td>${Number(cex.live_lookups || 0)} live / ${Number(cex.cache_hits || 0)} cached</td>
      <td>${Number((section.general_drops || section.drops || []).length)} <span class="hint">${section.general_drops ? "≥10%" : "legacy ≥30%"}</span></td>
      <td>${finds} (${Number(section.new_alerts || 0)} error leads, ${Number(cex.cash_flips || 0) + Number(cex.arbitrage || 0)} CeX)</td>`;
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

function selectRadarLeads(deals, now = Date.now()) {
  const config = deals.alert_policy || {};
  const threshold = (key, fallback) => {
    const value = Number(config[key]);
    return config[key] != null && Number.isFinite(value) ? value : fallback;
  };
  const drops = (deals.general_drops || deals.drops || []).filter(row => {
    if (row.resale_eligible === false) return false;
    const nowPrice = Number(row.price_eur);
    const reference = Number(row.reference_eur ?? row.median_eur);
    const saved = reference - nowPrice;
    const advertised = row.drop_kind === "list_price";
    const percent = 100 * saved / reference;
    const severeHistoryError = !advertised && reference >= Math.max(500, threshold("min_reference_eur",250)) &&
      saved >= Math.max(250, threshold("min_saving_eur",120)) &&
      percent >= Math.max(75, threshold("min_history_drop_percent",50));
    const observed = Date.parse(row.last_scanned_at || row.observed_at || deals.last_section_at || deals.generated_at);
    return Number.isFinite(nowPrice) && nowPrice > 0 && Number.isFinite(reference) && reference > nowPrice &&
      (nowPrice >= threshold("min_amazon_price_eur",120) || severeHistoryError) &&
      reference >= threshold("min_reference_eur",250) &&
      saved >= (advertised ? threshold("min_list_saving_eur",250) : threshold("min_saving_eur",120)) &&
      percent >= (advertised ? threshold("min_list_drop_percent",75) : threshold("min_history_drop_percent",50)) &&
      Number.isFinite(observed) && now >= observed && now-observed <= 12*3600000;
  }).map(row => ({...row, radar_type:"drop", radar_value:Number(row.reference_eur ?? row.median_eur)-Number(row.price_eur)}));
  const cex = [...(deals.cex_flips || []),...(deals.cex_arbitrage || [])].filter(row => {
    if (row.resale_eligible === false) return false;
    const observed=Date.parse(row.observed_at || row.last_scanned_at || deals.last_section_at || deals.generated_at);
    return Number(row.price_eur) >= threshold("min_cex_buy_price_eur",0.01) &&
      Number(row.cex_cash_profit_eur) >= threshold("min_cex_cash_profit_eur",5) &&
      Number(row.cex_net_roi_percent) >= threshold("min_cex_roi_percent",0) &&
      ["barcode_verified","model_verified"].includes(row.match_confidence) &&
      row.cex_quote_fresh_for_alert === true && row.cex_cash_flip !== false &&
      Number.isFinite(observed) && now >= observed && now-observed <= 12*3600000;
  }).map(row => ({...row, radar_type:"cex",radar_value:Number(row.cex_cash_profit_eur)}));
  drops.sort((a,b)=>b.radar_value-a.radar_value);
  cex.sort((a,b)=>b.radar_value-a.radar_value);
  const byAsin = new Map();
  for(const row of [...cex,...drops]) if(row.asin && !byAsin.has(row.asin)) byAsin.set(row.asin,row);
  return {drops,cex,items:[...byAsin.values()].slice(0,6)};
}
function mergeFastCex(deals, hotlist, now = Date.now()) {
  const checked = (hotlist.products || []).filter(row => row.scan_lane === "cex_target" &&
    Number.isFinite(Date.parse(row.checked_at)) && now >= Date.parse(row.checked_at) &&
    now - Date.parse(row.checked_at) <= 30 * 60000);
  const latest = new Map(checked.map(row => [row.asin, row]));
  const retain = row => !latest.has(row.asin) || Date.parse(latest.get(row.asin).checked_at) <
    Date.parse(row.observed_at || row.last_scanned_at || deals.generated_at);
  const profitable = checked.filter(row => row.status === "profitable" &&
    row.cex_quote_fresh_for_alert === true && Number.isFinite(Date.parse(row.cex_quote_checked_at)) &&
    now >= Date.parse(row.cex_quote_checked_at) && now - Date.parse(row.cex_quote_checked_at) <= 12 * 3600000)
    .map(row => ({...row, observed_at: row.checked_at}));
  return {...deals, cex_flips: (deals.cex_flips || []).filter(retain),
    cex_arbitrage: [...profitable, ...(deals.cex_arbitrage || []).filter(retain)]};
}

function renderOpportunityRadar(deals) {
  const root = document.getElementById("radar-cards");
  if (!root) return;
  const result = selectRadarLeads(mergeFastCex(deals, lastTargetSnapshot));
  const p = deals.alert_policy || {};
  document.getElementById("radar-drop-count").textContent = String(result.drops.length);
  document.getElementById("radar-cex-count").textContent = String(result.cex.length);
  document.getElementById("radar-last-published").textContent = fmtTime(deals.generated_at);
  document.getElementById("radar-policy").textContent =
    "Telegram: large historical drops (from " + fmtEuro(p.min_amazon_price_eur ?? 120) +
    " Amazon price, " + fmtEuro(p.min_saving_eur ?? 120) + " saved, " +
    (p.min_history_drop_percent ?? 50) + "% off) or exact-model CeX margins of at least " +
    fmtEuro(p.min_cex_cash_profit_eur ?? 5) +
    ((p.min_cex_roi_percent ?? 0) > 0 ? " and " + p.min_cex_roi_percent + "% ROI" : " net profit") +
    ". Always confirm seller, variant and checkout.";
  root.replaceChildren();
  const make = (tag, label, cls) => {
    const node = document.createElement(tag);
    if (label != null) node.textContent = label;
    if (cls) node.className = cls;
    return node;
  };
  if (!result.items.length) {
    const box = make("div", null, "radar-empty");
    box.appendChild(make("strong", "No major opportunities qualify right now."));
    box.appendChild(make("p", "The tracker still scans everything. Smaller price drops and title-only CeX matches remain in the other tabs.", "hint"));
    root.appendChild(box);
    return;
  }
  for (const row of result.items) {
    const card = make("article", null, "radar-item");
    const head = make("div", null, "radar-item-head");
    head.appendChild(make("span", row.radar_type === "cex" ? "MATCHED CeX CASH" : "LARGE AMAZON DROP", "eyebrow"));
    head.appendChild(make("span", "Unverified", "badge fetch"));
    card.appendChild(head);
    const h = make("h3"), link = make("a", row.title || row.asin || "Product");
    link.href = /^[A-Z0-9]{10}$/i.test(String(row.asin || "")) ?
      "https://www.amazon.ie/dp/" + row.asin : "https://www.amazon.ie/";
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    h.appendChild(link);
    card.appendChild(h);
    const price = make("div", null, "radar-item-price");
    price.appendChild(make("strong", fmtEuro(row.radar_value)));
    price.appendChild(make("span", row.radar_type === "cex" ?
      "estimated net cash margin" : "saved vs Amazon price history"));
    card.appendChild(price);
    const details = row.radar_type === "cex" ?
      "Amazon " + fmtEuro(row.price_eur) + " · " + Number(row.cex_net_roi_percent).toFixed(0) +
      "% ROI · exact " + row.match_confidence.replaceAll("_", " ") :
      "Amazon " + fmtEuro(row.price_eur) + " · usual " + fmtEuro(row.reference_eur ?? row.median_eur) +
      " · " + Number(row.drop_percent).toFixed(0) + "% off";
    card.appendChild(make("p", details, "hint"));
    card.appendChild(make("p", "Seller, condition and checkout not checked. Open listing to verify.", "hint"));
    root.appendChild(card);
  }
}

function researchLogRows(deals, includeUnrecognised = false) {
  return (deals.drops || []).filter(row => includeUnrecognised || row.resale_eligible !== false)
    .map(row => ({...row, found_at: Number.isFinite(Date.parse(row.found_at)) ? row.found_at : null}));
}

function renderDeals(deals) {
  if (!deals || !deals.generated_at) return;
  lastDealsSnapshot = deals;
  const summary = document.getElementById("deals-summary");
  const fresh = sectionFreshness(deals);
  summary.textContent =
    `Bestsellers ${fmtTime(deals.generated_at)} · ${Number(deals.items_seen || 0).toLocaleString("en-IE")} item-slots tracked across sections · ${Number(deals.items_with_history || 0).toLocaleString("en-IE")} with enough history` +
    (deals.last_section_name ? ` · latest: ${deals.last_section_name}` : "") +
    (deals.blocked ? " · Amazon blocked part of a recent crawl" : "") +
    (fresh ? ` · ${fresh}` : "");

  const includeControl = document.getElementById("research-include-unrecognised");
  const includeUnrecognised = includeControl?.checked || false;
  if (includeControl) includeControl.onchange = () => renderDeals(deals);
  const alerts = (deals.alerts || []).filter(row => includeUnrecognised || row.resale_eligible !== false);
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

  const drops = researchLogRows(deals, includeUnrecognised);
  document.getElementById("research-log-empty").hidden = Boolean(drops.length || alerts.length);
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
      <td>${escapeHtml(String(d.drop_percent))}%</td>
      <td class="time">${d.found_at ? fmtTime(d.found_at) : "Unknown"}</td>`;
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
  renderGeneralDrops(deals);
  renderOpportunityRadar(deals);
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
  loadCexTargets();
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
loadCexTargets();
