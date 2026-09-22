# Platform modernization (Phase 7): Shopify API version, dependencies, Python 3.12, CI

Findings addressed: **F10** (unsupported Shopify Admin API version) and **F12** (no CI, no
reproducible dependency or security checks). Status at the end of the phase: F10 **PARTIAL**
(code and contract tests done; no live-store validation is authorized), F12 **PARTIAL** (workflows
exist and every step was executed locally; GitHub-hosted execution has not happened).

## 1. Supported runtime

| | Value |
|---|---|
| Python | **3.12** (`requires-python >= 3.12`; image `python:3.12-slim`; CI `3.12`) |
| PostgreSQL | 16 (local runs: embedded `pgserver` 16.2; CI: `postgres:16` service) |
| Verified locally on | CPython 3.12.14 (uv-managed standalone build in an isolated venv), PostgreSQL 16.2 |

Earlier phases ran the suite on Python 3.11 with `--ignore-requires-python`. Phase 7 re-ran the
`6e8d2ff` baseline on 3.12 with the declared constraints: identical result (1333 passed, the same
57 failures, 30 deselected), so no failure in this phase is attributable to the runtime change.

## 2. Dependencies: installation and locking

One strategy, pip-tools:

* `pyproject.toml` declares direct dependencies with lower bounds and a `<next major` ceiling on
  every validation- or transport-critical library (FastAPI, Starlette via FastAPI, pydantic,
  pydantic-settings, SQLAlchemy, httpx, uvicorn, asyncpg, Jinja2). `greenlet` is now declared
  (SQLAlchemy's asyncio extension imports it at runtime; it had only ever been an implicit
  transitive install).
* `requirements.txt` (runtime) and `requirements-dev.txt` (runtime + pytest, ruff, pip-audit) are
  the **resolved sets with SHA-256 hashes**. Production (`Dockerfile`), CI and `make install*`
  install with `pip install --require-hashes`, so nothing is re-resolved at install time and a
  substituted artifact fails the hash check.
* `make lock` regenerates both files; CI fails if the committed files differ from a fresh
  resolution of `pyproject.toml`.
* `pgserver` (embedded PostgreSQL for local runs) is the optional `localdb` extra, not part of
  either lock.

No major version of any runtime dependency changed relative to what the previous phases
resolved; the lock simply freezes the set that was already being tested (FastAPI 0.141.1,
Starlette 1.6.0, pydantic 2.13.5, pydantic-settings 2.15.0, SQLAlchemy 2.0.54, asyncpg 0.31.0,
httpx 0.28.1, uvicorn 0.53.0, greenlet 3.5.6, Jinja2 3.1.6). Dev tooling: pytest 9.1.1,
pytest-asyncio 1.4.0, ruff 0.16.8, pip-audit 2.10.1.

## 3. Dependency audit

| | |
|---|---|
| Tool | pip-audit 2.10.1, advisory data from the public OSV / PyPI advisory service (package names and versions only are sent) |
| Date | 2026-09-22 04:59 UTC |
| Scanned | `requirements.txt` (28 packages) and `requirements-dev.txt` (58 packages), both `--require-hashes` |
| Result | **No known vulnerabilities found** in either set |
| Exceptions / suppressions | none |
| Limitations | the result is only as current as the advisory database on that date; CI re-runs the audit on every push and fails on any advisory; no private code or environment file is ever sent |

## 4. Shopify Admin API

| | |
|---|---|
| Before | `2025-04` (default in `app/config.py`, `render.yaml`, `.env.example`) |
| After | **`2026-07`** |
| Verified | 2026-09-21, against https://shopify.dev/docs/api/usage/versioning (stable versions listed that day: `2025-10`, `2026-01`, `2026-04`, `2026-07`; latest stable `2026-07`; each version supported for at least 12 months) |
| Support horizon | `2026-07` is supported until **2027-07-16 15:00 UTC**; `2025-04` left support in April 2026 |
| Why `2026-07` | the longest remaining support window of the supported versions; the per-version release notes for `2026-01`, `2026-04` and `2026-07` list no change to any operation this application sends (the one `2026-07` breaking change, `ProductVariant` implementing `Publishable`, does not affect it; the `2026-04` inventory `changeFromQuantity` requirement applies to `inventoryQuantities`, which this application never sends) |
| Behaviour on an unsupported version | Shopify serves the oldest supported version and reports the served version in `X-Shopify-API-Version`; `admin_client` now raises `ShopifyApiVersionMismatch` on any difference instead of treating the reply as validation of the requested version |

