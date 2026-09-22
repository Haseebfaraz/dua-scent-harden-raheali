# Release readiness (Phase 8)

Assessment of branch `security-hardening` at the end of the hardening programme. Nothing on this
branch has been pushed, deployed, or connected to a live store, ERP, model account or customer
data. The verdicts below rest on the deterministic evidence in `docs/SECURITY_AUDIT.md` section 22.

## 1. Verdicts

| Target | Verdict | Why |
|---|---|---|
| Isolated staging (synthetic data, no real customers, no real purchases) | **CONDITIONAL GO** | code-level controls are in place and regression-tested; the conditions are the staging-column items below (hosted CI green, image built and started, migrations on a clean database, dev-store validation before any Shopify credential is configured) |
| Public chat (real customers, live model) | **NOT READY** | the semantic classifier and the model's behaviour have never been evaluated live (F4/F5 PARTIAL); the storefront theme still carries the pre-hardening widget contract (N3); no hosted CI run exists |
| Real purchases | **NOT READY** | inventory approval needs an Odoo contract that does not exist yet (F9); no reservation; a published product remains purchasable through ordinary storefront paths without any backend check; no development-store validation of the write sequence |
| Customer deletion | **NOT READY** | the shared database with the Node/Prisma application has not been reviewed; deletion is refused by default until `SHARED_DATA_DELETION_REVIEWED=true`; external copies (Shopify product/metafields, orders, model-provider logs, backups) have no erasure path (F11 PARTIAL, N7 OPEN) |
| Automatic retention | **NOT READY** | policy is provisional and unapproved; execution is disabled by default; no scheduler exists |

## 2. Blockers

Legend for "blocks": S = isolated staging, C = public chat, P = real purchases, D = customer
deletion, R = automatic retention.

| # | Finding | Action required | Responsible role | Prerequisite | Closure evidence | Environment | Blocks | Status |
|---|---|---|---|---|---|---|---|---|
| B1 | N3 (theme widget contract) | migrate the storefront theme's chat widget to the Phase 2/6A contract: `POST /chat/session`, `X-Conversation-Token` header, `id` event handling, 401 = new session, deletion semantics (`docs/CHAT_SECURITY_CONTRACT.md`) | theme developer | theme repository access | widget code review + manual session in a dev store showing bootstrap, continuation, 401 recovery, deletion messages | Shopify dev store | C, D | OPEN |
| B2 | F9 (Odoo endpoint contract) | obtain a written contract for the stock source: endpoint, location scope, unreserved-available semantics, unit; set `ODOO_INVENTORY_URL`, `ODOO_INVENTORY_LOCATION_SCOPE`, `ODOO_INVENTORY_QUANTITY_SEMANTICS`; declare `MANUFACTURING_MAX_OIL_ML_PER_BOTTLE` from the production formula | ERP owner + operations | Odoo sandbox that echoes the contract | recorded sandbox responses matching the declared contract; `tests/security/test_commerce_policy_5a.py` premises re-checked against them | Odoo sandbox | P | OPEN |
| B3 | F9 (manufacturing acceptance) | define how a saved build reaches manufacturing (order -> Odoo) and what "accepted" means; until then a saved product is not a manufacturable order | ERP owner | B2 | documented flow + a synthetic order traced end to end | Odoo sandbox | P | OPEN |
| B4 | F9 (purchase / reservation enforcement) | decide and implement checkout-side enforcement for custom builds (reservation at add-to-cart, or a checkout validation, or keeping products unpublished until reserved); the backend cannot stop a storefront purchase today (`docs/INVENTORY_COMMERCE_SECURITY.md` section 9) | product owner + Shopify developer | B2 | design accepted; regression tests for the chosen mechanism | dev store | P | OPEN (not implemented on this branch by design) |
| B5 | F4 / F5 (live model evaluation) | run `tests/e2e/test_ai_red_team_live.py` (24 tests) and the conversation simulation with a development model key; review the classifier's decisions on the benign-unresolved set (see `docs/AI_RED_TEAM_RESULTS.md`) | AI owner | development OpenAI key, `ALLOW_LIVE_NETWORK=1`, manual `live-eval.yml` run | run log with counts; no attack routed to the model; benign false-negative rate reviewed | CI (manual workflow) or a workstation | C | OPEN |
| B6 | F10 (Shopify development-store validation) | execute the build sequence against a development store: draft creation, price, read-back, activation, publication, App Proxy signature, version header `2026-07` | Shopify developer | dev store + client credentials for that store only | recorded run; `ShopifyApiVersionMismatch` never raised; product ends ACTIVE with the computed price on every variant | dev store | S (before any Shopify credential is configured), P | OPEN |
| B7 | F12 (hosted CI) | push the branch to a repository with Actions enabled and get `.github/workflows/ci.yml` green (lint, lock sync, audit, startup check, migrations, security suite, full suite) | repository owner | a push (not authorized in the hardening programme) | green run URL | GitHub Actions | S | OPEN |
| B8 | F12 (container verification) | build the two-stage `Dockerfile`, start the image with fake settings, confirm `/health`; no container runtime existed on the validation machine | operations | a machine with a container runtime | build log + startup log | build host | S | OPEN (IMAGE BUILD NOT RUN) |
| B9 | F11 (shared-database review) | complete the review in `docs/DATA_RETENTION_AND_DELETION.md` section 2a (which tables/rows the Node/Prisma application also reads; whether purging them breaks it); then, and only then, set `SHARED_DATA_DELETION_REVIEWED=true` | data owner + Node application owner | Prisma schema and access to the Node application's queries | signed-off checklist; `tests/security/test_deletion_6a.py` still green | staging | D, R | OPEN |
| B10 | F11 (retention approval and scheduling) | approve or amend the provisional retention periods; schedule `scripts/data_retention.py` (dry run first, then `RETENTION_EXECUTION_ENABLED=true`) | data owner | B9 | approved policy; a dry-run report from staging; a scheduled job definition | staging | R | OPEN |
| B11 | N7 (external erasure and metafield exposure) | define the process for erasing a customer's data held outside this backend: the Shopify product and its `custom.customer_name` / `custom.customer_email` / `custom.internal_components` metafields (still written on every build for the manufacturing hand-off), orders, model-provider request logs, backups; confirm the theme does not render `custom.*` metafields | privacy owner | inventory of external copies (`docs/DATA_RETENTION_AND_DELETION.md` section 8) | written procedure + one exercised request | Shopify admin, provider consoles | D | OPEN |
| B12 | credential rotation | rotate every credential that was ever present in a developer environment or an earlier commit history of this codebase (Shopify app secret/tokens, Odoo key, OpenAI key, internal API key, database password); the hardening programme was not authorized to rotate anything | operations / security | access to each provider | rotation record with dates | all | S | OPEN (operator action) |
| B13 | reference-data validation | run `make test-reference` (13 tests) against the production reference catalog on a workstation; they cannot run in CI | engineering | read access to the reference catalog export | 13 passed, with the catalog snapshot date | workstation | C (recommendation quality assurance, not security) | OPEN (NOT RUN on this branch) |
| B14 | staging smoke | after B7/B8, deploy to isolated staging with synthetic data and run `scripts/staging_recommendation_smoke_test.py` plus one manual guest journey | operations | B7, B8 | smoke log | staging | C, P | OPEN (NOT RUN) |
| B15 | copy-model cost bound (observation, not security) | the copy model is called once per candidate with a retry (about 15 calls per generation in the Phase 8 journey); bound or batch before public traffic | engineering | none | measured calls per generation | staging | C (cost) | OPEN (low) |
| B16 | system prompt names internal systems (observation, low) | the static system prompt tells the model not to mention "checking Odoo"; if the prompt ever leaked it would reveal the ERP vendor; consider neutral wording | engineering | none | prompt text review | none | none | OPEN (low) |

