# Security architecture (Phase 8)

Trust boundaries of the DUA Scent AI backend as they exist at the end of the hardening branch
(`security-hardening`). This describes what the code does today. It does not describe
integrations that are not implemented (checkout enforcement, stock reservation, Odoo manufacturing
acceptance, external erasure), which are listed as blockers in `docs/RELEASE_READINESS.md`.

Detailed contracts live in the per-phase documents; this file is the map:

| Topic | Document |
|---|---|
| Findings and evidence, phase by phase | `docs/SECURITY_AUDIT.md` |
| Guest session, tokens, deletion (frontend contract) | `docs/CHAT_SECURITY_CONTRACT.md` |
| Scope gate, permissions, attack routing | `docs/AI_SECURITY_GATE.md`, `docs/AI_RED_TEAM_RESULTS.md` |
| What a model may see | `docs/AI_DATA_BOUNDARY.md` |
| Build authorization, Shopify writes | `docs/SHOPIFY_BUILD_SECURITY_CONTRACT.md` |
| Inventory policy, commerce transaction safety | `docs/INVENTORY_COMMERCE_SECURITY.md` |
| Data lifecycle, retention, deletion | `docs/DATA_RETENTION_AND_DELETION.md` |
| Runtime, dependencies, Shopify version, CI, build | `docs/PLATFORM_MODERNIZATION.md` |
| Release blockers | `docs/RELEASE_READINESS.md` |

## 1. Components and trust levels

```
 Storefront widget (untrusted browser)          Shopify App Proxy (HMAC-signed)      Node adapter (server, shared key)
        |                                              |                                     |
   POST /chat/session  POST /chat  GET /chat      GET/POST /apps/scent-library/          POST /internal/chat
   POST /chat/delete   (X-Conversation-Token)     fragrance-preview (?signature=, bt=)   GET /internal/chat/history
        |                                              |                                     GET /internal/recommendations/{id}
        v                                              v                                     |
 +-------------------------------------------------------------------------------------------------+
 |  FastAPI (app/api)  input limits -> capability/ownership -> rate limits -> scope gate -> lock     |
 |                                                                                                 |
 |  app/ai/conversation_flow   (model orchestration, server-owned generation trigger)              |
 |  app/ai/security_gate       (deterministic + fail-closed semantic classifier, per-turn perms)   |
 |  app/ai/tool_executor       (least-privilege model tools; private engine behind a boundary)     |
 |  app/services/*             (profile, capabilities, recommendation pipeline, commerce gate,     |
 |                              build commerce, data lifecycle, rate limits, locks)                |
 |  app/shopify/*              (trusted shop, App Proxy HMAC, Admin GraphQL 2026-07 client)        |
 +-------------------------------------------------------------------------------------------------+
        |                     |                       |                          |
   PostgreSQL 16         OpenAI (raw httpx)     Odoo stock source (GET,      Shopify Admin GraphQL
   (shared with the      chat / extraction /    https only, declared          (draft -> price -> read
    Node/Prisma app)     classifier / copy      contract required)            back -> activate -> publish)
```

Trust levels, from least to most:

1. **Browser / storefront widget**: untrusted. Everything it sends is validated for shape and
   size, and it authorizes nothing by itself.
2. **Shopify App Proxy request**: the query string is HMAC-verified with the app secret
   (`app/shopify/hmac.py`); `shop` must equal the single configured trusted shop
   (`app/shopify/trusted_shop.py`); `logged_in_customer_id`, when present, is a Shopify-verified
   customer id, which still says nothing about which objects that customer owns.
3. **Node adapter** (`/internal/*`): server-to-server shared key (`INTERNAL_API_KEY`, constant
   time compare, fails closed when unset). It may continue an existing conversation by id but
   cannot make the server adopt a non-existent or deleted id.