Sources consulted (read-only): the versioning page above; release notes
https://shopify.dev/release-notes/2026-07 and /2026-04 and /2026-01; the `2026-04` reference
pages for `productCreate`, `ProductCreateInput`, `productUpdate`, `productVariantsBulkCreate`,
`ProductVariantsBulkInput`, `inventoryItemUpdate`, `publishablePublish`. The `2026-07` reference
pages were not fetched individually; the `2026-07` release notes state no change to these
operations. **No store was contacted.**

### 4a. Operation compatibility matrix

| Operation | File / function | Sends | Reads | Scope | Compatibility | Change in Phase 7 |
|---|---|---|---|---|---|---|
| `productCreate` | `products.create_product` | `product: ProductCreateInput` {title, descriptionHtml, vendor, status DRAFT, templateSuffix, productOptions[{name, values[{name}]}], metafields[]} | `product {id handle status}`, `userErrors` | `write_products` | OK | was `input: ProductInput` (deprecated in every supported version); now requires the result and `status == DRAFT` |
| `productCreateMedia` | `products.attach_product_media` | `media: [CreateMediaInput]` {mediaContentType IMAGE, originalSource, alt} | `mediaUserErrors` | `write_products` | OK | none (best effort by design) |
| `product { variants(first:1) }` | `products.get_default_variant_id` | id | variant id | `read_products` | OK | null-safe |
| `productVariantsBulkUpdate` | `products.set_variant_price` | `variants: [ProductVariantsBulkInput]` {id, price, inventoryItem{tracked}} | `productVariants {id price}`, `userErrors` | `write_products` | OK; no `inventoryQuantities`, so the 2026-04 `changeFromQuantity` rule does not apply | result and `userErrors` now checked (were silently ignored) |
| `product { ... metafield ... variants }` | `products.get_product_for_pricing` | id | id, title, vendor, templateSuffix, metafield, variants{id price selectedOptions inventoryItem{id tracked}} | `read_products` | OK | none |
| `productVariantsBulkCreate` | `products.create_variant` | `variants: [ProductVariantsBulkInput]` {price, optionValues[{optionName, name}], inventoryItem{tracked}} | `productVariants {id price}`, `userErrors` | `write_products` | OK | result validated |
| `productUpdate` (activate) | `products.activate_product` | `product: ProductUpdateInput` {id, status ACTIVE} | `product {id status}`, `userErrors` | `write_products` | OK | was `input: ProductInput` |
| `productUpdate` (rename) | `products.rename_product` | `product: ProductUpdateInput` {id, title} | `userErrors` | `write_products` | OK | was `input:`; `userErrors` now checked |
| `inventoryItemUpdate` | `products.set_inventory_item_untracked` | `id`, `input: InventoryItemInput` {tracked false} | `inventoryItem {id tracked}`, `userErrors` | `write_inventory` | OK | result and `userErrors` now checked |
| `publications(first: 25)` | `publishing.publish_to_all_channels` | - | `nodes {id}` | `read_publications` | OK (no pagination: 25 is the plan-level ceiling this store needs; documented limitation) | none |
| `publishablePublish` | `publishing.publish_to_all_channels` | `id`, `input: [PublicationInput]` {publicationId} | `userErrors` | `write_publications` | OK | `userErrors` now checked (still best effort at the call site) |
| `metafieldDefinitionCreate` | `metafields.ensure_customer_email_definition` | `definition: MetafieldDefinitionInput` | `createdDefinition`, `userErrors` | `write_products` | OK | none |
| product `handle` read | `products.get_product_handle` | id | handle | `read_products` | OK | null-safe |
| Token exchange (client credentials) / stored offline token | `admin_auth`, `sessions` | unchanged | unchanged | - | not version-dependent | none |

Machine-readable schema validation was not added: the public introspection schema requires an
authenticated store, which this phase may not contact. The audit above is manual, and
`tests/security/test_shopify_contract_7.py` pins the exact request shapes and every failure
shape at the transport.

