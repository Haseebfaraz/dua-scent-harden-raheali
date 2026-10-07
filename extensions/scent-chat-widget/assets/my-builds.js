// "My Builds": the signed-in customer's saved fragrance builds. Same-origin calls through the
// Shopify App Proxy, which signs the customer's identity; nothing here (no email, no customer id)
// is sent as proof of who the customer is.
(function () {
  "use strict";

  var root = document.getElementById("dua-my-builds");
  if (!root) return;
  var PROXY_PATH = (root.dataset.proxyPath || "/apps/scent-library").replace(/\/+$/, "");
  var list = root.querySelector("[data-dua-builds-list]");
  var status = root.querySelector("[data-dua-builds-status]");
  var pager = root.querySelector("[data-dua-builds-pager]");
  var LABELS = { saved: "Saved", draft: "Not saved yet", processing: "Being finalized" };
  var loading = false;

  function setStatus(text) { status.textContent = text || ""; }

  function card(build) {
    var item = document.createElement("li");
    item.className = "dua-build";
    var title = document.createElement("h3");
    title.textContent = build.name;
    var meta = document.createElement("p");
    meta.className = "dua-build-meta";
    meta.textContent = (LABELS[build.status] || build.status) + " · " + new Date(build.createdAt).toLocaleDateString();
    item.appendChild(title);
    item.appendChild(meta);
    if (build.productUrl) {
      var link = document.createElement("a");
      link.href = build.productUrl;
      link.className = "button";
      link.textContent = "View fragrance";
      item.appendChild(link);
    } else if (build.status !== "processing") {
      var open = document.createElement("button");
      open.type = "button";
      open.className = "button";
      open.textContent = "Open preview";
      open.addEventListener("click", function () { reopen(build.recommendationId, open); });
      item.appendChild(open);
    }
    return item;
  }

  async function reopen(recommendationId, button) {
    button.disabled = true;
    try {
      var res = await fetch(PROXY_PATH + "/my-builds/open", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recommendationId: recommendationId }),
      });
      var body = await res.json();
      if (!res.ok || !body.previewUrl) throw new Error(body.error || "We couldn't open that fragrance.");
      window.location.href = body.previewUrl;
    } catch (err) {
      setStatus(err.message);
      button.disabled = false;
    }
  }

  async function load(page) {
    if (loading) return;
    loading = true;
    setStatus("Loading your fragrances…");
    try {
      var res = await fetch(PROXY_PATH + "/my-builds?page=" + page, { headers: { Accept: "application/json" } });
      var body = await res.json();
      if (!res.ok) throw new Error(body.error || "We couldn't load your fragrances.");
      list.replaceChildren.apply(list, body.builds.map(card));
      setStatus(body.builds.length ? "" : "You haven't created a fragrance yet.");
      renderPager(page, Math.max(1, Math.ceil(body.total / body.pageSize)));
    } catch (err) {
      setStatus(err.message);
    } finally {
      loading = false;
    }
  }

  function renderPager(page, pages) {
    pager.replaceChildren();
    if (pages <= 1) return;
    [["Previous", page - 1, page <= 1], ["Next", page + 1, page >= pages]].forEach(function (spec) {
      var b = document.createElement("button");
      b.type = "button";
      b.textContent = spec[0];
      b.disabled = spec[2];
      b.addEventListener("click", function () { load(spec[1]); });
      pager.appendChild(b);
    });
  }

  load(1);
})();
