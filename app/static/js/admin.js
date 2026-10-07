// Embedded merchant dashboard. Every request carries a fresh App Bridge session token; nothing is
// cached in the browser. All server data is rendered with textContent (never innerHTML).
(function () {
  "use strict";

  const view = document.getElementById("view");

  async function api(path) {
    const token = await window.shopify.idToken();
    const res = await fetch(path, { headers: { Authorization: "Bearer " + token } });
    if (!res.ok) throw new Error(res.status === 401 ? "Your admin session expired. Reload the app." : "Request failed (" + res.status + ").");
    return res.json();
  }

  function el(tag, attrs, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === "class") node.className = v;
      else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) node.setAttribute(k, v);
    }
    for (const c of children.flat(Infinity)) {
      if (c === null || c === undefined || c === false) continue;
      node.append(c instanceof Node ? c : document.createTextNode(String(c)));
    }
    return node;
  }

  const fmtDate = (iso) => (iso ? new Date(iso).toLocaleString() : "—");
  const list = (v) => (Array.isArray(v) && v.length ? v.join(", ") : "—");
  const text = (v) => (v === null || v === undefined || v === "" ? "—" : String(v));

  function badge(label, tone) {
    return el("span", { class: "badge " + (tone || "") }, label);
  }

  function statusBadge(r) {
    const tones = { confirmed: "ok", pending: "warn", expired: "bad" };
    const build = { saved: "ok", creating: "bad", pending_review: "bad", draft: "" };
    return [badge(r.status, tones[r.status]), badge("build: " + r.buildStatus, build[r.buildStatus])];
  }

  function setState(node) {
    view.replaceChildren(node);
  }

  function loading(label) {
    setState(el("p", { class: "muted" }, label || "Loading…"));
  }

  function failure(err, retry) {
    setState(el("div", { class: "card error" }, el("p", {}, err.message || "Something went wrong."), retry ? el("button", { onclick: retry }, "Retry") : null));
  }

  function pager(page, pageSize, total, go) {
    const pages = Math.max(1, Math.ceil(total / pageSize));
    return el("div", { class: "pager" },
      el("button", { disabled: page <= 1 ? "" : null, onclick: () => go(page - 1) }, "Previous"),
      el("span", {}, `Page ${page} of ${pages} (${total})`),
      el("button", { disabled: page >= pages ? "" : null, onclick: () => go(page + 1) }, "Next"));
  }

  // ---------------------------------------------------------------- customers
  async function customers(q, page) {
    loading();
    let data;
    try {
      data = await api(`/admin/api/customers?page=${page}&pageSize=25` + (q ? "&q=" + encodeURIComponent(q) : ""));
    } catch (e) {
      return failure(e, () => customers(q, page));
    }
    const input = el("input", { type: "search", placeholder: "Search by name or email", value: q || "", "aria-label": "Search customers" });
    const form = el("form", { class: "search", onsubmit: (ev) => { ev.preventDefault(); customers(input.value.trim(), 1); } }, input, el("button", { type: "submit" }, "Search"));
    const rows = data.customers.map((c) => el("tr", {},
      el("td", {}, el("a", { href: "#/customers/" + encodeURIComponent(c.conversationId) }, c.name || "(no name)")),
      el("td", {}, text(c.email)), el("td", {}, text(c.location)), el("td", {}, list(c.likes)), el("td", {}, text(c.preferredStyle)),
      el("td", {}, text(c.occasion)), el("td", { class: "num" }, c.recommendationCount), el("td", { class: "num" }, c.buildCount), el("td", {}, fmtDate(c.lastActivity))));
    setState(el("section", {},
      el("h1", {}, "Customers"), form,
      data.customers.length
        ? el("table", {}, el("thead", {}, el("tr", {}, ["Name", "Email", "Location", "Likes", "Style", "Occasion", "Recs", "Builds", "Last active"].map((h) => el("th", {}, h)))), el("tbody", {}, rows))
        : el("p", { class: "muted" }, q ? "No customers match that search." : "No customer profiles yet."),
      pager(data.page, data.pageSize, data.total, (p) => customers(q, p))));
  }

  // ---------------------------------------------------------------- customer detail
  function inventoryBlock(inv) {
    if (!inv) return el("p", { class: "muted" }, "No inventory snapshot was stored for this combination.");
    const state = !inv.inventoryValidated ? badge("NOT VALIDATED", "warn") : inv.buildable ? badge("BUILDABLE", "ok") : badge("NOT BUILDABLE", "bad");
    return el("div", { class: "inventory" },
      el("p", {}, state, ` lookup: ${inv.requestStatus} · checked ${fmtDate(inv.checkedAt)} (stored snapshot, not live)`),
      el("table", {}, el("thead", {}, el("tr", {}, ["Component", "Oil SKU", "Ratio", "Required ml", "Stock reported", "Mapping", "Status"].map((h) => el("th", {}, h)))),
        el("tbody", {}, inv.components.map((c) => el("tr", {},
          el("td", {}, c.productTitle), el("td", {}, text(c.odooSku)), el("td", { class: "num" }, Math.round(c.ratioPercent) + "%"),
          el("td", { class: "num" }, c.requiredOilMl), el("td", { class: "num" }, text(c.onHandQty)), el("td", {}, c.mappingStatus),
          el("td", {}, c.sufficient === true ? "Enough" : c.sufficient === false ? "Insufficient" : "Unknown"))))),
      el("p", { class: "muted" }, `Oil ${inv.oilTotalMl} ml · alcohol ${inv.alcoholMl} ml · max bottles ${text(inv.maxBuildableBottles)} · limiting SKU ${text(inv.limitingSku)}`));
  }

  function confidenceBlock(r) {
    const dims = r.confidenceBreakdown && typeof r.confidenceBreakdown === "object" ? Object.entries(r.confidenceBreakdown) : [];
    const risks = Array.isArray(r.riskBreakdown) ? r.riskBreakdown : [];
    return el("details", {}, el("summary", {}, "Confidence " + text(r.confidence) + " · risk penalty " + text(r.riskPenalty)),
      dims.length ? el("table", {}, el("tbody", {}, dims.map(([k, v]) => el("tr", {}, el("th", {}, k), el("td", {}, typeof v === "object" ? JSON.stringify(v) : String(v)))))) : null,
      risks.length ? el("ul", {}, risks.map((x) => el("li", {}, typeof x === "object" ? JSON.stringify(x) : String(x)))) : null,
      el("p", {}, "Exact notes — matched: " + list(r.exactNotes.matched) + " · missing: " + list(r.exactNotes.missing) + " · coverage " + text(r.exactNotes.coverageScore)));
  }

  function recommendationCard(r) {
    const ratios = Array.isArray(r.ratios) ? r.ratios.map((x) => `${x.productTitle} ${Math.round(x.ratioPercent)}%`).join(" / ") : "—";
    const components = Array.isArray(r.products) ? r.products.map((p) => `${p.title} (${p.contribution || "component"})`).join("; ") : "—";
    const review = r.needsReview
      ? el("p", { class: "warn-text" }, r.buildStatus === "creating"
        ? "Creation started but was never confirmed. Not retried automatically — check Shopify before acting (see Help → Builds that need review)."
        : "Outcome unknown or a later step failed. Not retried automatically — check Shopify before acting (see Help → Builds that need review).")
      : null;
    return el("article", { class: "card" },
      el("h3", {}, r.name || "(unnamed)", " ", badge(r.combinationType), " ", statusBadge(r)),
      el("p", { class: "muted" }, "Created " + fmtDate(r.createdAt) + (r.confirmedAt ? " · confirmed " + fmtDate(r.confirmedAt) : "") + " · id " + r.id),
      review,
      el("p", {}, el("b", {}, "Ratios: "), ratios),
      r.draftRatios ? el("p", {}, el("b", {}, "Customer's adjusted ratios: "), JSON.stringify(r.draftRatios)) : null,
      el("p", {}, el("b", {}, "Real components (internal): "), components),
      r.customerFacing && r.customerFacing.customerFacingWhySuits ? el("p", {}, el("b", {}, "Why it suits: "), r.customerFacing.customerFacingWhySuits) : null,
      r.shopifyAdminUrl ? el("p", {}, el("a", { href: r.shopifyAdminUrl, target: "_top" }, "Open Shopify product")) : null,
      confidenceBlock(r),
      el("h4", {}, "Inventory at recommendation time"), inventoryBlock(r.inventory));
  }

  async function customerDetail(id, msgPage, recPage) {
    loading();
    let c, msgs, recs;
    try {
      const base = "/admin/api/customers/" + encodeURIComponent(id);
      [c, msgs, recs] = await Promise.all([api(base), api(`${base}/messages?page=${msgPage}`), api(`${base}/recommendations?page=${recPage}`)]);
    } catch (e) {
      return failure(e, () => customerDetail(id, msgPage, recPage));
    }
    const p = c.profile;
    setState(el("section", {},
      el("p", {}, el("a", { href: "#/customers" }, "← Customers")),
      el("h1", {}, c.name || "(no name)"),
      el("div", { class: "card" }, el("dl", {},
        [["Email", c.email], ["Location", [p.city, p.stateRegion, p.country].filter(Boolean).join(", ")], ["Likes", list(p.likes)], ["Dislikes", list(p.dislikes)],
          ["Style", p.preferredStyle || p.inferredStyle], ["Occasion", p.occasion || p.giftRecipient], ["Strength", p.strengthPreference],
          ["Season / weather", [p.requestedSeasonStyle, p.weatherDirection].filter(Boolean).join(" / ")], ["Last active", fmtDate(c.lastActivity)],
          ["Recommendations / builds", `${c.recommendationCount} / ${c.buildCount}`]].map(([k, v]) => [el("dt", {}, k), el("dd", {}, text(v))]))),
      el("h2", {}, `Combinations (${recs.total})`),
      recs.recommendations.length ? recs.recommendations.map(recommendationCard) : el("p", { class: "muted" }, "No combinations generated yet."),
      recs.total > recs.pageSize ? pager(recs.page, recs.pageSize, recs.total, (n) => customerDetail(id, msgPage, n)) : null,
      el("h2", {}, `Conversation (${msgs.total} messages)`),
      msgs.messages.length ? el("div", { class: "chat" }, msgs.messages.map((m) => el("div", { class: "msg " + m.role }, el("span", { class: "muted" }, m.role + " · " + fmtDate(m.createdAt)), el("p", {}, m.content)))) : el("p", { class: "muted" }, "No messages."),
      msgs.total > msgs.pageSize ? el("p", { class: "muted" }, "Page 1 shows the most recent messages.") : null,
      msgs.total > msgs.pageSize ? pager(msgs.page, msgs.pageSize, msgs.total, (n) => customerDetail(id, n, recPage)) : null));
  }

  // ---------------------------------------------------------------- activity
  async function activity(page, attentionOnly) {
    loading();
    let data;
    try {
      data = await api(`/admin/api/activity?page=${page}` + (attentionOnly ? "&attentionOnly=true" : ""));
    } catch (e) {
      return failure(e, () => activity(page, attentionOnly));
    }
    const s = data.summary;
    setState(el("section", {},
      el("h1", {}, "Activity"),
      el("div", { class: "card" },
        el("p", {}, badge(`${s.needsReview} build(s) need review`, s.needsReview ? "bad" : "ok")),
        el("p", { class: "muted" }, "Recommendations: " + JSON.stringify(s.byStatus) + " · builds: " + JSON.stringify(s.byBuildStatus) + " · inventory lookups: " + JSON.stringify(s.inventoryLookups)),
        el("label", {}, el("input", { type: "checkbox", checked: attentionOnly ? "" : null, onchange: (ev) => activity(1, ev.target.checked) }), " Only builds needing review")),
      data.items.length ? el("table", {}, el("thead", {}, el("tr", {}, ["When", "Name", "Status", "Inventory", "Customer", "Shopify"].map((h) => el("th", {}, h)))),
        el("tbody", {}, data.items.map((r) => el("tr", { class: r.needsReview ? "attention" : "" },
          el("td", {}, fmtDate(r.createdAt)), el("td", {}, text(r.name)), el("td", {}, statusBadge(r)),
          el("td", {}, r.inventory ? `${r.inventory.requestStatus}${r.inventory.buildable ? " · buildable" : ""}` : "—"),
          el("td", {}, el("a", { href: "#/customers/" + encodeURIComponent(r.conversationId) }, "open")),
          el("td", {}, r.shopifyAdminUrl ? el("a", { href: r.shopifyAdminUrl, target: "_top" }, "product") : "—")))))
        : el("p", { class: "muted" }, "Nothing to show."),
      pager(data.page, data.pageSize, data.total, (n) => activity(n, attentionOnly))));
  }

  // ---------------------------------------------------------------- readiness
  async function readiness() {
    loading();
    let data;
    try {
      data = await api("/admin/api/readiness");
    } catch (e) {
      return failure(e, readiness);
    }
    const rows = (obj) => Object.entries(obj).map(([k, ok]) => el("tr", {}, el("td", {}, k), el("td", {}, ok ? badge("ok", "ok") : badge("missing", "bad"))));
    setState(el("section", {},
      el("h1", {}, "Readiness: " + data.status),
      el("h2", {}, "Required"), el("table", {}, el("tbody", {}, rows(data.checks))),
      el("h2", {}, "Commerce and data (commerce stays blocked until inventory items are ok)"), el("table", {}, el("tbody", {}, rows(data.informational)))));
  }

  // ---------------------------------------------------------------- router
  function route() {
    const hash = location.hash || "#/customers";
    const tab = hash.split("/")[1] || "customers";
    document.querySelectorAll(".tabs a").forEach((a) => a.classList.toggle("active", a.dataset.tab === tab));
    const detail = hash.match(/^#\/customers\/(.+)$/);
    if (detail) return customerDetail(decodeURIComponent(detail[1]), 1, 1);
    if (tab === "activity") return activity(1, false);
    if (tab === "readiness") return readiness();
    if (tab === "help") return setState(document.getElementById("help-template").content.cloneNode(true));
    return customers("", 1);
  }

  window.addEventListener("hashchange", route);
  route();
})();
