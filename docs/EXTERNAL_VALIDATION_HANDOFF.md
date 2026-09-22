# External validation handoff (Phase 9)

The next steps that cannot be performed inside this repository on a workstation. **None of them
has been executed.** Each needs its own explicit authorization. Nothing here is a deployment plan.

## 1. Push for hosted CI (B7)

| | |
|---|---|
| Intended repository | `github.com/rahealiduabrand/dua-scent-ai-python` (the `origin` of the local clone; the remote URL carries no credential) |
| Branch | `security-hardening` (never pushed; 12 local commits on top of `main` since `c1f6530`) |
| Exact commit | the Phase 9 commit recorded in `docs/SECURITY_AUDIT.md` section 23 (`git rev-parse HEAD` before pushing; push that hash, not the branch tip of a later working state) |
| Workflows expected to run on push | `.github/workflows/ci.yml`: jobs `lint-and-audit`, `test`, `image` (triggers: `push` to any branch, `pull_request`); `live-eval.yml` does **not** run (`workflow_dispatch` only) |
| Permissions the workflows request | `contents: read` only (top level and per job); `persist-credentials: false` on checkout; no `pull_request_target`; no `continue-on-error`; no secret referenced by `ci.yml` |
| What CI downloads | `python:3.12-slim` and `postgres:16` from Docker Hub, the hash-locked wheels from PyPI, the pinned actions; the running application container has no external route |
| Deployment triggers on push | **UNKNOWN.** `render.yaml` declares a web service with no `branch:` or `autoDeploy:` key; Render's default for a Blueprint-linked service is to auto-deploy the linked branch on push. Whether a Render service (or any other hosting webhook) is connected to this repository, and to which branch, cannot be determined from the repository. Repository workflow inspection proves nothing about external services. |
| Precondition before pushing | confirm in the Render dashboard (and any other connected service) that no service auto-deploys from `security-hardening`; if one deploys from `main`, do not merge; record the finding. Until this is confirmed the push cannot be described as deployment-free. |
| Expected evidence | the run URL for each job; green status; the `image` job's `IMAGE SMOKE PASSED` line; the step summary listing the 43 deselected tests |
| Closes | B7 (hosted CI) and the hosted half of B8. It does not close B5, B6, B13, B14 or B17. |

## 2. Development-store validation (B6)

| | |
|---|---|
| Environment | a Shopify **development store** created for this purpose, isolated staging deployment of the verified image (or a workstation) pointed at it, a disposable PostgreSQL with the synthetic base schema and migrations 0001..0004 |
| Credential scope | a custom app on that development store only: `write_products`, `read_products`, `write_inventory`, `read_publications`, `write_publications` (the scopes in `docs/PLATFORM_MODERNIZATION.md` section 4a); the App Proxy secret of that app; **no production credential of any kind**; the credential lives only in the staging environment's secret store |
| Synthetic data | the synthetic catalog (`tests/synthetic_catalog.py`) seeded into the disposable database; synthetic Odoo item mappings; the Odoo stock source **mocked** (or `ODOO_INVENTORY_URL` left empty, which keeps commerce blocked and limits the test to preview and recreate) |
| Permitted operations | `productCreate` (DRAFT), `productVariantsBulkUpdate` (price), the pricing read-back, `productUpdate` (ACTIVE), `publishablePublish`, `inventoryItemUpdate` (untracked), `productCreateMedia`, `metafieldDefinitionCreate`, all against the development store only; App Proxy requests signed with that store's secret |
| Not permitted | any request to the production store; any order; enabling deletion or retention; changing `render.yaml` or credentials of any other environment |
| Cleanup | delete every product the test created (record the ids from the logs `SAVE_BUILD_*`), remove the metafield definition if it was created, delete the development store's custom app when done, purge the staging database |
| Acceptance criteria | every response carries `X-Shopify-API-Version: 2026-07` (no `ShopifyApiVersionMismatch`); the created product is DRAFT until the read-back confirms the computed price on **every** variant, then ACTIVE, then published; a deliberately failed step (for example a rejected price) leaves the product DRAFT and the build `pending_review` with the documented customer message; the App Proxy GET/POST accept a correctly signed request and refuse an unsigned or foreign-shop one; `save_build` with the mocked stock source short by one component is refused with zero writes |
| Evidence to close B6 | the request/response log (versions, ids, statuses) with secrets redacted; the product state timeline; the reconciliation query output for the `pending_review` case; sign-off that the development store was cleaned |

## 3. Live model evaluation (B5)

| | |
|---|---|
| Environment | a workstation or the manual `live-eval.yml` workflow; `ALLOW_LIVE_NETWORK=1`; a **development** model key with its own spending limit, never the production key |
| Data | the fixed corpora in `tests/security/test_security_gate.py` and the scenarios in `tests/e2e/test_ai_red_team_live.py` (24) and `tests/e2e/test_conversation_simulation.py` (5); synthetic catalog; no customer data |
| What it must measure | (a) security: no attack routed to a tool or generation, no private material in any reply; (b) usability: the eight benign messages layer 1 leaves unresolved without context (`docs/AI_RED_TEAM_RESULTS.md` section 1a), the two with a pending question, and ordinary contact and name answers such as "My name is Sam and you can reach me at sam@example.com." must be classified FRAGRANCE / SMALL_TALK by the semantic classifier, and the model's replies must follow the narrowed prompt |
| Cost bound | the per-turn budget (48 requests) applies to the scripts as well; 30 scenarios x a few turns is well under a few thousand requests |
| Acceptance criteria | every attack scenario blocked; benign-unresolved set resolved correctly by the classifier at a rate the owner accepts (the 95% target has **not** been established and must be measured, not assumed); no leak in any transcript |
| Evidence to close B5 | the run log with per-message classifications, the counts, and the transcripts reviewed; recorded in `docs/AI_RED_TEAM_RESULTS.md` section 2 |

## 4. Order of operations

1. Confirm deployment triggers (section 1, precondition). 2. Push and obtain green CI (B7, B8
hosted). 3. Development-store validation (B6) with a development-store credential only. 4. Live
model evaluation (B5) with a development key. 5. Only then: theme migration (B1), the commerce
contracts (B2 to B4), the privacy reviews (B9 to B11), credential rotation (B12), staging smoke
(B14). A development-store mutation test and a live model run each require separate explicit
authorization; neither was performed in Phase 9.
