async function fetchJson(url, options) {
  const res = await fetch(url, options);
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || res.statusText);
  }
  return res.json();
}

function fmtTime(iso) {
  try {
    return new Date(iso).toLocaleString();
  } catch {
    return iso;
  }
}

async function loadHealth() {
  const el = document.getElementById("status");
  const h = await fetchJson("/api/health");
  const parts = [`Polling every ${h.poll_interval_seconds}s`];
  if (h.telegram_configured) parts.push("Telegram: ready");
  else parts.push("Telegram: not configured");
  if (h.whatsapp_configured) parts.push("WhatsApp: ready");
  el.textContent = parts.join(" · ");
  el.className = "status " + (h.telegram_configured || h.whatsapp_configured ? "ok" : "warn");
}

async function loadProducts() {
  const products = await fetchJson("/api/products");
  const table = document.getElementById("products-table");
  const body = document.getElementById("products-body");
  const empty = document.getElementById("products-empty");
  body.innerHTML = "";
  if (!products.length) {
    table.hidden = true;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  table.hidden = false;
  for (const p of products) {
    const tr = document.createElement("tr");
    const title = p.title || p.url;
    tr.innerHTML = `
      <td><a href="${p.url}" target="_blank" rel="noopener">${escapeHtml(title)}</a></td>
      <td>${p.asin}</td>
      <td>${p.enabled ? "On" : "Off"}</td>
      <td>
        <button type="button" class="secondary" data-toggle="${p.id}">${p.enabled ? "Pause" : "Resume"}</button>
        <button type="button" class="secondary" data-delete="${p.id}">Remove</button>
      </td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll("[data-toggle]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-toggle");
      const row = products.find((x) => String(x.id) === id);
      await fetchJson(`/api/products/${id}`, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: !row.enabled }),
      });
      await loadProducts();
    });
  });
  body.querySelectorAll("[data-delete]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.getAttribute("data-delete");
      if (!confirm("Remove this product from the watch list?")) return;
      await fetchJson(`/api/products/${id}`, { method: "DELETE" });
      await loadProducts();
    });
  });
}

async function loadAlerts() {
  const alerts = await fetchJson("/api/alerts");
  const list = document.getElementById("alerts-list");
  const empty = document.getElementById("alerts-empty");
  list.innerHTML = "";
  if (!alerts.length) {
    list.hidden = true;
    empty.hidden = false;
    return;
  }
  empty.hidden = true;
  list.hidden = false;
  for (const a of alerts) {
    const li = document.createElement("li");
    const title = a.product_title || a.asin;
    li.innerHTML = `
      <div><a href="${a.url}" target="_blank" rel="noopener">${escapeHtml(title)}</a></div>
      <div class="price">€${Number(a.price_eur).toFixed(2)}${a.list_price_eur ? ` (was €${Number(a.list_price_eur).toFixed(2)})` : ""}</div>
      <div>${escapeHtml(a.reason)}</div>
      <div class="time">${fmtTime(a.created_at)} · Telegram ${a.notified_telegram ? "sent" : "pending"}</div>`;
    list.appendChild(li);
  }
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

document.getElementById("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("product-url");
  try {
    await fetchJson("/api/products", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url: input.value.trim() }),
    });
    input.value = "";
    await loadProducts();
  } catch (err) {
    alert(err.message);
  }
});

async function refresh() {
  await Promise.all([loadHealth(), loadProducts(), loadAlerts()]);
}

refresh();
setInterval(refresh, 15000);
