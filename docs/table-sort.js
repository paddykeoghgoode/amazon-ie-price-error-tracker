(function (global) {
  const sorts = new Map();

  function header(label, key) {
    const safe = String(key).replace(/"/g, "&quot;");
    return `<th scope="col"><button type="button" class="th-sort" data-sort-key="${safe}">${label}</button></th>`;
  }

  function getSort(tableId, fallback) {
    return sorts.get(tableId) || fallback;
  }

  function setSort(tableId, sort) {
    sorts.set(tableId, sort);
  }

  function compareValues(a, b, kind) {
    if (kind === "number") {
      const na = Number(a);
      const nb = Number(b);
      const aBad = !Number.isFinite(na);
      const bBad = !Number.isFinite(nb);
      if (aBad && bBad) return 0;
      if (aBad) return 1;
      if (bBad) return -1;
      return na - nb;
    }
    if (kind === "date") {
      return String(a || "").localeCompare(String(b || ""));
    }
    return String(a ?? "").localeCompare(String(b ?? ""), "en", { sensitivity: "base" });
  }

  function sortRows(rows, sort, schema) {
    if (!rows?.length || !sort?.key || !schema[sort.key]) return rows || [];
    const col = schema[sort.key];
    const dir = sort.dir === "asc" ? 1 : -1;
    return [...rows].sort((left, right) => {
      const cmp = compareValues(col.get(left), col.get(right), col.kind || "text");
      if (cmp) return dir * cmp;
      const tie = String(left.asin || left.boxId || left.section_id || left.issue_number || "")
        .localeCompare(String(right.asin || right.boxId || right.section_id || right.issue_number || ""));
      return dir * tie;
    });
  }

  function updateHeaders(tableId, sort) {
    const table = typeof tableId === "string" ? document.getElementById(tableId) : tableId;
    if (!table || !sort) return;
    table.querySelectorAll(".th-sort").forEach((btn) => {
      const active = btn.dataset.sortKey === sort.key;
      btn.setAttribute(
        "aria-sort",
        active ? (sort.dir === "asc" ? "ascending" : "descending") : "none",
      );
    });
  }

  function bind(tableId, schema, rerun, initialSort) {
    const table = document.getElementById(tableId);
    if (!table) return;
    const keys = Object.keys(schema);
    if (!sorts.has(tableId)) {
      sorts.set(tableId, initialSort || { key: keys[0], dir: schema[keys[0]]?.kind === "text" ? "asc" : "desc" });
    }
    if (table.dataset.sortBound === "1") return;
    table.dataset.sortBound = "1";
    table.querySelector("thead")?.addEventListener("click", (event) => {
      const btn = event.target.closest(".th-sort");
      if (!btn?.dataset.sortKey || !schema[btn.dataset.sortKey]) return;
      const key = btn.dataset.sortKey;
      const kind = schema[key].kind || "text";
      let sort = sorts.get(tableId) || { key, dir: "desc" };
      if (sort.key === key) sort = { key, dir: sort.dir === "asc" ? "desc" : "asc" };
      else sort = { key, dir: kind === "text" ? "asc" : "desc" };
      sorts.set(tableId, sort);
      updateHeaders(tableId, sort);
      rerun(sort);
    });
  }

  function bindElement(table, tableKey, schema, rerun, initialSort) {
    if (!table) return;
    if (!table.id) table.id = tableKey;
    bind(table.id, schema, rerun, initialSort);
  }

  global.TableSort = { header, getSort, setSort, sortRows, updateHeaders, bind, bindElement };
})(window);
