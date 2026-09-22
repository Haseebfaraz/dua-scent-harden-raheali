(function () {
  "use strict";

  var root = document.getElementById("dua-chat-widget");
  if (!root) return;

  var BACKEND_URL = (root.dataset.backendUrl || "").replace(/\/+$/, "");
  var STORAGE_ID = "duaChat:conversationId";
  var STORAGE_TOKEN = "duaChat:conversationToken";

  var toggleBtn = document.getElementById("dua-chat-toggle");
  var closeBtn = document.getElementById("dua-chat-close");
  var panel = document.getElementById("dua-chat-panel");
  var messagesEl = document.getElementById("dua-chat-messages");
  var form = document.getElementById("dua-chat-form");
  var input = document.getElementById("dua-chat-input");
  var sendBtn = form.querySelector("button[type=submit]");

  var conversationId = localStorage.getItem(STORAGE_ID) || null;
  var conversationToken = localStorage.getItem(STORAGE_TOKEN) || null;
  var opened = false;
  var sending = false;

  function appendMessage(role, text) {
    var el = document.createElement("div");
    el.className = "dua-chat-message " + role;
    el.textContent = text;
    messagesEl.appendChild(el);
    messagesEl.scrollTop = messagesEl.scrollHeight;
    return el;
  }

  function setSession(id, token) {
    conversationId = id;
    conversationToken = token;
    if (id) localStorage.setItem(STORAGE_ID, id);
    if (token) localStorage.setItem(STORAGE_TOKEN, token);
  }

  function clearSession() {
    conversationId = null;
    conversationToken = null;
    localStorage.removeItem(STORAGE_ID);
    localStorage.removeItem(STORAGE_TOKEN);
  }

  function setBusy(busy) {
    sending = busy;
    input.disabled = busy;
    sendBtn.disabled = busy;
  }

  async function ensureSession() {
    if (conversationId && conversationToken) return;
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
      if (res.status === 401) clearSession();
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
          window.location.href = event.previewUrl;
          return;
        } else if (event.type === "error") {
          appendMessage("error", event.error || "Something went wrong.");
        }
      }
    }
  }

  async function handleSend(text) {
    setBusy(true);
    appendMessage("user", text);
    try {
      await ensureSession();
      await streamChat(text);
    } catch (err) {
      appendMessage("error", "Couldn't reach the fragrance studio. Please try again.");
    } finally {
      setBusy(false);
      input.focus();
    }
  }

  toggleBtn.addEventListener("click", async function () {
    panel.hidden = !panel.hidden;
    if (!panel.hidden && !opened) {
      opened = true;
      try {
        await ensureSession();
      } catch (err) {
        appendMessage("error", "Couldn't reach the fragrance studio. Please try again.");
      }
      input.focus();
    }
  });

  closeBtn.addEventListener("click", function () {
    panel.hidden = true;
  });

  form.addEventListener("submit", function (evt) {
    evt.preventDefault();
    if (sending) return;
    var text = input.value.trim();
    if (!text) return;
    input.value = "";
    handleSend(text);
  });
})();
