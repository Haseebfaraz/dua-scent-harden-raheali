// Product-page helper for `custom-scent` build products (docs/SHOPIFY_BUILD_SECURITY_CONTRACT.md
// section 4). Replaces the retired `{productId, ratios, name}` save-build call.
//
//  * reads `#scentBuild=<recommendationId>.<buildToken>` once, keeps it in sessionStorage for this
//    product only, and strips it from the URL (never localStorage, cookies, analytics or URLs);
//  * exposes window.DuaScentBuild.reprice(ratios, name) -> server-priced variant;
//  * optionally wires existing theme controls: inputs [data-dua-ratio="top|middle|base"], a
//    button [data-dua-save-build], a price element [data-dua-price], a message [data-dua-message].
// The price shown after a save is always the server's. Without a token the page works as a normal
// product page (existing variants can be bought); only re-pricing needs the capability.
(function () {
  "use strict";

  var root = document.getElementById("dua-custom-scent-build");
  if (!root) return;
  var BACKEND_URL = (root.dataset.backendUrl || "").replace(/\/+$/, "");
  var KEY = "duaScentBuild:" + (root.dataset.productId || "");

  function session() { try { return window.sessionStorage; } catch (e) { return null; } }

  (function captureFragment() {
    var match = /(?:^#|&)scentBuild=([^&]+)/.exec(window.location.hash || "");
    if (!match) return;
    var raw = decodeURIComponent(match[1]);
    var dot = raw.indexOf(".");
    if (dot > 0 && session()) session().setItem(KEY, JSON.stringify({ recommendationId: raw.slice(0, dot), buildToken: raw.slice(dot + 1) }));
    try { history.replaceState(null, "", window.location.pathname + window.location.search); } catch (e) {}
  })();

  function capability() {
    try { return JSON.parse((session() && session().getItem(KEY)) || "null"); } catch (e) { return null; }
  }

  function message(text) {
    var el = root.querySelector("[data-dua-message]") || document.querySelector("[data-dua-message]");
    if (el) el.textContent = text || "";
  }

  var inFlight = false;
  async function reprice(ratios, name) {
    var cap = capability();
    if (!cap) throw Object.assign(new Error("Reopen your fragrance preview from the chat to adjust this blend."), { code: "no_capability" });
    if (inFlight) throw Object.assign(new Error("Your previous change is still being saved."), { code: "build_in_progress" });
    inFlight = true;
    try {
      var res = await fetch(BACKEND_URL + "/api/save-build", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ recommendationId: cap.recommendationId, buildToken: cap.buildToken, ratios: ratios, name: name || undefined }),
      });
      var body = {};
      try { body = await res.json(); } catch (e) {}
      if (!res.ok || body.error) {
        if (body.code === "build_not_authorized" && session()) session().removeItem(KEY);
        throw Object.assign(new Error(body.error || "We couldn't update your blend right now."), { code: body.code || "failed" });
      }
      return body; // { price, variantId, created }
    } finally {
      inFlight = false;
    }
  }

  window.DuaScentBuild = { available: function () { return !!capability(); }, reprice: reprice };

  var save = document.querySelector("[data-dua-save-build]");
  if (!save) return;
  if (!capability()) save.disabled = true;
  save.addEventListener("click", async function () {
    var ratios = {};
    document.querySelectorAll("[data-dua-ratio]").forEach(function (input) { ratios[input.getAttribute("data-dua-ratio")] = parseInt(input.value, 10); });
    save.disabled = true;
    message("Saving your blend…");
    try {
      var result = await reprice(ratios);
      var priceEl = document.querySelector("[data-dua-price]");
      if (priceEl) priceEl.textContent = result.price;
      var numericId = String(result.variantId || "").replace(/^.*\//, "");
      document.querySelectorAll('form[action*="/cart/add"] [name="id"]').forEach(function (field) { field.value = numericId; });
      message("Saved.");
    } catch (err) {
      message(err.message);
    } finally {
      save.disabled = !capability();
    }
  });
})();