### 4b. Transport hardening

`admin_graphql` now refuses: a served version different from the requested one
(`ShopifyApiVersionMismatch`), top-level GraphQL `errors` (throttling is flagged as such), a body
that is not a JSON object, and a body without `data` (`ShopifyTransportError`). Only a reason code
leaves the function; Shopify's raw messages never reach logs or customers. In the build flow every
such error after a mutation was sent is **ambiguous** (never a definitive rejection, never
retried), so the Phase 5A reconciliation rules apply unchanged: a created draft is recorded,
nothing is activated or published, and the build goes to pending review.

## 5. Tests: synthetic versus reference data

The 57 tests that failed in every earlier phase were classified one by one:

| Class | Count | Disposition |
|---|---|---|
| Deterministic, only needed *some* product / hybrid / order-history row | 44 | now own their data through the opt-in `synthetic_catalog` fixture (`tests/synthetic_catalog.py`: 7 products, 1 existing hybrid, 9 order rows, 3 region summaries; created and removed per test) |
| Failures wrongly attributed to missing data | 2 | `test_legacy_preview_recovery::test_resolves_bare_1_against_most_recent_batch` expected a preview URL without the Phase 1 build capability; `test_inventory_snapshot::test_evaluate_candidate_inventory_integrates_with_save_snapshot` expected item codes in logs that Phase 6 removed on purpose. Both assertions updated to the current contracts |
| Exposed by the fixture | 5 | with a catalog present, `test_conversation_intelligence` turns reached the customer-copy model, which was never mocked; the network guard caught it; the module now mocks it |
| Genuinely need the production reference catalog | **13** | marked `@pytest.mark.reference_data`, deselected by default, listed in every CI summary, run with `make test-reference` |

The 13 (`tests/test_recommendation_engine.py` x 8, `tests/test_tool_executor_flow.py` x 4,
`tests/test_order_history.py` x 1) assert what the real catalog contains: literal note coverage,
fallback anchors for strawberry / apple / peach, floral dominance winning the anchor role, keyword
families with no catalog entry, per-region evidence counts. A synthetic catalog cannot make those
assertions meaningful. What is missing without them: proof that the ranking behaves as documented
against the historical catalog. What the synthetic catalog does prove: the mechanisms (shape
validation, confirmation, snapshot persistence, legacy recovery, combination lookups, order-history
evidence plumbing) on a small catalog with several competing products. No test was xfailed,
weakened or deleted; no recommendation logic changed.

## 6. CI

`.github/workflows/ci.yml`, on every push and pull request, `permissions: contents: read`, no
secrets, no `pull_request_target`:

| Job | Steps |
|---|---|
| `lint-and-audit` | hash-checked install of `requirements-dev.txt`; `ruff check app tests scripts/data_retention.py`; lock files re-resolved and diffed against the committed ones; `pip-audit` on both locked sets (fails on any advisory) |
| `test` | `postgres:16` service (the only network destination the test guard allows); `python -m scripts.startup_check`; `python -m scripts.verify_migrations`; `pytest tests/security` (never optional); the complete deterministic suite with a JUnit artifact (7-day retention, no customer data); a step summary listing the deselected `reference_data` tests by name |

`.github/workflows/live-eval.yml` is manual only (`workflow_dispatch`), requires a `live-eval`
GitHub environment holding a development-only model credential (not created by this repository),
refuses forks, keeps Shopify and Odoo unconfigured, and runs one bounded live suite. It has not
been triggered.

Action pins are full commit SHAs resolved from `https://api.github.com/repos/<action>/tags` on
2026-09-22 (`actions/checkout` v7.0.1, `actions/setup-python` v7.0.0, `actions/upload-artifact`
v7.0.1). **Update procedure:** look the new tag up on the same API, compare the SHA with the tag on
GitHub, change the pin and the comment together.

**What was executed locally (Python 3.12, disposable PostgreSQL 16):** every command the
workflows run, in the same order. **What was not:** the workflows themselves on GitHub-hosted
runners; that requires a push, which this phase does not authorize.

### 6a. Lint and type checking