4. **Shopify webhooks** (`/shopify/webhooks`): body HMAC-verified; only `app/uninstalled` is
   handled (removes that shop's stored session).
5. **The server itself** decides everything that matters: which conversation a request may touch,
   whether a turn may reach a model, which tools exist, when a recommendation is generated, which
   product ids are written, whether a build may be sold.

## 2. Public entry points

| Route | Auth | Purpose | Limits (defaults) |
|---|---|---|---|
| `POST /chat/session` | none | mints a conversation id + capability token (server chooses both) | 10 creates / hour / IP |
| `POST /chat` | `X-Conversation-Token` (or implicit bootstrap without an id) | one chat turn, SSE | 12/min and 200/day per conversation, 30/min per IP; message 4000 chars; body 64 KiB; one turn per conversation at a time (advisory lock class 1); 8 concurrent turns per process; 90 s deadline |
| `GET /chat?history=true&conversation_id=` | `X-Conversation-Token` | read-only history (last 100 customer-visible messages) | 60/min per conversation, 120/min per IP |
| `POST /chat/delete` | `X-Conversation-Token` | owner-requested deletion of exactly this conversation | 120/min per IP |
| `GET /apps/scent-library/fragrance-preview` | App Proxy HMAC + `bt` build capability | preview page for one recommendation | none beyond HMAC |
| `POST /apps/scent-library/fragrance-preview` | App Proxy HMAC + `buildToken` | `recreate`, `save_build`, `add_to_cart` | build lock (class 2) per recommendation |
| `POST /api/save-build` | `buildToken` in body; CORS allow-list (`ALLOWED_ORIGINS`, empty by default) | legacy direct save; legacy `productId`/`shop` body shape refused (400) | same |
| `GET /health` | none | liveness | |
| `POST /internal/chat`, `GET /internal/chat/history`, `GET /internal/recommendations/{id}` | `X-Internal-API-Key` | Node adapter | per-conversation turn limits |
| `POST /shopify/webhooks` | webhook HMAC | `app/uninstalled` only | |

Every refusal on the public chat routes is the same 401 body whether the id is unknown, the token
wrong, the capability expired or revoked, or the conversation deleted. Preview refusals are 403
before the recommendation's existence is revealed.

## 3. Guest session and conversation ownership (F7)

* A conversation exists only after the server mints it. The client never chooses an id.
* Ownership is a random capability token delivered once (bootstrap response or the first
  stream's `id` event), stored hashed (`ConversationCapability.tokenHash`), sent back in a header,
  never in a URL. Tokens expire; a deleted conversation revokes them.
* A conversation id, a customer name, an email, an `Origin`, or a shop domain authorize nothing.
* If the capability was bound to a Shopify-verified customer id, a request carrying a different
  verified id is refused.
* Reads never write: `GET /chat` no longer appends messages or re-creates rows (N14).

Regression evidence: `tests/security/test_conversation_ownership.py`, `test_chat_input_limits.py`,
`test_rate_limiting.py`, `test_identity_trust.py`, `test_final_validation_8.py`.

## 4. Verified identity versus self-reported identity (F8)

`customer_name` / `customer_email` in a chat body, names or emails typed into the chat, and the
Node adapter's assertions are **self-reported**. They only fill a gap in the profile (used on the
blend record and the Shopify product metafield) and never override what is stored, never
authorize a read, a build or a deletion. The only verified identity is Shopify's
`logged_in_customer_id` inside a valid App Proxy signature, and it is used only to bind or check
a capability, never to look objects up by customer.

## 5. Request path of one chat turn

```
validate message ----> trusted shop configured? ----> capability/ownership ----> rate limits
      |                                                                              |
      v                                                                              v
 scope gate: layer 1 deterministic; layer 2 one bounded classifier call (no history, no tools,
 no private data) only when layer 1 is uncertain; ANY classifier failure = UNRESOLVED
      |
      v
 permissions_for(gate): ONE server-owned TurnPermissions for the whole turn (default deny)
      |
      +-- no model_completion (ATTACK / OFF_TOPIC / SERVICE_META / INVALID / UNRESOLVED):
      |     server-authored reply; no model, no extraction, no tools, no external call,
      |     no profile write, no generation; repeated attacks throttle conversation + IP
      |
      +-- FRAGRANCE / SMALL_TALK / MIXED (fragrance remainder only):
            turn lock (class 1) -> extraction (forced structured call, customer-safe context)
            -> system prompt + customer context as DATA + bounded history
            -> server checks readiness (should_generate) -> private pipeline -> safe DTO
            -> bridge completion (tools disabled) -> SSE: id, safe events, chunk, preview_ready, end_turn
```

Model tool surface (the complete list a model can name): `save_customer_profile_field`,
`verify_customer_location`, `resolve_season_preference`, `refine_fragrance_recommendation`, plus
the forced extraction tool `record_profile_updates`. Every call goes through
`execute_model_tool(..., allowed_tool_names=)`; a tool not offered on this turn is refused at the
dispatcher even if it exists globally. Generation is never a model tool: the server triggers it
when the profile is complete (`app/services/recommendation_pipeline.should_generate`).

Bounds: at most 10 tool-resolution completions and 6 tool calls per completion per turn, 40
messages / 24 000 characters of history in context, 700 output tokens per completion, 30 s per
model request, 90 s per turn.

## 6. Model-visible data and the private pipeline (F1, F2, F3)

Every production model call, its inputs and where its output goes:

| Call | Where | Input | History | Tools | Output goes to |
|---|---|---|---|---|---|
| Scope classifier | `security_gate.classify_semantically` | static prompt, the message (bounded), one boolean, last 300 chars of the previous assistant reply | none | one forced classification tool | server routing only (validated enum; UNRESOLVED on any failure) |
| Extraction | `conversation_flow._extract_and_persist_profile_facts` | static prompt, customer-safe profile view as data, last 6 user/assistant texts (6000 chars) | bounded | forced `record_profile_updates` | validated field writes through the same dispatcher the model uses |
| Main conversation | `conversation_flow.call_ai` loop | system prompt, customer context as data, bounded projected history, this turn's extraction results | projected (attack text never replayed) | the offered subset above | customer text (validated/repaired), tool arguments (validated) |
| Bridge | `conversation_flow._bridge_for_recommendation` | the same context plus the safe recommendation DTO | bounded | none | customer text |
| Output repair | `conversation_flow._validate_and_repair_customer_text` | the offending text and a static instruction | none | none | customer text |
| Copy generation | `copy_generation.call_copy_model` | `CopyModelInput` allow-list: notes by role, the customer's stated preferences | none | none | `description` / `whySuits` on the safe DTO |

Never in any model request: source product titles, handles, SKUs, Odoo item codes or
quantities, scores, order-history evidence, Shopify ids, recommendation or conversation ids,
capability tokens, hashes, or profile control fields (`selectedRecommendationId`,
`pendingRecreateRecommendationId`, `customBuildAccepted`, ...). The private engine
(`app/services/recommendation_engine.py`, `order_history.py`, `combination_analysis.py`,
`odoo_inventory.py`, `app/fragrance/*`) runs entirely server side and returns a
`CustomerSafeRecommendation` built by allow-list (`app/ai/safe_views.py`). Recommendation ids and
preview URLs are control data emitted by the server in SSE events, never model text.

Evidence: `tests/security/test_model_boundary.py` (canary engine, every call captured),
`test_context_and_cost_bounds.py`, `test_final_validation_8.py` (real pipeline on a synthetic
catalog; 42 captured requests inspected, plus SSE, JSON and preview HTML).

## 7. Safe output boundary

SSE events are filtered by an allow-list per event type (`app/api/chat.public_sse_event`); the
preview page receives only `recommendationId`, its own `buildToken`, the blend name, note
buckets, ratios, price per position and pills (no product titles, no Shopify ids, no status).
Model text is checked for leaked internal identifiers and scope/privacy violations and repaired
or replaced. Commerce failures use fixed customer-safe messages (`COMMERCE_FAILURES`) that never
carry item codes, quantities, locations, retry hints or exception text.

## 8. Recommendation and build ownership (F1, F6, F10)

* Each recommendation belongs to one conversation. The preview URL carries a server-minted
  **build capability** (`bt`) bound to exactly that recommendation, conversation and shop.
  A capability for another build, a conversation token, or a bare recommendation id is refused.
* The `recreate` intent is an explicit, authorized POST that appends the re-entry prompt and sets
  a marker consumed inside the next turn lock; history reads never do this (N14).
* `save_build` / `add_to_cart`: ratios (three positions, integers summing to 100), name and prices
  are validated before any mutation (`app/shopify/build_input.py`); the product id written back is
  the one the server created, never a client value; the legacy body shape (`productId`, `shop`) is
  rejected.

## 9. Shopify trust and write ordering (F10)

* One trusted shop (`SHOPIFY_SHOP_DOMAIN`); the `shop` parameter is compared to it and never used
  to pick a credential destination. Redirects are disabled on the Admin client.
* Admin API version `2026-07` (`X-Shopify-API-Version` on every reply must match or the call
  fails as a transport error). Every mutation checks `userErrors` and the returned object.
* The created product carries metafields for the manufacturing hand-off: `custom.note_composition`,
  `custom.internal_components` (source titles), `custom.customer_name`, `custom.customer_email`.
  These are store-side data, not model-visible data; see N7 in `docs/SECURITY_AUDIT.md`.
* Build write order, inside the build lock (class 2), after the inventory decision:
  create as **DRAFT** -> record the product id (`creating`) -> set the computed price ->
  read the product back and require **every** variant to carry exactly that price -> re-verify
  inventory freshness -> **ACTIVE** -> publish. Any failure after a mutation was sent is
  **ambiguous** (`pending_review`): nothing is activated or published, nothing is retried
  blindly, and the customer is told the honest state. Operator recovery starts read-only
  (`docs/INVENTORY_COMMERCE_SECURITY.md` section 8).

## 10. Inventory policy and manufacturing limits (F9)

The commerce gate (`app/services/commerce_inventory.py`) approves quantity 1 of a build only when
every fact is known: an active Odoo item mapping with unit `ml` for every component, a declared
source contract (`ODOO_INVENTORY_URL` https, `ODOO_INVENTORY_LOCATION_SCOPE`,
`ODOO_INVENTORY_QUANTITY_SEMANTICS=UNRESERVED_AVAILABLE`), a declared
`MANUFACTURING_MAX_OIL_ML_PER_BOTTLE` (14 to 34 ml, consistent with the repository formula), and a
fresh observation (30 s) echoing the declared location. Anything else is `INSUFFICIENT`,
`UNCONFIRMED` or `SERVICE_UNAVAILABLE` and blocks with a customer-safe message and zero Shopify
writes. **All of these settings are empty by default, so commerce starts blocked.** Recommendation
time availability is lenient and silent; it is never an approval.

Not implemented (release blockers, not described here as controls): stock reservation, checkout
enforcement (a published product can still be bought through ordinary storefront paths), and the
Odoo manufacturing acceptance contract.

## 11. Deletion and retention (F11)

* `POST /chat/delete` is authorized only by the conversation capability. It is synchronous and
  atomic: 200 means the single transaction that revoked the tokens, wrote the tombstone and purged
  the conversation-owned rows has committed; 409 (`deletion_conflict`) means nothing was written
  because an operation was in flight; 401 is not proof of deletion. There is no pending state and
  no worker.
* The shared database with the Node/Prisma application has not been reviewed, so deletion is
  refused (503 `deletion_unavailable`, nothing changed) unless `SHARED_DATA_DELETION_REVIEWED=true`
  is set after that review. Retention (`scripts/data_retention.py`) is dry-run by default and
  refuses execution until `RETENTION_EXECUTION_ENABLED=true` and the same review flag.
* Write guard (lock class 4) + tombstone: every write path that could re-create conversation data
  refuses after deletion; stale in-process caches are evicted; commerce records for a product that
  exists in the store are minimized (preferences removed, product id kept) rather than deleted.
* External copies the backend cannot erase (the Shopify product and its metafields, which
  carry the blend's internal component titles and the customer's self-reported name and email
  for the manufacturing hand-off (N7), orders, backups, model-provider logs) are documented, not
  hidden.

## 12. Logging and errors

Structured JSON logs carry event names, ids, counts, states and reason codes. They never carry
message text, prompts, tokens, secrets, item codes, quantities, Shopify error bodies or stack
traces with values (`safe_exception_summary` logs exception types and frames only). Request logs
record the route template, status and duration. `httpx`/`httpcore` loggers are at WARNING so
URLs with query strings are not logged. Customer-facing errors are fixed strings.

## 13. Deployment prerequisites

Required: Python 3.12, PostgreSQL 16, the hash-locked `requirements.txt`, migrations 0001 to 0004
applied (`python -m scripts.verify_migrations` on a disposable database first),
`DATABASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `SHOPIFY_SHOP_DOMAIN`, `SHOPIFY_API_SECRET`,
`INTERNAL_API_KEY` (when the Node adapter is used), `SHOPIFY_API_VERSION=2026-07`.

Safe-by-default: `ALLOWED_ORIGINS` empty (no cross-origin save), Odoo contract empty (commerce
blocked), `SHARED_DATA_DELETION_REVIEWED=false` (deletion unavailable),
`RETENTION_EXECUTION_ENABLED=false` (dry run). Enabling any of them is a documented operator
decision with prerequisites listed in `docs/RELEASE_READINESS.md`.

Not verified on this branch: a container image build (no runtime on the machine), a hosted CI run,
a live Shopify development store, a live model evaluation, a staging smoke test.
