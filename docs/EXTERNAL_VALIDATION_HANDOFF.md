# External validation handoff (Phase 9)

The next steps that cannot be performed inside this repository on a workstation. **None of them
has been executed.** Each needs its own explicit authorization. Nothing here is a deployment plan.

## 1. Push for hosted CI (B7)

Read-only inspection performed on 2026-09-22 (`git ls-remote`, `git fetch`, unauthenticated
GitHub API; no push, nothing changed):

| Fact | Finding |
|---|---|
| Remote | `origin` = `https://github.com/rahealiduabrand/dua-scent-ai-python.git` (no credential in the URL; macOS keychain helper) |
| Remote branches | only `refs/heads/main` at `9416bd3`; **no remote `security-hardening`**, so a push creates the branch (normal, non-force) and cannot overwrite anything; local `main` == `origin/main` == the branch base, no divergence |
| Workflows on the remote | none on `origin/main` (no `.github/` at all); the remote has no workflow that could run on `workflow_run`, `deployment` or `pull_request_target` today. A push of this branch adds `ci.yml` (jobs `lint-and-audit`, `test`, `image`; triggers `push`, `pull_request`) and `live-eval.yml` (`workflow_dispatch` only) on that branch |
| Remote deployment files | `render.yaml` on `origin/main` is the OLD native blueprint (`runtime: python`, default auto-deploy) |
| Repository visibility | private (unauthenticated API: 404); webhooks endpoint requires authentication (401) |
| Repository webhooks / connected services | **UNKNOWN**: no `gh`, no GitHub or Render API token in this environment; not searched elsewhere |
| Render services linked to the repository, tracked branch, auto-deploy, preview deployments | **UNKNOWN** (same reason) |

The operator must verify, in the GitHub repository settings and the Render dashboard, before any
push: (1) whether a Render (or other) service is linked to this repository; (2) its tracked branch
(`main` or `security-hardening`); (3) its auto-deploy setting (Render's default is deploy on
commit to the tracked branch); (4) whether pull-request previews are enabled; (5) which repository
webhooks exist and what they trigger. No secret value is needed for any of these.

| | |
|---|---|
| Intended repository | `github.com/rahealiduabrand/dua-scent-ai-python` |
| Source branch / commit | `security-hardening` at the B17 commit recorded in `docs/SECURITY_AUDIT.md` section 24 (`git rev-parse HEAD` immediately before pushing) |
| Remote target branch | `security-hardening` (new; normal non-force push: `git push origin security-hardening:security-hardening`) |
| Workflows expected to run | `ci.yml` jobs `lint-and-audit` (lint, deploy-consistency check, lock sync, audit), `test`, `image` (builds both images, runs the smoke on a runner-local docker); `live-eval.yml` does not run |
| Permissions | `contents: read` only; no secret referenced; `persist-credentials: false` |
| Expected deployment behaviour | none from the repository's workflows; from external services: **UNKNOWN until the five facts above are verified**. If a Render service tracks `main`, a push to `security-hardening` does not deploy it, but a later merge would. The corrected blueprint (`autoDeployTrigger: "off"`, Docker runtime) only governs services created or synced from it |
| Expected evidence | run URLs; green status; `deploy consistency ok` in the lint job; `IMAGE SMOKE PASSED` in the image job; the step summary listing the 43 deselected tests |
| Closes | B7 and the hosted half of B8/B17. Not B5, B6, B13, B14 |

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
