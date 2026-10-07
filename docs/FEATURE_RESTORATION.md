# Feature restoration (merchant dashboard, accounts, history, builds, data maintenance)

Branch `feature/restore-merchant-features`, cut from `security-hardening` @ `25d0525`.
Reference (old) app: `Haseebfaraz/Scent-Ai-App` `main` @ `f3dec51`. Both commits were inspected
directly; the facts below come from source, not from the migration documentation.

Verified facts about the two snapshots:

* Old default branch is `main`; updated default branch is `security-hardening` (`origin/HEAD`).
* The old `app/routes/chat.jsx` still ran its own Claude/fragrance orchestration; it was never the
  thin adapter described in `docs/PYTHON_MIGRATION_AUDIT.md`. Nothing of it was ported.
* The updated repository has no AGENTS.md / CLAUDE.md; project rules come from `README.md` and
  `docs/*.md`.
* The updated project already hosts the App Proxy preview, Save Build and Shopify writes itself
  (the README's "Node adapter" diagram is historical).

This is additive. No prompt, conversation-flow, profiling, readiness, scoring, recommendation,
confirmation, security-gate, capability, inventory-policy or Shopify-write logic was changed.

---

## 1. Feature matrix

Legend: **Done** (already in the updated app), **Server-only** (implemented server-side but not
connected), **Restored** (added on this branch), **Changed by design** (security architecture
deliberately differs from the old app), **External** (needs Shopify theme / ERP / deployment work).

| Feature | Before (evidence) | After |
|---|---|---|
| Chat flow, profiling, readiness, question selection | Done (`app/ai/conversation_flow.py`, `app/ai/prompt.py`, `app/services/customer_profile.py`) | Unchanged |
| Recommendation engine, scoring, confidence, auto-select, confirmation, refinement | Done (`app/services/recommendation_*.py`, `app/ai/tool_executor.py`) | Unchanged |
| Security gate, safe projections, tool allowlists | Done (`app/ai/security_gate.py`, `app/ai/safe_views.py`) | Unchanged |
| Conversation / build capabilities | Done (`app/services/conversation_capability.py`, `build_capability.py`) | Unchanged; reused for linking and My Builds |
| Public history API (capability-gated, read-only) | Server-only (`GET /chat?history=true`, `app/api/chat.py`); widget never called it (`chat-widget.js` @25d0525) | Restored in widget; history route gained two log lines |
| History restore after refresh / navigation / preview return | Missing in widget | Restored (`extensions/scent-chat-widget/assets/chat-widget.js`) |
| Recreate question display | Server-only (persisted by the recreate POST, `app/api/preview.py`) | Restored: widget loads it; Recreate returns to the chat page |
| Expired-token recovery, storage-safe, duplicate-init guard, account-scoped storage | Missing | Restored (widget) |
| Customer name/email for confirmation | **Gap**: `confirm_recommendation` requires both (`recommendation_confirmation.py:171`); old widget sent Liquid name/email (unverified); updated widget sent neither | Restored with a *verified* path: App Proxy `POST /apps/scent-library/account/link` (`app/api/storefront.py`) |
| Guest discovery | Done | Unchanged; guests see a "sign in to save" note |
| Merchant customer list / detail / documentation | Missing (old: `app/routes/app._index.jsx`, `app.customers.$conversationId.jsx`, `app.documentation.jsx`, unscoped) | Restored as embedded admin `/admin` + `/admin/api/*` (`app/api/admin.py`) behind App Bridge session tokens |
| Merchant activity / builds needing review | Missing | Restored (`/admin/api/activity`, Activity tab) |
| Readiness | Missing (`/health` is liveness only) | Restored: `GET /health/ready`; detailed view in admin |
| My Builds | Old: `GET /api/customer-builds?email=&shop_domain=` (no auth, arbitrary email, browser-chosen shop, admin write on GET) | **Changed by design + Restored**: `GET /apps/scent-library/my-builds`, ownership from signed customer + capability binding |
| Re-open a saved build's preview | Missing | Restored: `POST /apps/scent-library/my-builds/open` (fresh capability bound to the owner) |
| Theme slider re-price | Server-only (`POST /api/save-build`, new token contract); theme caller external | Integration asset supplied: `custom-scent-build` block + `custom-scent-build.js` |
| Old `{productId, ratios, name}` save-build | Changed by design: refused with `build_contract_upgraded` | Unchanged |
| Preview UI | Done | Unchanged design; expired/unknown link now renders an HTML page (was raw JSON) |
| App Proxy config | **Broken**: `shopify.app.toml` had no `[app_proxy]` (a CLI deploy would remove it) | Restored in `shopify.app.toml` |
| OAuth callback | **Broken**: `redirect_urls` pointed to unimplemented `/api/shopify/auth/callback` | Pointed at the implemented `/admin` (managed install + client credentials; see §6) |
| Uninstall webhook | Server-only (`/shopify/webhooks`), never subscribed | Subscription declared in `shopify.app.toml` |
| Base schema provisioning | Missing (only ORM `create_all` in the verifier) | Restored: `migrations/0000_base_schema.sql` |
| Schema compatibility vs old Prisma | Missing | Restored: `scripts/check_schema_compat.py` (read-only) + verifier |
| Catalog / inspirations / combinations / oil SKU / notes / order history / summaries / mojibake | Missing (old `scripts/*.cjs`); `seed_staging_reference_data.py` copies only 4 tables, never `Note` or `OrderHistory` | Restored: `scripts/data_import.py`, `scripts/note_encoding.py` |
| Odoo inventory policy, freshness, fail-closed | Done (`app/services/commerce_inventory.py`, 70+ tests) | Unchanged |
| Odoo reservation / order-to-manufacturing handoff | Not implemented anywhere | **External** (§7) — commerce stays blocked until the contract is declared |
| Customer-account OAuth / MCP (`auth.customer.callback`, `token-status`) | Old: dead code, broken state parsing | Deliberately not restored |
| GDPR/compliance webhooks | Neither app | **External** decision (custom single-store app: not required by Shopify; add if distributed) |

---

## 2. Changed files

New

| File | Purpose |
|---|---|
| `app/shopify/session_token.py` | Verifies App Bridge session tokens (HS256, aud, exp/nbf, dest/iss = trusted shop). |
| `app/api/admin.py` | `/admin` shell (CSP `frame-ancestors` Shopify only) and read-only `/admin/api/*`: customers (search/paginate in SQL), detail, messages (paginated), recommendations (ratios, confidence, risk, exact notes, stored inventory snapshot), activity, readiness. No Shopify/Odoo calls. |
| `app/templates/admin.html`, `app/static/js/admin.js`, `app/static/css/admin.css` | Embedded dashboard + Help page. All data rendered with `textContent`. |
| `app/shopify/customers.py` | Reads a signed-in customer's first name/email by Shopify-signed id. |
| `app/api/storefront.py` | App Proxy: `account/link`, `my-builds`, `my-builds/open`. |
| `app/templates/preview_unavailable.html` | Customer page for expired/unknown preview links. |
| `extensions/scent-chat-widget/blocks/my-builds.liquid`, `assets/my-builds.js` | My Builds theme block. |
| `extensions/scent-chat-widget/blocks/custom-scent-build.liquid`, `assets/custom-scent-build.js` | Product-page helper for the build-token re-price contract. |
| `migrations/0000_base_schema.sql` | Shared base tables (consolidated Prisma final state). |
| `scripts/schema_spec.py` | Expected schema parsed from `migrations/*.sql`. |
| `scripts/check_schema_compat.py` | Read-only live-schema comparison. |
| `scripts/data_import.py`, `scripts/note_encoding.py` | Imports and maintenance. |
| `tests/test_admin_api.py`, `tests/security/test_storefront_account.py`, `tests/test_readiness.py`, `tests/test_data_import.py` | 43 focused tests. |

Modified

| File | Change |
|---|---|
| `app/main.py` | Registers the `storefront` and `admin` routers. |
| `app/api/health.py` | Adds `/health/ready` and `readiness_report()`; `/health` unchanged. |
| `app/api/request_limits.py` | Body-size limit now covers all `/apps/scent-library/` and `/admin/` routes (was only the preview path). Stricter only. |
| `app/api/chat.py` | Two `logger.info` lines in the public history route (`HISTORY_NOT_AUTHORIZED`, `HISTORY_LOADED` with id + count). No behavior change. |
| `app/api/preview.py` | GET with an invalid/expired capability or unknown recommendation returns the HTML page (same 403/404 codes) instead of a JSON body; logs `PREVIEW_LINK_REJECTED`. Authorization order unchanged. |
| `app/static/js/fragrance_preview.js` | Recreate navigates to the chat page path stored by the widget (same-origin path only), else the server URL. |
| `extensions/scent-chat-widget/assets/chat-widget.js`, `.css`, `blocks/chat-widget.liquid` | History, expiry recovery, safe storage, account-scoped sessions, account linking, loading/error/retry states, guest sign-in note. Same markup/design and SSE handling. |
| `scripts/verify_migrations.py` | Builds the base from `0000` (not ORM `create_all`) and checks every table's columns/types/nullability, indexes, FKs and ORM parity. |
| `shopify.app.toml` | `application_url` → `/admin`, `redirect_urls` → `/admin`, `read_customers` scope, `app/uninstalled` subscription, `[app_proxy]`. |

---

## 3. Preservation audit

Protected areas **not touched**: `app/ai/*`, `app/fragrance/*`, `app/services/*` (all),
`app/shopify/builds.py`, `products.py`, `admin_auth.py`, `admin_client.py`, `app_proxy.py`,
`trusted_shop.py`, `webhooks.py`, `app/db/models`, migrations 0001–0004, `app/schemas/*`.

Touched with reason:

* `app/api/chat.py` — diagnostics requested in §8 (history load/expiry). Two log lines, no ids
  other than the conversation id already logged elsewhere, no token, no content.
* `app/api/preview.py` — the customer-facing expired-link state requested in §7. Status codes and
  the "authorize before revealing anything" order are unchanged; the page reveals nothing.
* `app/api/request_limits.py` — new App Proxy and admin routes needed the same body limit.
* `fragrance_preview.js` — Recreate return navigation (§7); design untouched.
* `app/ai/security_gate.py` (approved by the owner after rollout testing) — short build commands
  ("okay create", "show preview", "lock it in") were classified SMALL_TALK, so generation never
  re-ran and the model claimed a preview that was never produced. With fragrance context they now
  route as FRAGRANCE (`CONTEXTUAL_ANSWER`). Anchored, closed vocabulary, checked after every
  attack / service-meta / off-topic rule; tests in `tests/security/test_gate_build_commands.py`.
* `app/services/recommendation_confirmation.py` (owner request) — the name+email requirement is
  behind `REQUIRE_CUSTOMER_IDENTITY_FOR_BUILD` (default `true`, unchanged behavior). `false`
  temporarily lets builds confirm without contact details (empty customer metafields).
* `app/services/commerce_inventory.py` (owner explicitly accepted selling without stock checks
  for now) — `COMMERCE_INVENTORY_CHECK_DISABLED` (default `false`, full policy). `true` makes the
  commerce gate approve without any Odoo lookup; every use logs `COMMERCE_INVENTORY_CHECK_DISABLED`
  at WARNING and Readiness shows `inventory.commerce_check_enabled = false`. Orders placed while
  it is on must be checked against real stock by hand. Remove the variable once Odoo is configured.

Profile rule preserved: account details only fill **empty** `name`/`email` profile fields (the
same rule `prompt.py` applies to known customer details); no profiling question, readiness
dimension or prompt was added. Accounts without a first name leave `name` empty and the existing
"ask what to call them" path handles it.

Disclosure preserved: storefront responses carry the customer-facing name only; the test
`test_my_builds_lists_only_the_signed_customers_confirmed_builds` asserts no source title leaks.
Internal components and inventory evidence appear only under `/admin/api`.

---

## 4. Account integration and isolation (how it works)

1. The widget creates/continues a conversation directly with the backend (unchanged contract).
2. When Liquid says a customer is signed in, the widget calls the **same-origin** App Proxy
   route `POST /apps/scent-library/account/link` with `{conversationId, conversationToken}`.
   Shopify signs `logged_in_customer_id`; the backend verifies the signature and trusted shop,
   proves the conversation with its capability, **binds** the capability to that customer
   (existing `bind_verified_customer`), reads the customer's first name/email via the Admin API,
   and fills empty profile/conversation fields under the conversation turn lock.
3. A conversation already bound to customer A is refused for customer B (401). The widget then
   starts a fresh conversation. Browser storage is namespaced by
   `shop | backend host | customer id or guest`, so signing out/in or switching accounts never
   picks up another account's token. A guest who signs in keeps the conversation they hold the
   token for (adopted only if the server accepts the binding).
4. The Liquid customer id is used only for that namespacing; it authorizes nothing.
5. Guests can explore; confirmation still requires name + email exactly as before. Guests see a
   "sign in to save your creation" link.

Residual: the public `GET /chat` history route is a direct (non-proxy) call and cannot see the
signed customer; it relies on the capability token, which the account-scoped storage keeps per
account. Server-side refusal of a bound conversation happens at the next link call.

---

## 5. Database

Fresh database (dev / staging / disposable):

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0000_base_schema.sql
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f migrations/0001_build_capability.sql   # then 0002, 0003, 0004
python -m scripts.check_schema_compat
```

Existing / shared database: **do not** apply `0000`. Run the read-only check (SELECTs on
`information_schema` in a READ ONLY transaction) and apply only the unapplied 0001–0004 it lists:

```bash
python -m scripts.check_schema_compat
```

Verification (disposable local database only; it drops and rebuilds every table):

```bash
make migrations        # python -m scripts.verify_migrations
```

`0000` was checked column-by-column against the old `prisma/schema.prisma` (all 16 models: names,
types, nullability identical) and against the final state of the ten Prisma migrations.

---

## 6. Data maintenance commands

All commands are dry runs unless `--apply`; `--apply` against a non-local database also needs
`--allow-remote`. Every run is one transaction and prints counts, skip reasons (with row numbers)
and warnings; `--report file.json` saves them.

```bash
python -m scripts.data_import catalog --file "data/Notes-Extraction-Separated-hybird.xlsx"
python -m scripts.data_import oil-skus --file "data/Notes-Extraction-Separated-with-Oil-SKU update.xlsx"
python -m scripts.data_import notes --notes-csv data/notes.csv --orders-csv data/order_history.csv
CUSTOMER_KEY_HASH_SALT=... python -m scripts.data_import order-history --file data/order_history.csv [--replace]
python -m scripts.data_import summaries [--prune-stale]
python -m scripts.data_import fix-note-encoding
python -m scripts.data_import validate
```

Order: `catalog` → `oil-skus` → `notes` → `order-history` → `summaries` → `validate`.

Semantics preserved from the reference scripts: `normalize_product_name`,
`create_combination_key`, component by "Dua Inspiration Name" column position, key-collision
skip, last-row-wins dedupe, title-then-handle inspiration matching, single-SKU rows only for oil
mappings (shared SKUs allowed), customer-key HMAC (`CUSTOMER_KEY_HASH_SALT`, trimmed lower-case
name), `CLASSIFICATION:` prefix stripping, note position tie rule (top > middle > base), family =
most frequent classification, and the exact summary aggregation SQL.

Deliberate differences (all safer): no Desktop default path; order history refuses to append on
top of existing rows (pass `--replace`, one transaction); summaries report stale rows and only
delete them with `--prune-stale`; a duplicate inspiration row for one product resolves
last-row-wins inside a run (so re-runs are idempotent); every note string (products, Note names,
order notes) passes through `fix_note_encoding`; nothing is deleted from products, combinations
or mappings.

Measured on a disposable local database with the reference `data/*.xlsx` files: 3,450 products
(1 non-text-title row skipped), inspirations 2,279 by title / 36 by handle / 1 unmatched / 8
superseded duplicates, combinations 377/51/5 (6 collisions, 18 blank-component rows),
2,890 oil mappings (529 multi-SKU rows skipped, 31 SKUs shared). Second run: everything
`unchanged`. One product keeps an unrecognised mojibake sequence (reported by `validate`).
`notes.csv` and `order_history.csv` are not in either repository; import them only from the
authorized source.

---

## 7. Shopify, theme and Odoo integration

**Shopify app (Dev Dashboard / `shopify app deploy`, done deliberately by an operator):**

1. Deploy `shopify.app.toml` from this branch: app proxy (`apps` / `scent-library` → `<backend>/apps/scent-library`), `application_url`/`redirect_urls` → `<backend>/admin`, `read_customers` scope, `app/uninstalled` webhook.
2. Request **protected customer data** access for *name* and *email* for this app; until approved the account link still binds but cannot fill contact details (confirmation then stays blocked for that customer, as today).
3. Re-authorize the store so the new `read_customers` scope is granted.
4. Backend env: `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET` (this app's), `SHOPIFY_SHOP_DOMAIN`, `ALLOWED_ORIGINS` = every storefront origin the widget runs on (custom domains included, exact `https://` origins). Old app tokens and proxy signatures are not valid for this app.
5. Auth model: Shopify-managed install; client-credentials grant for Admin API; App Bridge session tokens for `/admin`. No authorization-code callback is processed.

**Theme (in the theme editor):**

* Chat page: "DUA Fragrance Chat" block (now exports shop, customer id for storage scoping, proxy path).
* Account / "My fragrances" page: add the "DUA My Builds" block.
* `product.custom-scent` template (used by build products): add the "DUA custom scent build" block and mark existing controls: `data-dua-ratio="top|middle|base"` on the three slider inputs, `data-dua-save-build` on the save button, `data-dua-price` on the price element. The helper reads `#scentBuild=<id>.<token>`, keeps it in `sessionStorage`, strips it from the URL and calls `POST /api/save-build` with the new contract. The old theme section `custom-scent-product.liquid` is not in either repository; remove any call that sends `{productId, ratios, name}`.
* Ensure the `custom.internal_components` product metafield is **not** exposed to the storefront and not rendered by the template.

**Odoo (unchanged policy; commerce blocked until declared):**

* Configure `ODOO_INVENTORY_URL`, `ODOO_INVENTORY_API_KEY`, `ODOO_INVENTORY_LOCATION_SCOPE` (echoed exactly as `location` by the endpoint), `ODOO_INVENTORY_QUANTITY_SEMANTICS=UNRESERVED_AVAILABLE` only if the endpoint really returns `available_qty` (on hand minus reservations), `MANUFACTURING_MAX_OIL_ML_PER_BOTTLE`. The admin Readiness tab shows each gap using the gate's own `source_contract_gaps()`.
* The endpoint as integrated today reports `on_hand_qty` only; the truthful declaration is `ON_HAND_INCLUDES_RESERVED`, which keeps commerce blocked. That is correct and intended.
* The gate checks stock; it does **not** reserve it. Reservation at checkout and the order-to-manufacturing handoff need ERP-side support (a reserve/hold call bound to the Shopify order, and a manufacturing order created from the stored formula). Not implemented; contract in `docs/INVENTORY_COMMERCE_SECURITY.md` section 2a.

---

## 8. Operations and logs

New events (JSON lines with `request_id`): `ADMIN_ACCESS` (staff user id + route),
`ADMIN_ACCESS_DENIED`, `HISTORY_LOADED`, `HISTORY_NOT_AUTHORIZED`, `ACCOUNT_LINKED`
(conversation id + booleans), `ACCOUNT_LINK_REFUSED`, `SHOPIFY_CUSTOMER_CONTACT_UNAVAILABLE`
(error type), `MY_BUILDS_LISTED` (count), `MY_BUILD_REOPENED`, `PREVIEW_LINK_REJECTED`,
`READINESS_NOT_READY` (check names), `READINESS_DB_FAILED` (error type), `IMPORT_JOB` (counts).
Tests assert that tokens, emails, names and message text never appear.

Activity view: admin → Activity (from stored state; no log database). Use the request id from a
customer's `X-Request-Id` to find the matching log lines in Render.

Probes: `/health` (liveness, unchanged), `/health/ready` (config + DB + schema; names only).

**Ambiguous builds (`creating` / `pending_review`)** are never retried by the dashboard. Recovery:
in Shopify Admin search products whose `custom.note_composition` metafield contains the
recommendation id (shown on the customer page). If a product exists, finish or archive it there
and set the recommendation's `shopifyProductId`/`buildStatus` accordingly in a reviewed SQL
change; if none exists, set `buildStatus` back to `draft`. Record who did it and why.

---

## 9. Test results

Environment: Windows 11, Python 3.12.10, embedded PostgreSQL (pgserver). Dependencies installed
from `pyproject.toml` at the locked versions of the key packages (the hash lock includes
`uvloop`, which has no Windows build). A local-only pytest plugin (not committed) lets
`socket.socketpair()` create its own loopback pair, which Windows' asyncio needs and the test
network guard otherwise blocks.

* Baseline (clean `25d0525`): **1441 passed, 1 failed, 43 deselected**. The failure,
  `test_the_disposable_database_and_in_process_clients_still_work`, is Windows-only: the proactor
  loop connects through `ConnectEx`, which the guard's counter does not see. Not a product defect.
* This branch: **1483 passed, 2 failed, 43 deselected** (43 new test cases + all existing). Failures:
  the same Windows-only guard test as baseline, and
  `test_separate_conversations_still_share_the_per_ip_turn_budget`, which passed 2/2 when re-run
  in isolation (fixed 60 s window, four slow sequential turns on this machine can straddle a
  window boundary; no rate-limit or chat-turn code was changed). New tests alone: 43/43 passed.
* `make migrations` (updated verifier): `migrations verified`. `check_schema_compat`: compatible.
* JS: `node --check` passes for every changed asset.
* Browser harness (local backend + widget page): new session, history restore after refresh
  including the Recreate question, expiry recovery (revoked token → notice + new session),
  blocked `localStorage` (in-memory fallback) all verified.
* Not run: `live_ai` and `reference_data` suites (paid model / production catalog), real Shopify
  (proxy signing in a live store, protected customer data, theme blocks), real Odoo, Linux CI.

JS → Python helper differences (documented, not changed):

| Helper | Difference | Impact |
|---|---|---|
| `compute_required_oil_ml` | Python `round` is half-to-even; JS `Math.round` half-up: 13 ml × 12.5% → 1.62 vs 1.63 (only fractional percentages) | Recommendation feasibility snapshot only; commerce uses the declared bound |
| `compute_default_ratios` | Same rounding; note counts 1/1/6 → 12/12/76 vs 13/13/74 | Default preview sliders (and price if saved untouched) |
| `compute_feasibility([])` | Python raises; JS returned buildable/∞ | None: caller never passes an empty list |
| `ratioPercent: null` | Python raises → recommendation falls back to "unvalidated"; JS treated as 0% | Recommendation only; commerce rejects it |
| `on_hand_qty: true` | Python accepts bool as 1; JS treated as unknown | Recommendation snapshot only; commerce rejects bools |
| `classify_stock_status(NaN)` | `OUT_OF_STOCK` vs `UNKNOWN` | No caller |
| Price string | `f"{x:.2f}"` half-even vs `toFixed` half-up | ≤1¢ only at exact eighths; theoretical |

---

## 10. Remaining external blockers

1. Shopify: deploy the toml, protected customer data approval, scope re-grant, `ALLOWED_ORIGINS` for custom domains.
2. Theme: add the three blocks; migrate the external `custom-scent` slider caller.
3. Odoo: an unreserved-availability endpoint, declared location scope, manufacturing bound; reservation and manufacturing handoff APIs.
4. Data: authorized `notes.csv` and `order_history.csv`; `CUSTOMER_KEY_HASH_SALT` identical to the one used for existing rows.
5. CI: run this branch on Linux (`make test`, `make migrations`) — the Windows run above is not CI.

## 11. Rollout and rollback

Rollout

1. Staging: fresh DB → `0000`..`0004` → imports (dry run, then `--apply`) → `validate`.
2. Deploy the backend to staging; check `/health/ready`, open `/admin` from the staging store.
3. Deploy the app config + theme extension to the staging store; test: guest chat, sign in → link, confirm a build (with Odoo blocked, expect the honest "unavailable" path), My Builds, Recreate return, expired link page.
4. Production: run `check_schema_compat` (read-only), apply only missing 0001–0004, deploy backend, then app config, then theme blocks. Keep the Render auto-deploy off.

Rollback

* Backend: redeploy `25d0525`. New routes disappear; nothing they wrote needs undoing (they only bind capabilities and fill empty name/email, which the old code also accepts).
* App config: redeploy the previous toml (note: the previous file had no `[app_proxy]`; keep the proxy block or the preview breaks).
* Theme: remove the new blocks; the old widget keeps working against the same API.
* Data: imports are upserts; restore from the pre-import backup if a run must be undone. `0000` is never applied to an existing database.
