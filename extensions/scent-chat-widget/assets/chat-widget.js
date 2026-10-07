(function () {
  "use strict";

  var root = document.getElementById("dua-chat-widget");
  if (!root) return;

  var BACKEND_URL = (root.dataset.backendUrl || "").replace(/\/+$/, "");
  // Same-origin App Proxy prefix: Shopify signs these requests (shop + logged_in_customer_id).
  var PROXY_PATH = (root.dataset.proxyPath || "/apps/scent-library").replace(/\/+$/, "");
  var SHOP = root.dataset.shop || "";
  // Liquid-exported customer id: used ONLY to keep different accounts' sessions apart in this
  // browser. It authorizes nothing; the server learns the real customer from the signed proxy.
  var CUSTOMER_ID = root.dataset.customerId || "";
  var RETURN_PATH_KEY = "duaChat:returnPath";

  var messagesEl = document.getElementById("dua-chat-messages");
  var form = document.getElementById("dua-chat-form");
  var input = document.getElementById("dua-chat-input");
  var sendBtn = form.querySelector("button[type=submit]");

  // ---- storage that never throws (private mode, blocked storage, sandboxed previews) ----
  function safeStore(kind) {
    var memory = {};
    var real = null;
    try {
      real = window[kind];
      var probe = "duaChat:probe";
      real.setItem(probe, "1");
      real.removeItem(probe);
    } catch (e) {
      real = null;
    }
    return {
      get: function (k) { try { return real ? real.getItem(k) : (k in memory ? memory[k] : null); } catch (e) { return null; } },
      set: function (k, v) { try { if (real) real.setItem(k, v); else memory[k] = v; } catch (e) { memory[k] = v; } },
      remove: function (k) { try { if (real) real.removeItem(k); } catch (e) {} delete memory[k]; },
    };
  }
  var store = safeStore("localStorage");
  var tabStore = safeStore("sessionStorage");

  // One session per storefront + backend environment + account. Switching account (or signing
  // out) never picks up another account's conversation token.
  function namespace(customerId) {
    var backendHost = BACKEND_URL.replace(/^https?:\/\//, "");
    return "duaChat:v2:" + SHOP + "|" + backendHost + "|" + (customerId || "guest") + ":";
  }
  var NS = namespace(CUSTOMER_ID);
  var GUEST_NS = namespace("");

  var conversationId = store.get(NS + "id");
  var conversationToken = store.get(NS + "token");
  var sending = false;
  var initPromise = null;

  // Drop the pre-namespacing keys: they could belong to any account that used this browser.
  store.remove("duaChat:conversationId");
  store.remove("duaChat:conversationToken");

  function hideEmptyState() {
    var emptyState = document.getElementById("dua-chat-empty-state");
    if (emptyState) emptyState.remove();
  }

  function clearMessages() {
    Array.prototype.slice.call(messagesEl.children).forEach(function (child) {
      if (child.id !== "dua-chat-empty-state") child.remove();
    });
  }

  function appendMessage(role, text) {
    hideEmptyState();
    var el = document.createElement("div");
    if (role === "error" || role === "notice") {
      el.className = role === "error" ? "dua-chat-error" : "dua-chat-notice";
    } else {
      el.className = "shop-ai-message " + role;
      if (role === "user") el.setAttribute("data-initial", "U");
    }
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  var statusEl = null;
  function setStatus(text, retry) {
    if (statusEl) { statusEl.remove(); statusEl = null; }
    if (!text) return;
    statusEl = document.createElement("div");
    statusEl.className = retry ? "dua-chat-error" : "dua-chat-loading";
    statusEl.setAttribute("role", "status");
    statusEl.textContent = text;
    if (retry) {
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "dua-chat-retry";
      btn.textContent = "Try again";
      btn.addEventListener("click", retry);
      statusEl.appendChild(document.createTextNode(" "));
      statusEl.appendChild(btn);
    }
    messagesEl.appendChild(statusEl);
  }

  function setSession(id, token) {
    conversationId = id;
    conversationToken = token;
    if (id) store.set(NS + "id", id);
    if (token) store.set(NS + "token", token);
  }

  function clearSession() {
    conversationId = null;
    conversationToken = null;
    store.remove(NS + "id");
    store.remove(NS + "token");
    store.remove(NS + "linked");
  }

  function setBusy(busy) {
    sending = busy;
    input.disabled = busy;
    sendBtn.disabled = busy;
  }

  async function createSession() {
    var res = await fetch(BACKEND_URL + "/chat/session", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ with_welcome: true }),
    });
    if (!res.ok) throw new Error("session_failed");
    var data = await res.json();
    setSession(data.conversationId, data.conversationToken);
    if (data.welcomeMessage) appendMessage("assistant", data.welcomeMessage);
  }

  // Read-only: returns false when the token is no longer valid (expired / revoked / deleted).
  async function loadHistory() {
    var res = await fetch(BACKEND_URL + "/chat?history=true&conversation_id=" + encodeURIComponent(conversationId), {
      headers: { "X-Conversation-Token": conversationToken },
    });
    if (res.status === 401) return false;
    if (!res.ok) throw new Error("history_failed");
    var data = await res.json();
    clearMessages();
    (data.messages || []).forEach(function (m) { appendMessage(m.role, m.content); });
    return true;
  }

  // Signed-in customers: prove the conversation (token) and let Shopify prove the account
  // (signed proxy). The server binds them and fills empty account details. Best-effort: a
  // failure leaves the chat usable and is retried on the next page load.
  async function linkAccount() {
    if (!CUSTOMER_ID || store.get(NS + "linked") === conversationId) return true;
    var res;
    try {
      res = await fetch(PROXY_PATH + "/account/link", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ conversationId: conversationId, conversationToken: conversationToken }),
      });
    } catch (e) {
      return true;
    }
    if (res.status === 401) return false; // bound to a different account, or no longer valid
    if (res.ok) store.set(NS + "linked", conversationId);
    return true;
  }

  // A guest who signs in keeps the conversation they started (they hold its token). The server
  // refuses it if it is already bound to a different customer.
  function adoptGuestSession() {
    if (!CUSTOMER_ID || conversationId) return;
    var id = store.get(GUEST_NS + "id");
    var token = store.get(GUEST_NS + "token");
    if (!id || !token) return;
    setSession(id, token);
    store.remove(GUEST_NS + "id");
    store.remove(GUEST_NS + "token");
  }

  async function startFresh(notice) {
    clearSession();
    clearMessages();
    if (notice) appendMessage("notice", notice);
    await createSession();
    await linkAccount();
  }

  async function initialize() {
    setStatus("Loading your conversation…");
    adoptGuestSession();
    if (conversationId && conversationToken) {
      var valid = await loadHistory();
      if (valid) valid = await linkAccount();
      if (!valid) await startFresh("Your previous conversation has expired, so we've started a new one.");
    } else {
      await createSession();
      await linkAccount();
    }
    setStatus(null);
  }

  // One initialization at a time; a failed one can be retried and sends wait for it.
  function ensureReady() {
    if (!initPromise) {
      initPromise = initialize().catch(function (err) {
        initPromise = null;
        setStatus("Couldn't reach the fragrance studio.", function () { setStatus(null); ensureReady(); });
        throw err;
      });
    }
    return initPromise;
  }

  async function streamChat(message) {
    var res = await fetch(BACKEND_URL + "/chat", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Conversation-Token": conversationToken || "",
      },
      body: JSON.stringify({ conversation_id: conversationId, message: message }),
    });

    if (!res.ok) {
      if (res.status === 401) {
        clearSession();
        initPromise = null;
        appendMessage("notice", "Your conversation session expired. Send your message again to start a new conversation.");
        return;
      }
      var reason = "Something went wrong. Please try again.";
      try {
        var body = await res.json();
        if (body && body.detail) reason = typeof body.detail === "string" ? body.detail : reason;
      } catch (e) {}
      appendMessage("error", reason);
      return;
    }

    var reader = res.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    var assistantEl = null;
    var assistantText = "";

    while (true) {
      var chunk = await reader.read();
      if (chunk.done) break;
      buffer += decoder.decode(chunk.value, { stream: true });

      var parts = buffer.split("\n\n");
      buffer = parts.pop();

      for (var i = 0; i < parts.length; i++) {
        var line = parts[i];
        if (!line.startsWith("data: ")) continue;
        var event;
        try {
          event = JSON.parse(line.slice(6));
        } catch (e) {
          continue;
        }

        if (event.type === "id") {
          if (event.conversation_token) setSession(event.conversation_id, event.conversation_token);
          else if (event.conversation_id) conversationId = event.conversation_id;
        } else if (event.type === "chunk") {
          if (!assistantEl) assistantEl = appendMessage("assistant", "");
          assistantText += event.chunk;
          assistantEl.textContent = assistantText;
          messagesEl.scrollTop = messagesEl.scrollHeight;
        } else if (event.type === "preview_ready" && event.previewUrl) {
          // Remember where the chat lives so Recreate can bring the customer back here.
          tabStore.set(RETURN_PATH_KEY, window.location.pathname);
          window.location.href = event.previewUrl;
          return;
        } else if (event.type === "error") {
          appendMessage("error", event.error || "Something went wrong.");
        }
      }
    }
  }

  async function handleSend(text) {
    if (sending) return;
    setBusy(true);
    appendMessage("user", text);
    try {
      await ensureReady();
      await streamChat(text);
    } catch (err) {
      appendMessage("error", "Couldn't reach the fragrance studio. Please try again.");
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  ensureReady().catch(function () {});

  // Returning with the browser's back button can restore a cached page: re-read the history so a
  // Recreate question asked from the preview is shown.
  window.addEventListener("pageshow", function (evt) {
    if (!evt.persisted || sending || !conversationId || !conversationToken) return;
    loadHistory().catch(function () {});
  });

  form.addEventListener("submit", function (evt) {
    evt.preventDefault();
    if (sending) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    handleSend(text);
  });

  document.querySelectorAll(".scent-pill").forEach(function (pill) {
    pill.addEventListener("click", function () {
      if (sending) return;
      var scent = pill.getAttribute("data-scent");
      handleSend("My preferred fragrance classification is " + scent + ".");
    });
  });
})();
