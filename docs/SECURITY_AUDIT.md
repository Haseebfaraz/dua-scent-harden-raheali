# DUA Scent AI Security Audit (Phase 0)

Date: 2026-09-10
Repository: `rahealiduabrand/dua-scent-ai-python`, branch `main`, commit `9416bd3` ("savebuild")
Scope: complete static read of every file in the tree (125 files, ~18.5k lines) plus a mocked-network
reproduction harness run against the real application code. No application code was changed in
this phase. No live Shopify, Odoo, OpenAI, or database calls were made.

Verdict is at the end. Short version: **PHASE 0 VERDICT: GO TO IMPLEMENTATION** (we understand the
system well enough to fix it). **Current public-production readiness: NO-GO.**

---

## 0. Environment notes for whoever runs the tests next

* The project declares `requires-python >= 3.12`. This machine only has Python 3.11.4. The code
  itself imports and runs on 3.11 (`pip install --ignore-requires-python`), but SQLAlchemy's async
  layer additionally needs `greenlet`, which is not in `pyproject.toml`. On 3.12 it is pulled in
  automatically by SQLAlchemy's wheel metadata; on other interpreters it is missing. Recommend
  adding it explicitly in Phase 8.
* 17 of the 43 test files require a live Postgres containing the real catalog (`FragranceProduct`,
  `ExistingCombination`, 936k `OrderHistory` rows). No local Postgres, Docker, or `uv/pyenv` exists
  here, so those files cannot pass in this environment. The 26 remaining files are pure or mocked.
* Baseline run of the existing suite on this machine (no database): **400 passed, 168 failed,
  6 deselected (live_ai)**. 165 of the failures are `ConnectionRefusedError` or an internal error
  wrapping it (database-backed tests). The other 3 are **pre-existing stale tests** in
  `tests/test_shopify_admin_client.py`: they monkeypatch
  `app.shopify.admin_client.get_offline_access_token`, which stopped existing when commit
  `42f2bf2` moved auth into `admin_auth.py`. They fail on `main` regardless of environment and
  should be fixed (not weakened) in Phase 1.
* Reproduction harness: `tests/security/phase0_repro_harness.py` (deliberately **not** named
  `test_*.py`, so pytest does not collect it). Every check in it PASSES while a vulnerability is
  PRESENT. Run it with:

  ```
  DATABASE_URL="postgresql://u:p@127.0.0.1:1/x" OPENAI_API_KEY=x OPENAI_MODEL=x INTERNAL_API_KEY="" \
  python -m pytest -o addopts="" tests/security/phase0_repro_harness.py
  ```

  Result on `9416bd3`: **21 passed** (21 confirmed unsafe behaviours). Phase 1 inverts these into
  real regression tests that fail while the bug exists.

---

## 1. Architecture map (as actually implemented)