## 3. What is protected today, by which means

| Protected by code and regression tests | Documented or configured only | Not verifiable on this branch | Blocked on an external party |
|---|---|---|---|
| conversation ownership and bootstrap (F7); input limits and rate limits (F6); scope gate, per-turn permissions, attack routing without a model (F4/F5 deterministic part); model data boundary and safe DTOs (F1, F2, F3); build capability and preview authorization (F1, F6); trusted shop, redirects off, product ids from server state, write ordering, ambiguous outcomes (F10 code part); inventory policy fail-closed, concurrency, freshness (F9 code part); atomic deletion, write guard, no resurrection, cache eviction, minimized commerce records (F11 code part); log hygiene (N-series); locked dependencies, Python 3.12, migrations (F12 code part) | retention periods; shared-data review checklist; operator recovery procedure; credential rotation; external-copy inventory; Shopify version compatibility (manual matrix + 4 reference pages) | semantic classifier behaviour on a live model; the model's own behaviour under attack; container build; hosted CI; staging behaviour; reference-catalog ranking tests | Odoo contract and manufacturing acceptance; checkout enforcement; theme migration; Node/Prisma shared-database review; external erasure |

## 4. Next authorized work

Each item requires its own explicit authorization; none of it was performed on this branch:

1. Push the branch and obtain a green hosted CI run (B7); build and start the image (B8).
2. Development-store validation of Shopify writes and the App Proxy (B6), with a dev-store
   credential only.
3. Live model evaluation (B5) with a development key.
4. Theme widget migration (B1).
5. Odoo contract, manufacturing acceptance and checkout enforcement design (B2, B3, B4).
6. Shared-database review, then deletion enablement, then retention approval (B9, B10, B11).
7. Credential rotation (B12), reference-data run (B13), staging smoke (B14).