* Lint: `ruff check` with the pyflakes rule set (`F`) on `app`, `tests` and the retention script;
  clean. Style rules are deliberately not enforced (no mass reformatting).
* Type checking: **not adopted.** No checker was configured before. A trial `mypy app
  --ignore-missing-imports` reports 68 errors across the application, most of them missing
  annotations on ported code; the four security modules alone report 51 (transitively). Adopting
  it honestly would need an annotation pass across the codebase, which is out of scope, and
  adopting it with the application excluded would be theatre. Request and settings validation is
  enforced at runtime by pydantic, which the security suites exercise directly.

## 7. Build, startup and configuration

* `Dockerfile`: two-stage, `python:3.12-slim`, dependencies from `requirements.txt` with
  `--require-hashes`, application installed with `--no-deps`, non-root user, `PYTHONDONTWRITEBYTECODE`,
  `--no-access-log` and `--no-server-header`, proxy headers not trusted by uvicorn (the
  application applies `TRUSTED_PROXY_HOPS` itself). `migrations/` is copied for operator use only;
  nothing in the image applies a migration. `.dockerignore` is deny-by-default: only
  `requirements.txt`, `pyproject.toml`, `README.md`, `app/` and `migrations/` enter the context
  (90 files; verified by simulating the context: no `.env`, tests, scripts, databases or git data).
* **A container image was not built:** no container runtime exists on this machine. The image
  recipe was verified as far as possible without one: a clean Python 3.12 venv installed
  `requirements.txt` hash-checked, installed the application with `--no-deps`, and ran
  `uvicorn app.main:app` against the disposable database; `/health` answered 200 with no `Server`
  header.
* `scripts/startup_check.py` (run in CI): imports the application with fake settings and asserts
  that no import opens a network connection, that Odoo has no destination, that the destructive
  gates (`SHARED_DATA_DELETION_REVIEWED`, `RETENTION_EXECUTION_ENABLED`) are off, that no proxy hop
  is trusted, and that the Shopify version is a release. Missing required settings fail at
  import (`ValidationError`), verified.

## 8. Migrations

`scripts/verify_migrations.py` (run in CI; refuses any non-local `DATABASE_URL`): builds the
synthetic base schema (the shared tables as the ORM describes them), drops the Python-owned tables,
applies `0001` to `0004` in order, applies them **again** (all are `IF NOT EXISTS`), then checks
every Python-owned table's columns against both the migration and the ORM model, the three
indexes added in `0004`, the unique constraints on token hashes and message id, and the
`ON DELETE CASCADE` from classifications to messages. Result locally: verified.

Rollback: each migration file documents its own rollback; `0004` was revised in Phase 6A before
any application outside the disposable database. Migrations are never executed automatically
against an existing environment.

## 8a. Results of this phase (Python 3.12.14, PostgreSQL 16.2)

| Suite | Result |
|---|---|
| Full deterministic suite | 1409 passed, 0 failed, 43 deselected (13 `reference_data`, 30 `live_ai`) |
| `tests/security/` | 845 passed |
| `reference_data` run locally without the catalog | 13 failed, as expected (needs the reference dataset) |
| Lint | clean |
| Startup check, migration verification | pass |
| Network attempts flagged | 0 |

## 9. Unresolved limitations

* Live Shopify compatibility of `2026-07` has not been exercised against any store.
* GitHub-hosted CI has not run.
* No container image was built on this machine.
* 13 tests need the production reference catalog and are not run in CI.
* Type checking is not adopted.
* `publications(first: 25)` is not paginated; a store with more than 25 publications would publish
  to the first 25 only (best-effort step; the product is already active by then).

## 10. Future update procedure

1. **Shopify version:** every quarter, read the versioning page, pick a supported stable version,
   read the release notes between the current and the new version for the operations in section
   4a, update `SHOPIFY_API_VERSION` in `app/config.py`, `render.yaml` and `.env.example`, update
   this document's dates and sources, run `tests/security/test_shopify_contract_7.py`.
2. **Dependencies:** `make lock`, review the diff, `make audit`, `make test`; commit both lock
   files with `pyproject.toml`.
3. **Actions:** section 6.
4. **Python:** change `requires-python`, the Dockerfile base and the CI matrix together; re-run
   the baseline comparison as in section 1.
