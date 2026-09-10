# Chat Security Contract (storefront widget integration)

Audience: the developer maintaining the storefront chat widget. This describes what the browser
sends and receives. No secrets, no internal details.

Status: **the widget must be updated.** Continuing a conversation now requires the conversation
token described below; a widget that only stores `conversation_id` receives `401` on its next
message and must start a new conversation.

---

## 1. Concepts

* **conversationId**: opaque identifier of one conversation. It identifies, it does not authorize.
* **conversationToken**: an opaque, server-minted, high-entropy secret tied to that one
  conversation. Whoever presents it may continue the conversation and read its history. It
  expires after 30 days of inactivity and may be revoked. Treat it like a password.

Name, email, and shop values sent by the browser are contact data only. They never authenticate.

## 2. Starting a conversation

Either call the bootstrap endpoint:

```
POST /chat/session
Content-Type: application/json
{ "with_welcome": true }
```

```json
{ "conversationId": "…", "conversationToken": "…", "expiresAt": "2026-10-10T12:00:00Z", "welcomeMessage": "Hi! …" }
```

or send the first message with no `conversation_id`. The first SSE frame then carries the pair
exactly once:

```
data: {"type":"id","conversation_id":"…","conversation_token":"…","expires_at":"…"}
```

Store both in `sessionStorage` (not `localStorage`, not a cookie, never in the URL). Note the
trade-off: anything readable by page JavaScript is readable by an XSS payload on the storefront;
`sessionStorage` limits the lifetime to the tab.

`with_welcome: true` asks the server to open with its own welcome line. The old `greeting` field
is accepted for compatibility and ignored: the browser cannot supply assistant text.

## 3. Sending a message

```
POST /chat
Content-Type: application/json
X-Conversation-Token: <conversationToken>
{ "conversation_id": "<conversationId>", "message": "…", "customer_name": "optional", "customer_email": "optional" }
```

`conversation_token` may be sent in the body instead of the header. Messages are limited to
4000 characters and must not be empty. The response is the same SSE stream as before
(`id`, optional progress frames, `chunk`, `message_complete`, optional `preview_ready`,
`end_turn`, or `error`).

## 4. Reading history

```
GET /chat?history=true&conversation_id=<conversationId>
X-Conversation-Token: <conversationToken>
```

Returns `{ "messages": [ { "role": "user" | "assistant", "content": "…" } ] }`, most recent 100
at most. The token is never accepted in the query string.

## 5. Errors

| Status | Meaning | Widget action |
|---|---|---|
| 400 | Invalid or oversized message | Show the returned `detail`. |
| 401 | Missing, wrong, expired, or revoked conversation token | Clear stored session, start a new conversation (section 2). |
| 409 | A reply is already being generated for this conversation | Wait `Retry-After` seconds, then retry once. |
| 413 | Request body too large | Shorten the message. |
| 429 | Too many requests | Wait `Retry-After` seconds; show a gentle "one moment" notice. |
| 503 | Studio busy or not configured | Retry after `Retry-After`. |

Rate-limited and busy responses are returned before any stream starts, so the widget can rely
on the HTTP status.

## 6. Session reset

To start over, discard the stored pair and bootstrap again. Old history stays retrievable only
with the old token until it expires.