```
Storefront browser (Shopify theme + chat widget)
   |  POST /chat, GET /chat?history=true        (public, no auth, CORS allowlist from env)
   |  POST /api/save-build                      (public, no auth, CORS "*", shop from Origin)
   |  GET/POST /apps/scent-library/fragrance-preview  (via Shopify App Proxy, HMAC-signed query)
   v
Node adapter (separate repo, being retired)
   |  POST /internal/chat, GET /internal/chat/history, GET /internal/recommendations/{id}
   |  (X-Internal-Api-Key; enforced ONLY when INTERNAL_API_KEY is set)
   v
FastAPI service (this repo)  app/main.py
   |-- app/api/chat.py ............ conversation orchestration entry
   |     |-- services/legacy_preview_recovery.py (deterministic short-circuit)
   |     `-- ai/conversation_flow.py  call_ai()
   |           |-- OpenAI #1: forced record_profile_updates extraction (history[-6:] + full profile JSON)
   |           |-- ai/prompt.py build_system_prompt() -> ~17k-char prompt incl. full profile JSON
   |           |-- OpenAI loop (<=10 turns) with FRAGRANCE_AGENT_TOOLS or GENERAL_CONVERSATION_TOOLS
   |           |     `-- ai/tool_executor.py execute_fragrance_tool()  (13 tools, see section 6)
   |           |           |-- services/customer_profile.py      (CustomerProfileState)
   |           |           |-- services/location_verification.py (OrderHistory + open-meteo HTTP)
   |           |           |-- services/order_history.py         (OrderHistory, ProductRegionSummary, FragranceProduct)
   |           |           |-- services/product_catalog.py, combination_analysis.py (FragranceProduct, ExistingCombination)
   |           |           |-- services/recommendation_engine.py (catalog cache, scoring, ratios)
   |           |           |     `-- services/copy_generation.py  (OpenAI #2..#N, copy model, parallel)
   |           |           |-- services/recommendation_confirmation.py (FragranceRecommendation)
   |           |           |-- services/odoo_inventory.py -> integrations/odoo_client.py (Odoo REST, bearer)
   |           |           `-- services/inventory_snapshot.py
   |           |-- OpenAI bridge completion (tools disabled) after preview_ready
   |           `-- OpenAI repair completion if validate_customer_response flags brand/title/SKU
   |-- app/api/preview.py ......... Jinja2 page + recreate/save_build/add_to_cart actions
   |-- app/api/save_build.py ...... direct theme-slider reprice endpoint
   |-- app/shopify/{admin_auth,admin_client,builds,products,metafields,publishing,sessions,webhooks}
   |-- app/api/recommendations.py . internal read of "customer safe" recommendation
   `-- app/db/*  SQLAlchemy async, NullPool, shared Postgres with the Node app (Prisma schema)

Outbound: api.openai.com, {shop}/admin/oauth/access_token, {shop}/admin/api/{ver}/graphql.json,
          ODOO_INVENTORY_URL, geocoding-api.open-meteo.com, api.open-meteo.com
```

SSE contract emitted to the browser by `/chat`: `id`, `profile_progress`, `analysis_progress`,
`candidate_products`, `combination_recommendations`, `recommendation_selected`, `chunk`,
`message_complete`, `preview_ready`, `end_turn`, `error`.

---

## 2. Trust boundaries: every public input and where it becomes trusted

| Input | Route | Validation performed | Becomes trusted at | Verdict |
|---|---|---|---|---|
| `Origin` header | `POST /api/save-build` | strip scheme, split on `/`, default to hard-coded test store | immediately: used as the Shopify shop hostname for credential grant + Admin GraphQL | **UNSAFE (F1)** |
| `productId` body | `POST /api/save-build` | pydantic `str` only | immediately: passed to `productUpdate` mutation | **UNSAFE (F2)** |
| `name` body | `POST /api/save-build`, preview POST | none (no length, no charset) | immediately: becomes Shopify product title | UNSAFE (F2, N6) |
| `ratios` body | `POST /api/save-build`, preview POST | pydantic `dict[str, float]`; sum==100 only on first-time create | immediately: drives variant price and option values | **UNSAFE (N1 price manipulation)** |
| `conversation_id` | `POST /chat`, `GET /chat?history=true`, internal routes | none | immediately: loads and appends to that conversation | **UNSAFE (F7)** |
| `customer_name`, `customer_email` | `POST /chat`, `/internal/chat` | `"@" in email`; name non-empty | immediately: treated as the "known"/trusted account identity, written to `Conversation` and profile, used for identity preflight and Shopify product metafields | **UNSAFE (F8)** |
| `shop_domain` | `POST /chat` | none | immediately: used to build `previewUrl` in SSE | LOW (open-redirect-shaped) |
| `greeting` | `POST /chat` | none | immediately: injected as an `assistant` turn on a fresh conversation and persisted | UNSAFE (N2) |
| `message` | `POST /chat` | none (no max length) | sent to OpenAI with full history | UNSAFE (F6) |
| App Proxy query (`shop`, `timestamp`, `logged_in_customer_id`, `recommendationId`, `signature`) | preview GET/POST | HMAC-SHA256 over sorted params with `SHOPIFY_API_SECRET` | after HMAC: `shop` is trusted; `timestamp` not checked; `logged_in_customer_id` never read; `recommendationId` never bound to a customer | PARTIAL (N3, N4) |
| `X-Shopify-Hmac-Sha256` + raw body | `POST /shopify/webhooks` | HMAC over body | after HMAC | OK |
| `X-Internal-Api-Key` | `/internal/*` | equality (non-constant-time) only when configured | after check | PARTIAL (fail-open when unset, N10) |
| OpenAI tool-call arguments | conversation loop | per-tool checks in `tool_executor.py` / `tools.py` | after per-handler validation | MOSTLY OK (see section 6 gaps) |
| OpenAI text output | conversation loop | privacy regexes (brand, SKU shape, component titles, cuid-shaped ids) + one repair completion | never fully trusted, but is the ONLY leak barrier for data the model already holds | INSUFFICIENT AS PRIMARY CONTROL (F3/F5) |
| Odoo REST body | `odoo_client.py` | JSON parse, `success` flag, `default_code` match | after normalization | OK |
| open-meteo responses | `location_verification.py` | shape checks | after parse | OK |

---

## 3. Data classification

| Class | Data | Where it lives | Who may see it today |
|---|---|---|---|
| SECRET | `OPENAI_API_KEY`, `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `ODOO_INVENTORY_API_KEY`, `INTERNAL_API_KEY`, `CUSTOMER_KEY_HASH_SALT`, `DATABASE_URL`, `Session.accessToken` | env + `Session` table | Server only. **Exception: F1 sends the Shopify key/secret to an attacker-chosen host.** |
| HIGHLY SENSITIVE (business) | Source product titles, handles, `inspirationName` / `inspirationBrand`, `collection`, `notesJson` per product, `ExistingCombination` component lists, `OdooOilMapping` SKUs, on-hand quantities, scoring weights, relevance/final scores, order counts by city/state/country/season, repeat-purchase counts | `FragranceProduct`, `ExistingCombination`, `OdooOilMapping`, `ProductRegionSummary`, `OrderHistory`, `FragranceRecommendation.productsJson/scoreJson/evidenceJson`, `RecommendationInventory*` | Server, **and the conversational LLM (F3)**; product titles also written to Shopify product metafield `custom.internal_components` (N7); `/internal/recommendations/{id}` "customer safe" DTO includes `components[].productName` and `expectedResult` naming titles (N8) |
| CUSTOMER DATA | name, email, city/state/country, weather, likes, dislikes, occasion, gift recipient, strength, all messages, recommendation ids, Shopify product/variant ids for their build | `Conversation`, `Message`, `CustomerProfileState`, `FragranceRecommendation`, Shopify product metafields `custom.customer_name/customer_email` | The customer, **and anyone holding the `conversation_id` (F7)**; the LLM (full profile JSON in the system prompt and `get_customer_profile` tool); Shopify admin staff via metafields |
| INTERNAL (operational) | request ids, conversation ids, recommendation ids, SKUs and titles in `ODOO_INVENTORY_*` logs, `previewUrl` in logs | logs | Ops. Logs contain product titles and SKUs by explicit design comment; acceptable if log access is restricted, flagged in section 7 |
| PUBLIC | note names shown on the preview page, customer-facing name/description/why-suits copy, prices | preview page, Shopify product | Everyone |

---

## 4. External calls (every outbound hostname and where it originates)

| Call site | Hostname origin | Method | Credential carried | Redirects | Timeout |
|---|---|---|---|---|---|
| `app/shopify/admin_auth.py:45` `https://{shop}/admin/oauth/access_token` | `shop` argument. From `/api/save-build`: **caller `Origin` header**. From preview: HMAC-verified `shop` param. | POST JSON | **client_id + client_secret in body** | httpx default `follow_redirects=False` (verified in harness) | 15s |
| `app/shopify/admin_client.py:38` `https://{shop}/admin/api/{ver}/graphql.json` | same `shop` | POST JSON | `X-Shopify-Access-Token` (token was obtained from that same host) | not followed | 15s |
| `app/ai/openai_client.py:26`, `app/services/copy_generation.py:169` | constant `api.openai.com` | POST | Bearer OpenAI key | n/a | 30s / 12s |
| `app/integrations/odoo_client.py:67` `{ODOO_INVENTORY_URL}?skus=` | env (defaults to a sandbox hostname hard-coded in `config.py`) | GET | Bearer Odoo key | not followed | 8s |
| `app/services/location_verification.py:24,152` open-meteo | constant | GET | none | not followed | 8s |
| `app/ai/preview_url.py`, `app/api/preview.py` redirect/cart/product URLs | `shop_domain` (from `/chat` body, `resolve_shop_domain()`, or HMAC-verified `shop`) | none (returned to browser) | n/a | n/a | n/a |

Only the first two rows carry credentials, and only the `/api/save-build` path lets the caller pick
the hostname. No allowlist, no `.myshopify.com` requirement, no hostname syntax check, no IP-literal
or private-range rejection exists anywhere. A DNS-rebinding trick is unnecessary because the
attacker simply names their own host.

---

## 5. AI context inventory: what the conversational model can see

Assembled per turn in `conversation_flow.call_ai()`:

1. **System prompt** (`prompt.py`, ~17k chars in discovery mode): persona and style rules, plus
   `json.dumps(profile)` of the **entire CustomerProfileState** (name, email, city, state, country,
   weather, likes, dislikes, occasion, gift recipient, `selectedRecommendationId`,
   `pendingRecreateRecommendationId`, all booleans) and the list of missing readiness fields.
   Profile string values are customer-influenced (extracted by a model from customer text), so the
   system prompt is a persistent injection surface (N5).
2. **Conversation history**: every user/assistant/tool message of the conversation, unbounded
   (F6). The first assistant turn can be supplied by the caller via `greeting` (N2).
3. **Synthetic extraction tool messages** (record_profile_updates results).
4. **Tool results**, verbatim JSON, including:
   * `analyze_customer_product_candidates`: up to 15 candidates with `productName`,
     `normalizedProductName`, `collection`, `relevanceScore`, `sameCityOrders`, `sameStateOrders`,
     `sameCountryOrders`, `sameSeasonOrders`, `distinctSimilarCustomers`,
     `repeatPurchaseCustomers`, `preferenceMatches`, `dislikeConflicts`, `classification`,
     `orderHistoryNotes`, `evidenceLevel`. **All HIGHLY SENSITIVE.** (The SSE copy strips only the
     two title fields; the model copy is unredacted.)
   * `get_product_notes_and_combination_status`: `title`, `handle`, notes, `fragranceFamily`,
     `collection`, `tagLine`, **`inspirationName`, `inspirationBrand`**, membership in existing
     Hybrid/Tribrid/Quadbrid combinations. Callable for **any catalog title the customer names**.
   * `find_existing_combinations_for_product`, `check_exact_combination_exists`,
     `find_combinations_using_similar_notes`: existing combination titles, types, tag lines, and
     **component product lists** (formula disclosure).
   * `get_customer_profile`: full profile JSON incl. email and internal recommendation ids.
   * `generate_new_product_combinations` / `refine_*` success: `recommendationId` (internal DB
     key) plus customer-facing facts only (this one is well designed). Failure messages describe
     inventory/confidence/identity gating in internal terms.
   * `select_recommendation`, `confirm_product_combination`: recommendation ids.
5. **Model-facing instructions in tool results** ("do not list", "ask the customer to sign in").

Classification of what the model holds: PUBLIC (notes shown to customers anyway), CUSTOMER DATA
(profile, history), **INTERNAL and HIGHLY SENSITIVE** (titles, handles, inspirations, evidence
counts, scores, combination formulas, DB ids). The only thing standing between that context and
the customer is prompt wording plus `validate_customer_response()` regexes. Finding 3 confirmed.

The copy-generation model (`copy_generation.py`) receives notes-by-role and profile preferences
only; titles are used solely for the leak check. That boundary is correct and should be the
template for the main model.

---

## 6. Tools inventory (13 model-callable tools, `app/ai/tools.py` + `tool_executor.py`)

| Tool | Category | Args (model-controlled) | Validation | Reads | Returns to model | Side effects | Needed? |
|---|---|---|---|---|---|---|---|
| `save_customer_profile_field` | WRITE_PROFILE | field (enum), value | field allowlist, type/enum, string <=200, arrays <=20 items (**item length unbounded**), name plausibility, vocab correction, trusted-identity override | profile | profile + missing list | persists profile | Yes |
| `get_customer_profile` | READ_PRIVATE (PII) | none | n/a | profile | full profile JSON incl. email, ids | none | Marginal (prompt already has it) |
| `verify_customer_location` | WRITE_PROFILE + external HTTP | cityText | non-empty | OrderHistory cities, open-meteo | city/country/source text | persists city/weather | Yes |
| `resolve_season_preference` | WRITE_PROFILE | choice enum | enum | profile | text | persists | Yes |
| `analyze_customer_product_candidates` | READ_PRIVATE (catalog + order history) | none | readiness gate | OrderHistory, ProductRegionSummary, FragranceProduct | **raw candidate objects** | scratch cache | Yes, but output must be redacted |
| `get_product_notes_and_combination_status` | READ_PRIVATE (catalog lookup by arbitrary title) | productTitle | non-empty | FragranceProduct, ExistingCombination | **handle, inspiration brand, combos** | none | **Not for the customer-facing model** (catalog enumeration oracle) |
| `find_existing_combinations_for_product` | READ_PRIVATE | productTitle | non-empty | ExistingCombination | combination formulas | none | **Not needed** |
| `check_exact_combination_exists` | READ_PRIVATE | 2-4 titles | count | ExistingCombination | formula/existence | none | **Not needed** (engine already guarantees novelty) |
| `find_combinations_using_similar_notes` | READ_PRIVATE | title, limit (unbounded int) | non-empty | full catalog scan | formulas | none | **Not needed** |
| `select_recommendation` | WRITE_PROFILE | selectionText | non-empty | pending recs for conversation | rec id | persists selected id | Legacy only |
| `generate_new_product_combinations` | WRITE (DB) + Odoo + OpenAI copy | maximumResults, allowedTypes (unbounded/unchecked) | readiness gate | everything | grounded customer-safe facts + rec id | saves recs, inventory snapshot, confirms, emits preview_ready | Yes |
| `refine_combination_recommendations` | WRITE (DB + profile) | feedback | non-empty | same | same | same + mutates likes/dislikes | Yes |
| `confirm_product_combination` | WRITE (DB) | recommendationId | compared to selected id | rec | text | confirms | Legacy only |

No tool performs Shopify writes; commerce mutations happen only in `preview.py` /
`save_build.py`. That separation is good and must be preserved. Tool-argument validation is
generally sound; the problem is what the READ_PRIVATE tools return.

---

## 7. Vulnerabilities

Severity scale: CRITICAL (unauthenticated compromise of credentials or store integrity), HIGH
(data exposure of proprietary or customer data, financial manipulation, or abuse enabling large
cost), MEDIUM, LOW.

### F1 CRITICAL, CONFIRMED: caller-controlled shop hostname receives Shopify client credentials (SSRF / credential exfiltration)

* File/function: `app/api/save_build.py::_shop_from_origin` -> `app/shopify/builds.py::reprice_existing_build` -> `app/shopify/products.py::rename_product` -> `app/shopify/admin_client.py::admin_graphql` -> `app/shopify/admin_auth.py::get_admin_access_token` -> `_request_client_credentials_token`.
* Root cause: `shop = origin.removeprefix("https://").removeprefix("http://").split("/")[0]`, no
  validation, then `f"https://{shop}/admin/oauth/access_token"` with `client_id` and
  `client_secret` in the JSON body.
* Reproduced: harness `test_F1_*` (7 hostname variants incl. attacker domain, port, IP literal,
  userinfo `user:pass@host`, and a non-URL string). All delivered the sentinel key and secret to
  the chosen host. Missing `Origin` falls back to `test-3d-products.myshopify.com`.
* Answers to the brief: shop attacker-controlled **YES**; hostname syntax validated **NO**;
  `.myshopify.com` required **NO**; installed shop allowlisted **NO**; arbitrary domains reachable
  **YES** (including private IPs, so this is also SSRF into the deployment network on Render);
  redirects followed **NO** (httpx default); DNS tricks **unnecessary**; encoded hostname bypass
  **irrelevant, no validation to bypass**; secret actually sent **YES** on every request whose shop
  is not already cached; token later sent to untrusted host **YES but only the token that host
  itself issued** (the process cache is keyed by shop, so no cross-shop token leak was found).
* Impact: full Shopify app credential compromise. With the client id/secret an attacker can mint
  Admin tokens for the real store (client-credentials grant), i.e. read/write products, inventory,
  publications with the app's scopes. Also SSRF to internal addresses.
* Fix: canonical trusted shop resolution (section 9).

### F2 CRITICAL, CONFIRMED: unauthenticated mutation of arbitrary Shopify products before any validation

* File/function: `app/shopify/builds.py::reprice_existing_build` line 149 calls
  `rename_product(session, shop, product_id, name)` **first**, then fetches the product. No
  recommendation lookup, no ownership, no `custom.note_composition` check, no vendor/template
  check, no customer identity, no auth header.
* Reproduced: harness `test_F2_rename_mutation_fires_before_any_ownership_or_metafield_check`.
  A product with no note_composition metafield (i.e. not a Scent AI build) got renamed to
  "PWNED TITLE" and the endpoint then returned 404.
* Impact: anyone can rename every product in the store by GID (GIDs are sequential integers and
  also public via the storefront). Existing builds can also receive unlimited new variants
  (`productVariantsBulkCreate`) with attacker-chosen prices (see N1).
* Also affects `app/api/preview.py::preview_action`: it verifies the App Proxy signature but never
  binds `recommendationId` to the caller (N3), so knowledge of a recommendation id allows rename,
  variant creation, Shopify product creation, draft overwrite, and setting the victim profile's
  `pendingRecreateRecommendationId`.

### N1 HIGH, CONFIRMED (new): price manipulation through unvalidated ratios

* `reprice_existing_build` never checks that ratios sum to 100, are non-negative, or cover the
  three positions. Harness `test_F2b`: ratios `{top:1, middle:1, base:1}` on a $136 product
  created a variant priced under $5 for the same 34 ml bottle. `create_shopify_build_product`
  checks only `sum == 100`; harness `test_F2c` shows `{top:-100, middle:100, base:100}` passes and
  produces a nonsense "(-100%)" option. Variants are published ACTIVE to all sales channels.
* Fix: server-side ratio schema (exactly top/middle/base, integers 5..90, sum 100) shared by both
  paths; price recomputed from server data only.

### F3 HIGH, CONFIRMED: internal catalog data is in the conversational model's context

* Files: `app/ai/tool_executor.py::_handle_analyze_candidates` (raw candidates -> `modelContent`),
  `execute_fragrance_tool` branches for the four catalog tools (raw dicts -> `json.dumps`),
  `get_customer_profile` branch, `app/ai/prompt.py::build_system_prompt` (full profile JSON).
* Reproduced: harness `test_F3`, `test_F3b`, `test_F3c`.
* Exploitability: prompt-injection or plain social engineering ("which products did you use",
  "what is it inspired by", "list your candidates as JSON"). The output filter blocks only the
  brand word, SKU-shaped tokens, the exact component titles of the *selected* recommendation
  (for the bridge turn only) and cuid-shaped ids. It does not block other candidate titles,
  handles, inspiration brands, order counts, scores, or combination formulas, and any leak can
  be paraphrased or encoded past a substring check. The `_LEAKED_ID_PATTERN` (`\bc[a-z0-9]{20,}\b`)
  matches roughly 1 in 16 of the uuid4-hex ids this service mints (harness
  `test_leaked_id_regex_misses_uuid_hex_ids_most_of_the_time`).
* Impact: disclosure of the "inspired by" mapping, source formulas, regional sales evidence, and
  scoring internals; catalog enumeration via the lookup tools.

### F4 HIGH, CONFIRMED: general-purpose chat is intentionally enabled

* `app/ai/prompt.py` early template: "If the customer asks a normal general question, answer it
  naturally." Discovery template: "Answer ordinary general conversation naturally when
  appropriate." No off-topic gate exists in code; `GENERAL_CONVERSATION` mode only withholds
  fragrance tools, it does not restrict topics.
* Impact: cost and brand exposure (homework, code, politics, medical). Fix in Phase 4 plus prompt.

### F5 HIGH, CONFIRMED: no application-layer prompt-injection / extraction defence

* Only controls: prompt instructions, `validate_customer_response()` (brand/SKU/title/formatting),
  one repair completion, cuid regex. No input classification, no normalization of Unicode or
  zero-width characters, no encoded-output detection.
* New injection surfaces found: `greeting` body field is written as an assistant turn and
  persisted (harness `test_F5_greeting_*`, N2); profile values are interpolated into the system
  prompt every turn (N5); tool-result text embeds model-directed imperatives that the model has no
  way to distinguish from customer text.

### F6 HIGH, CONFIRMED: no abuse or cost controls on the public chat

* `/chat` and `/api/save-build` have no rate limiting, no per-IP or per-session budget, no
  max message length (harness `test_F6`: 2,000,000-character message accepted and forwarded), no
  cap on retained history (entire history is re-sent to OpenAI every turn; up to 10 tool loops
  plus extraction, bridge, repair, and N parallel copy calls per turn), no concurrency limit, no
  cap on the process-global `_CONVERSATIONS` dict (harness `test_F6b`), no timeout on the SSE
  response as a whole. Uvicorn has no request body limit. `NullPool` means each request opens a
  new Postgres connection, so a flood also exhausts the database.

### F7 HIGH, CONFIRMED: conversation history and state are readable and writable with only the id

* `GET /chat?history=true&conversation_id=X` returns all messages (harness `test_F7`). It also
  performs a state change on GET (appends a message and clears `pendingRecreateRecommendationId`).
* `POST /chat` with a victim's `conversation_id` loads the victim's full history into the model
  context, continues their conversation, and overwrites `Conversation.customerEmail/customerName`
  and the profile's `name/email` with attacker-supplied values (harness `test_F8`).
* Ids are uuid4 hex (128-bit) for new conversations but are stored in browser localStorage by the
  widget (per the JS reference), appear in `previewUrl` query strings, referrer headers, logs, and
  the preview page HTML. Legacy cuid ids from the Node era also exist in the table. High entropy
  is not authorization.

### F8 HIGH, CONFIRMED: caller-supplied identity is trusted as Shopify identity

* `ChatRequest.customer_name/customer_email` -> `known_customer_*` -> `tool_context` ("the
  trusted, Shopify/session-supplied identity" per code comments) -> written to profile in
  `build_system_prompt`, used by `confirm_recommendation` identity preflight, and stamped into the
  Shopify product metafields `custom.customer_name/customer_email`. The public `/chat` route has no
  Shopify customer session at all; the Node hop that used to resolve identity is gone.
  `extract_email_from_history` additionally promotes any email typed in chat to "confirmed".
* Impact: impersonation, PII overwrite (with F7), products created under another customer's name.

### F9 MEDIUM/HIGH, CONFIRMED: inventory fails open into irreversible commerce actions

* `app/services/odoo_inventory.py::evaluate_candidate_inventory` returns
  `buildable=True, inventoryValidated=False` for MISSING mapping, SKU_NOT_FOUND, LOOKUP_FAILED,
  and on any internal exception. That is acceptable for ranking.
* However `app/api/preview.py` save_build/add_to_cart and `app/api/save_build.py` **never consult
  inventory at all** (grep confirms no `evaluate_candidate_inventory`, `buildable`, or
  `inventoryValidated` reference in the commerce path). Products are created ACTIVE and published
  regardless of the snapshot, and the snapshot is only taken at generation time.
* Fix in Phase 6: explicit states UNKNOWN / VERIFIED / INSUFFICIENT / SERVICE_UNAVAILABLE and a
  fail-closed (or explicitly approved fallback) policy before product creation / cart.

### F10 MEDIUM, CONFIRMED: Shopify Admin API version 2025-04 is unsupported

* `config.py` default and `render.yaml` pin `2025-04`. Shopify's version table (fetched
  2026-09-10) lists 2025-04 as **Unsupported since 2026-04-16**; supported: 2025-10, 2026-01,
  2026-04, 2026-07 (latest stable). Shopify serves unsupported versions by falling forward to the
  oldest supported version, so calls currently "work" against 2025-10 semantics without anyone
  choosing that. Operations using deprecated argument shapes (`productCreate(input:)`,
  `productUpdate(input:)`, `productCreateMedia`) need an explicit compatibility audit before the
  version string moves; exact breakage is UNKNOWN until checked against the 2026-07 schema.

### F11 MEDIUM, CONFIRMED: no retention, expiry, deletion, or anonymization

* Only deletions in the code base: `Session` rows on `app/uninstalled`. Recommendations are
  marked `expired` lazily on confirm attempts but never removed. No customer deletion workflow,
  no GDPR webhook handlers (`customers/data_request`, `customers/redact`, `shop/redact`, which
  Shopify requires for public apps), no cleanup job. Conversations, messages, profiles (name,
  email, city), recommendations, and inventory snapshots persist forever.

### F12 MEDIUM, CONFIRMED: no CI

* No `.github/` directory, no lint or type-check configuration, no lockfile, no
  dependency-audit step. `pyproject.toml` has lower bounds only.

### Additional findings not in the brief

| ID | Sev | Location | Finding |
|---|---|---|---|
| N1 | HIGH | `builds.py` both paths | Price manipulation via ratios (details under F2/N1 above). |
| N2 | MEDIUM | `api/chat.py:91` | `greeting` body field injects and persists an attacker-authored assistant turn. |
| N3 | HIGH | `api/preview.py` | No binding between `recommendationId` and the caller (`logged_in_customer_id` from the signed proxy query is never read). Anyone with a recommendation id can create/rename/re-variant Shopify products and mutate the owning conversation's profile. |
| N4 | LOW | `shopify/hmac.py` | App Proxy `timestamp` is never checked, so a captured signed URL is replayable forever. (Shopify's own libraries also do not enforce this; documented, not urgent.) |
| N5 | MEDIUM | `prompt.py::build_system_prompt` | Customer-influenced profile strings are interpolated into the **system** prompt each turn (persistent indirect injection channel). |
| N6 | MEDIUM | `save_build.py`, `preview.py`, `products.py::rename_product` | Product title (`name`) has no length/charset validation; arbitrary titles land on live, published products. |
| N7 | MEDIUM | `shopify/metafields.py` | Real source product titles are written to product metafield `custom.internal_components`, and customer name/email to `custom.customer_name/customer_email`, on an ACTIVE, published product. Liquid can read `custom.*` metafields, so theme exposure is possible; actual theme behaviour UNKNOWN. Also PII on a product record. |
| N8 | MEDIUM | `recommendation_confirmation.py::to_customer_safe_recommendation` | The "customer safe" DTO includes `components[].productName`, `customerFacingNotesByProduct[].label` (titles), `expectedResult` ("built from A and B"), `analogousExistingCombinations`, and raw historical counts. Only reachable via `/internal/recommendations/{id}` today, but it is named and documented as the safe shape. |
| N9 | MEDIUM | `api/chat.py::require_internal_api_key` | Auth is skipped entirely when `INTERNAL_API_KEY` is unset (fail-open); comparison is not constant-time. Same fail-open pattern for `SHOPIFY_API_SECRET` = "" in `verify_app_proxy_signature` (returns False, i.e. fail-closed, good) versus `admin_auth` (falls back to Session table). |
| N10 | MEDIUM | `api/chat.py::chat_action` | `shop_domain` from an unauthenticated body is used to build the `previewUrl` the widget navigates to (open redirect within the customer's own session; low direct impact, but the value should never be caller-controlled). |
| N11 | LOW | `conversation_flow.py:57` | `_LEAKED_ID_PATTERN` is cuid-shaped and misses ~94% of uuid4-hex ids. |
| N12 | LOW | `tools.py::validate_profile_field_value` | `string_array` items have no per-item length limit; `limit` argument of `find_combinations_using_similar_notes` and `maximumResults` are unbounded. |
| N13 | LOW | `config.py:29-30` | Real Odoo sandbox hostname hard-coded as a default; if env is unset in production the service silently talks to the sandbox. |
| N14 | LOW | `api/chat.py::_history_payload` | State-changing side effect on a GET (CSRF-shaped). |
| N15 | LOW | `db/session.py` | `NullPool` in production: one new Postgres connection per request; combined with F6 this is a database-exhaustion vector. |
| N16 | LOW | logging | `logger.error("Action error: %s", err, exc_info=True)` and `OpenAI API error: ... response.text` log full tracebacks / provider bodies; no secrets observed in them, but `SAVE_BUILD_FAILED` logs the raw attacker-controlled `shop` string. No secret was found in any log call. |
| N17 | INFO | Shopify GraphQL | `create_variant` raises on `userErrors`, but `set_variant_price`, `attach_product_media`, `rename_product`, `set_inventory_item_untracked` ignore `userErrors` silently. |

### Things checked and NOT found (rejected or clean)

* SQL injection: none. All queries go through SQLAlchemy Core/ORM parameters; the only `text()`
  is a constant in a staging script.
* Secrets in git history: none (full `git log -p` scan for OpenAI/Shopify/GitHub token shapes and
  DB URLs with passwords; only test placeholders). `.env` correctly ignored.
* XSS on the preview page: Starlette's Jinja2 environment autoescapes; the JSON blob is escaped
  for `</`. No reflected parameters. Clean.
* Path traversal / command execution / arbitrary file reads: none (no filesystem or subprocess
  use outside static mounts).
* Webhook HMAC and App Proxy HMAC implementations: correct, constant-time compare. (Multi-valued
  query params are not joined per Shopify's spec; no route uses them.)
* Redirect following on credentialed calls: disabled by httpx default. Confirmed.
* Cross-shop token cache contamination: none (cache keyed by shop).
* ReDoS: all regexes are simple alternations; no nested quantifiers found.
* Dependencies: `pip-audit` against the resolved set (fastapi 0.141, starlette 1.6, pydantic 2.13,
  sqlalchemy 2.0.52, asyncpg 0.31, httpx 0.28, jinja2 3.1.6, uvicorn 0.52) reports **no known
  vulnerabilities** in application dependencies (only the venv's bundled pip/setuptools, which
  are tooling). Risk: no upper bounds and no lockfile, so builds are not reproducible.
* The "review" claim that the frontend receives a redacted candidate list while the LLM does not:
  **confirmed exactly** (`_redact_titles_for_sse`).

---

## 8. Findings mapped to the brief

| Brief item | Status | Severity here |
|---|---|---|
| 1 Shop-domain trust / credential exfiltration | **VERIFIED, exploitable** | CRITICAL |
| 2 Unauthorized product mutation | **VERIFIED, exploitable** (plus price manipulation N1, plus preview IDOR N3) | CRITICAL |
| 3 Internal catalog data to LLM | **VERIFIED** | HIGH |
| 4 General-purpose chat enabled | **VERIFIED** (explicit prompt text) | HIGH |
| 5 Injection/extraction defence insufficient | **VERIFIED** (plus new vectors N2, N5) | HIGH |
| 6 Public chat abuse/cost | **VERIFIED** | HIGH |
| 7 Conversation ownership | **VERIFIED** (read and write) | HIGH |
| 8 Customer identity trust | **VERIFIED** | HIGH |
| 9 Inventory fails open | **VERIFIED**, and commerce path never checks inventory at all | MEDIUM/HIGH |
| 10 Shopify API version | **VERIFIED** unsupported since 2026-04-16 | MEDIUM |
| 11 Retention/privacy | **VERIFIED** nothing exists | MEDIUM |
| 12 CI | **VERIFIED** absent | MEDIUM |

Nothing in the brief was rejected. Every suspected finding reproduced.

---

## 9. Proposed Phase 1 (critical fixes only)

Ordered so that no mutation can occur before authorization, with regression tests written first
(the harness assertions inverted).

1. **Canonical trusted shop resolution** (`app/shopify/shop_identity.py`, new):
   `resolve_trusted_shop(session)` returns the single installed shop from, in order: an explicit
   `SHOPIFY_SHOP_DOMAIN` setting (required in production), else the offline `Session` row. A
   strict validator (`validate_shop_hostname`) enforces lowercase ASCII hostname grammar, IDNA
   normalization, no userinfo/port/path, `.myshopify.com` suffix, and rejects IP literals and
   localhost. `admin_auth._request_client_credentials_token` and `admin_client.admin_graphql`
   call the validator and refuse any shop that is not the trusted shop (fail closed).
   `save_build._shop_from_origin` is deleted. App Proxy `shop` must equal the trusted shop.
2. **Save-build redesign**: `POST /api/save-build` stops accepting `productId`. It takes an opaque
   `buildToken` (or the `recommendationId` plus the ownership token from Phase 2; in Phase 1 the
   minimum is: server loads `FragranceRecommendation` by id, requires `buildStatus == "saved"`,
   takes `shopifyProductId` from the row, and verifies via GraphQL that the product has
   `custom.note_composition` whose `recommendationId` equals the row id, vendor "The Dua Brand",
   and template suffix `custom-scent`) **before** any mutation. The theme's slider payload will
   need a small change to send the recommendation id instead of the GID; that is documented, not
   hidden. Origin is only used for CORS, never for identity.
3. **Mutation ordering**: in `reprice_existing_build`, fetch and verify the product first; rename
   last. Rename also moves behind the ownership check in `preview.py`.
4. **Ratio and name schema** shared by both paths: exactly `top/middle/base`, integers, each
   5..90, sum 100; `name` 1..80 printable characters. Server recomputes price only from
   metafield data.
5. **Preview route ownership (minimum for Phase 1)**: bind `recommendationId` to the
   `logged_in_customer_id` from the signed query when present, and record it on first access;
   full session-token ownership arrives in Phase 2.
6. **Regression tests** (`tests/security/test_shopify_trust_boundary.py`,
   `test_save_build_authorization.py`): every harness case inverted, plus positive tests that the
   legitimate flow still works with the trusted shop.

Not in Phase 1: chat ownership, rate limiting, LLM isolation, prompt changes, inventory policy,
API version bump (these are Phases 2 to 8).

### Risks of the Phase 1 changes

* The live theme section `custom-scent-product.liquid` posts `productId` + ratios to
  `/api/save-build`. Changing the contract requires a coordinated theme update; until then, that
  slider path must fail closed (customers see "please reopen your preview") rather than stay
  exploitable. This is a deliberate availability trade for security and needs sign-off.
* `test-3d-products.myshopify.com` is hard-coded as a fallback in two places and as
  `ALLOWED_ORIGINS` in `render.yaml`; production must set `SHOPIFY_SHOP_DOMAIN` explicitly or
  Admin calls will be refused.
* The Session-table fallback in `admin_auth` becomes shop-restricted; if the real install's row
  has a different shop string than the configured one, Admin calls fail until config is fixed.
  This is the correct failure mode.
* Existing tests `test_save_build_api.py` and parts of `test_preview.py` assert the old contract
  and will be updated, not weakened: the security expectations get stricter.

---

## 10. Performance baseline (for later phases, so hardening does not regress it)

Per chat turn today: 1 extraction completion + 1..10 loop completions + 1 bridge + 0..1 repair;
per generation: up to 8 parallel copy completions plus a retry wave; 1 Odoo batch call per
candidate walked; DB reads: profile (several times), 5 shortlist queries + 6 aggregate queries in
analyze, full `FragranceProduct` scan in `_top_products_by_like_match` (3,450 rows, in-process),
catalog cache (5 min TTL). The system prompt is ~17k characters plus the full history every turn.
Any intent gate added in Phase 4 must be deterministic-first with the model classifier only as a
tie-breaker, and history must be capped, or latency and cost will rise.

---

## 11. PHASE 0 VERDICT

**GO TO IMPLEMENTATION.** The system is fully mapped; every suspected finding is reproduced with
a runnable harness; the deterministic recommendation engine, compatibility logic, HMAC
verification, safe JSON embedding, Odoo batching, and the customer-facing copy boundary are
sound and will be preserved.

**Current public-production readiness: NO-GO.** Two CRITICAL findings (F1, F2/N1/N3) allow an
unauthenticated internet user to exfiltrate the Shopify app credentials and to rename, re-price,
or create live store products. These must be closed before any storefront exposure.
