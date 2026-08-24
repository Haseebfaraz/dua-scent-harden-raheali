# Migration Audit — JS Reference → Python (dua-scent-ai-python)

The JS/Remix repository (`shop-chat-agent-python`, kept as-is) is the **read-only behavioral
reference** for this port: algorithms, scoring constants, Prisma schema, Odoo semantics,
conversation behavior, and its Vitest suite are the source of truth. This document is the
file-by-file map plus the parity table requested for this migration; a more narratively detailed
version of the same audit (written during the in-repo prototyping phase) lives at
`docs/PYTHON_MIGRATION_AUDIT.md` in the JS reference repo and is linked from each section below
where useful.

Business model is fixed and not revisited: blend-builder / custom Hybrid-Tribrid-Quadbrid
creation from real catalog components, never a plain catalog-recommendation flow.

## 1–8: Per-module mapping

### Fragrance utilities (`app/fragrance/`)

| JS file (reference repo) | Python replacement | Functions ported | Dependencies | JS tests (regression spec) |
|---|---|---|---|---|
| `app/utils/fragranceNormalization.js` | `normalization.py` | `normalizeProductName`, `normalizeRegionText`, `resolveLocationInput`, `correctPreferenceVocabulary(List)`, `SEASON_ALIASES` | stdlib `re`, `unicodedata` | `fragranceNormalization.test.js` |
| `app/utils/fragranceVocabulary.js` | `vocabulary.py` | `directionForRole`, `pickWords` (same JS-style hash for determinism), `describeCharacter`, `DIRECTION_VOCABULARY` | none | `fragranceVocabulary.test.js` |
| `app/utils/fragranceCompatibility.js` | `compatibility.py` | `PREFERENCE_FAMILIES`/`COMPATIBILITY_TAGS`, `detectFamilies`, `textToPreferenceFamilies`, `splitDislikesByExactness`, risk rules (`RISK_RULES`, `assessCombinationRisk(Details)`, `groupAndPenalizeRisks`), `interpretCustomerPreferences`, `interpretLifestyleContext`, literal-note helpers | `normalization.py` | `fragranceCompatibility.test.js` |
| `app/utils/fragranceScoring.js` | `scoring.py` | `SCORE_WEIGHTS`, `classifyDislikeConflict`, `matchedLikes`, `likeMatchStrength`, `computeEvidenceLevel` | `compatibility.py` | `fragranceScoring.test.js` |
| `app/utils/notePositionMapping.js` | `note_positions.py` | `classifyNote`, `assignNotePositions` | `compatibility.py` | `notePositionMapping.test.js` |
| `app/utils/weatherSeason.js` | `weather.py` | `getCalendarSeason`, `describeWeatherSimple`, `deriveWeatherDirection`, `weatherDirectionToQuerySeason`, `hasSeasonWeatherConflict` | stdlib `datetime` | `weatherSeason.test.js` |
| `app/utils/combinationKey.js` | `combination_key.py` | `createCombinationKey` | `normalization.py` | `combinationKey.test.js` |
| `app/utils/recommendationSelectionParser.js` | `selection_parser.py` | `parseRecommendationSelection` (+ Levenshtein-1 fuzzy match) | none | `recommendationSelectionParser.test.js` |
| `app/services/fragranceFormula.server.js` | `formulas.py` | `computeAlcoholMl`, `computeRequiredOilMl`, `buildProductionFormula`, `computeComponentCapacity`, `computeFeasibility` | none | `fragranceFormula.test.js` |

**Scoring constants preserved verbatim** (do not "improve" these — see the JS reference repo's
own audit for why each one exists): `SCORE_WEIGHTS` (sameCity 5 / sameCountry 4 /
sameStateRegionOrClimate 3 / sameSeason 4 / matchesLike 5 / conflictsDislike -10 /
repeatPurchaseBySimilarCustomer 3 / popularAmongSimilarCustomers 2), `EXACT_NOTE_COVERAGE_TIERS`
[10, 7, 5], `FAMILY_BREADTH_BONUS_FROM_SECOND` [6, 3], `RISK_SEVERITY_PENALTY` (advisory -1 / low
-2 / medium -5 / high -10 / critical -10), `MAX_HISTORY_SCORE` 6, `TYPE_SIMPLICITY_SCORE`
(non-sensitive HYBRID 10 / TRIBRID -6 / QUADBRID -16; sensitive HYBRID 14 / TRIBRID -10 / QUADBRID
-22), `SWEET_HEAVY_MAX_PERCENT` 35, `NEAR_DUPLICATE_OVERLAP_RATIO` 0.5. These live split across
three JS files in the reference (`fragranceScoring.js`, `fragranceCompatibility.js`,
`recommendationEngine.server.js`) — the Python port keeps them in the equivalent three modules
(`scoring.py`, `compatibility.py`, `services/recommendation_engine.py`) rather than consolidating,
so a future audit can still diff module-for-module against the reference.

### Services (`app/services/`)

| JS file | Python replacement | DB models used | Migration status |
|---|---|---|---|
| `customerProfile.server.js` | `customer_profile.py` | `CustomerProfileState` | done |
| `locationVerification.server.js` | `location_verification.py` | `OrderHistory` (read) | done |
| `orderHistoryAnalysis.server.js` | `order_history.py` | `OrderHistory`, `ProductRegionSummary`, `FragranceProduct` | done |
| `productCatalog.server.js` | `product_catalog.py` | `FragranceProduct`, `ExistingCombination` | done |
| `combinationAnalysis.server.js` | `combination_analysis.py` | `ExistingCombination`, `FragranceProduct` | done (read-only lookups only — NOT the generator, see below) |
| `recommendationEngine.server.js` | `recommendation_engine.py` | `FragranceProduct`, `ExistingCombination` | done (the ~1300-line core: anchors, roles, ratios, all scoring dimensions, confidence, diversity selection) |
| `recommendationConfirmation.server.js` | `recommendation_confirmation.py` | `FragranceRecommendation` | done |
| `fragranceCopyGeneration.server.js` | `copy_generation.py` | none (in-memory only) | done |
| `odooClient.server.js` | `integrations/odoo_client.py` | none (HTTP only) | done |
| `odooInventory.server.js` | `odoo_inventory.py` (+ `evaluate_candidate_inventory`, pulled forward from the JS tool-orchestration file since it's inventory logic) | `OdooOilMapping`, `FragranceProduct` | done |
| `recommendationInventorySnapshot.server.js` | `inventory_snapshot.py` | `RecommendationInventorySnapshot`, `RecommendationInventoryComponent` | done |
| `legacyPreviewRecovery.server.js` | `legacy_preview_recovery.py` | `FragranceRecommendation` | done |
| (conversation helpers in `db.server.js`) | `conversation.py` | `Conversation`, `Message` | done |
| `shopDomain.server.js` | *(stays in Node — reads the Shopify `Session` table Python has no access to; `shop_domain` is now an explicit parameter Node passes in)* | — | N/A (boundary decision) |

**Important correction versus the original task brief**: `combinationAnalysis.server.js` is a
110-line read-only `ExistingCombination` lookup module, not the Hybrid/Tribrid/Quadbrid
*generator* — that logic lives entirely in `recommendationEngine.server.js` (anchor selection,
role assignment, ratio math, dedup) plus the auto-select walk in the JS tool-orchestration file.
Scope this correctly if re-estimating remaining work.

### AI orchestration (`app/ai/`)

| JS file/section | Python replacement | Notes |
|---|---|---|
| `chat.jsx`'s `callOpenAIOnce` | `openai_client.py` | Raw `httpx` POST (matches the JS reference's raw `fetch`, not the OpenAI SDK — no SDK dependency needed) |
| `chat.jsx`'s `buildSystemPrompt` + helpers | `prompt.py` | Byte-faithful prompt text, including the early-phase fragrance-bridge gate (`hasConcreteContext`) |
| `chat.jsx`'s `callAI` | `conversation_flow.py` | The 6-turn tool-resolution loop, conversation rehydration |
| `fragranceAgentTools.server.js`'s tool schemas | `tools.py` | All 13 OpenAI function-calling tool definitions + argument validation |
| `fragranceAgentTools.server.js`'s `executeFragranceTool`, `evaluateAutoConfirmEligibility`, `autoSelectAndConfirmBest` | `tool_executor.py` | The dispatcher and the ranked auto-select-and-confirm walk |
| `fragranceAgentTools.server.js`'s `deriveRefinementAdjustments` | `refinement.py` | Refinement-feedback → profile-bias parsing |
| `previewUrl.server.js` | `preview_url.py` | Takes `shop_domain` as an explicit parameter (see boundary note above) |

### SSE events (unchanged contract)

`id`, `profile_progress`, `analysis_progress`, `candidate_products`, `combination_recommendations`,
`recommendation_selected`, `preview_ready`, `chunk`, `message_complete`, `end_turn`, `error` — all
confirmed as real, currently-emitted events by reading the JS reference's tool dispatcher in full
(not just `chat.jsx` alone, where a couple of these are only named in a comment).

**Widget consumption note** (found during the Integration & Parity Gate, see §9): the storefront
widget (`extensions/chat-bubble/assets/chat.js`) only has `switch` cases for `id`, `chunk`,
`message_complete`, `end_turn`, `error`, `rate_limit_exceeded`, `preview_ready` (handled specially,
checked before the switch), `auth_required`, `product_results`, `combination_recommendations`,
`recommendation_refined`, `tool_use`, `new_message`, `content_block_complete`. It has **no case**
for `profile_progress`, `analysis_progress`, `candidate_products`, or `recommendation_selected` —
they are real events the tool dispatcher emits, but the widget silently no-ops on them today. This
is pre-existing JS behavior, not something the Python port changed or should "fix" unasked; noted
here so a future SSE contract change doesn't mistake "widget ignores it" for "event doesn't exist."

### Environment variables

See `.env.example` for the full list. Departures from the original generic template worth calling
out: Odoo auth here is a bearer-token REST call against one fixed URL
(`ODOO_PING_URL`/`ODOO_INVENTORY_URL`/`ODOO_INVENTORY_API_KEY`), not a `ODOO_URL` +
`ODOO_DATABASE` + `ODOO_USERNAME` + `ODOO_PASSWORD` XML-RPC connection — the latter shape doesn't
match how the real integration actually authenticates, confirmed against the reference
implementation. `INTERNAL_API_KEY` (header `X-Internal-Api-Key`) is a new boundary that didn't
exist in the JS-only architecture — it secures the Node → Python service-to-service hop.

### Database strategy

No schema changes. SQLAlchemy models in `app/db/models/` mirror the existing Postgres schema
exactly (verified against real row counts during development: `FragranceProduct` 3,450,
`ExistingCombination` 432, `OrderHistory` 936,819, `OdooOilMapping` 2,890,
`ProductRegionSummary` 156,362). Real foreign keys are declared only where Prisma has them
(`Message`→`Conversation`, and the inventory-snapshot chain) — every other cross-model reference
(`conversationId`, `fragranceProductId`, `normalizedProductName`, ...) is a plain string/UUID
column, matching Prisma's own logical-only relations, so Python never becomes silently stricter
than the production data actually is.

## Parity table

| Module | JS Reference | Python Tests | Output Parity | Production Ready |
|---|---|---|---|---|
| Normalization | `fragranceNormalization.js` | `test_normalization.py` | Verified vs. real fixtures | Yes |
| Vocabulary | `fragranceVocabulary.js` | `test_vocabulary.py` | Verified vs. real fixtures | Yes |
| Compatibility | `fragranceCompatibility.js` | `test_compatibility.py` | Verified vs. real fixtures | Yes |
| Scoring | `fragranceScoring.js` | `test_scoring.py` | Verified vs. real fixtures | Yes |
| Note positions | `notePositionMapping.js` | `test_note_positions.py` | Verified vs. real fixtures | Yes |
| Weather/season | `weatherSeason.js` | `test_weather.py` | Verified vs. real fixtures | Yes |
| Combination key | `combinationKey.js` | `test_combination_key.py` | Verified vs. real fixtures | Yes |
| Selection parser | `recommendationSelectionParser.js` | `test_selection_parser.py` | Verified vs. real fixtures | Yes |
| Fragrance formulas | `fragranceFormula.server.js` | `test_formulas.py` | Verified vs. real fixtures | Yes |
| Customer Profile | `customerProfile.server.js` | `test_customer_profile.py` | Verified against live shared DB | Yes |
| Location Verification | `locationVerification.server.js` | `test_location_verification.py` | Verified against live shared DB (geocoding HTTP mocked) | Yes |
| Product Catalog | `productCatalog.server.js` | `test_product_catalog.py` | Verified against live shared DB | Yes |
| Order History | `orderHistoryAnalysis.server.js` | `test_order_history.py` | Verified against live shared DB (936k real rows) | Yes |
| Combination Analysis | `combinationAnalysis.server.js` | `test_combination_analysis.py` | Verified against live shared DB | Yes |
| Recommendation/Combination Engine | `recommendationEngine.server.js` | `test_recommendation_engine.py` | 55/55 passed against real production data on first full run | Yes |
| Recommendation Confirmation | `recommendationConfirmation.server.js` | `test_recommendation_confirmation.py` | Verified against live shared DB | Yes |
| Copy Generation | `fragranceCopyGeneration.server.js` | `test_copy_generation.py` | Verified (OpenAI HTTP mocked; guardrail branching is the thing under test) | Yes |
| Odoo Client + Inventory | `odooClient.server.js`, `odooInventory.server.js` | `test_odoo_inventory.py` | Verified (Odoo HTTP mocked, DB mappings real) | Yes |
| Inventory Snapshot | `recommendationInventorySnapshot.server.js` | `test_inventory_snapshot.py` | Verified against live shared DB, incl. a `caplog`-based secret-leak check | Yes |
| Legacy Preview Recovery | `legacyPreviewRecovery.server.js` | `test_legacy_preview_recovery.py` | Verified against live shared DB | Yes |
| Tool Executor / Auto-Confirm | `fragranceAgentTools.server.js` | `test_tool_executor_*.py` | Verified against live shared DB; found and fixed a real SQLAlchemy transaction-rollback bug during this pass | Yes |
| Refinement Adjustments | `fragranceAgentTools.server.js` (`deriveRefinementAdjustments`) | `test_refinement.py` | Verified vs. real fixtures | Yes |
| Prompt / Early-Phase Gate | `chat.jsx` | `test_prompt.py` | Verified vs. real fixtures | Yes |
| Chat/SSE (FastAPI endpoint) | `chat.jsx` | `test_chat_api.py` | Verified via TestClient + one real live round trip (real OpenAI call, real DB persistence, through the actual Node proxy); Integration & Parity Gate (§9) re-confirmed the contract byte-for-byte and fixed two boundary gaps (timeouts, SSE headers) | Yes, pending Phase 9 and DB-backed re-verification |

**Phase 9 (genuine end-to-end against a live Shopify storefront — real customer login, real
theme, Save Build/Add to Cart)** has not been run by an agent in this environment; it requires a
live Shopify dev store and tunnel. Everything up to and including the Node↔Python HTTP boundary
has been proven with a real request/response round trip, not just mocks.

## 9. Integration & Parity Gate (2026-08-24)

Boundary review of the three uncommitted Node-adapter files plus a live contract comparison
between the committed (pre-Phase-8) `chat.jsx` — the last version that assembled SSE frames
itself — and the new FastAPI `/internal/chat`. Runtime code was treated as the source of truth
over any prior doc/comment claims.

**Adapter boundary — confirmed clean.** `chat.jsx` imports only `resolveShopDomain`; no
recommendation/scoring/OpenAI import exists in the file. `hasConcreteContext` is still exported
from it, but only so `chatFlow.test.js` keeps passing — it is dead code on the live request path
(the real gate is `app/ai/prompt.py`'s `has_concrete_context` in this repo). The admin dashboard
route `app.customers.$conversationId.jsx` reads persisted rows straight from Prisma for display;
the one hit on "recommendationEngine" there is a comment, not a call. No JS business logic
executes on the customer-facing path anymore.

**Contract comparison result: byte-level match**, with two real gaps found and fixed in this pass:

| Checklist item | Result |
|---|---|
| Request payload compatibility | Match — `ChatRequest` field-for-field vs. the JSON body `chat.jsx` sends |
| Conversation/session ID handling | Match — same `id` event / mint-if-absent semantics |
| Internal API-key auth | Match — `X-Internal-Api-Key` / `INTERNAL_API_KEY` both sides; `test_history_rejects_missing_or_wrong_internal_secret` and `test_post_chat_rejects_missing_internal_secret` pass |
| HTTP status/error propagation | Match — history loader forwards Python's real status; the POST action always returns 200 with an in-band `error` SSE frame on any failure, exactly like the JS original's own catch block |
| Timeouts | **Gap, fixed** — every Python outbound call (OpenAI/DB/Odoo/geocoding) already had an explicit timeout, but neither of Node's two `fetch()` calls to Python did. Added `signal: AbortSignal.timeout(45_000)` to both in `chat.jsx` |
| SSE headers | **Gap, fixed** — Node always set `Cache-Control: no-cache` / `Connection: keep-alive` on its own response to the widget (so the widget was never actually affected), but Python's `StreamingResponse` didn't set them on the Node→Python hop. Added explicitly in `app/api/chat.py` |
| SSE event names | Match — `id`/`chunk`/`message_complete`/`end_turn`/`error`/`preview_ready`/`profile_progress`/`analysis_progress`/`candidate_products`/`recommendation_selected`/`combination_recommendations`, verified against the JS tool dispatcher, not just `chat.jsx` |
| SSE payload shapes | Match — same camelCase keys preserved verbatim in Python's dict payloads (`missingFields`, `candidateProducts`, `recommendationId`, `previewUrl`, ...) since these are wire keys, not Python identifiers |
| Streaming/chunk behavior | Match — event order is identical: `id` → tool `sseEvents` in order → `chunk` → `message_complete` → `end_turn` |
| Client disconnect handling | Not independently tested (needs a live streamed client); both sides use standard async generators/ReadableStreams with no custom disconnect handling, same as the JS original |
| `preview_ready` | Match — `recommendationId`/`previewId`/`previewUrl` keys identical; the widget's `handlePreviewReady` special-case (checked before its type switch) needs no changes |
| Recommendation ID propagation | Match — internal ids never appear in model-facing reply text, only in structured SSE fields, in both implementations |
| Refinement/recreate behavior | Match at the code level (`refinement.py`, `legacy_preview_recovery.py`); full behavioral parity blocked on DB, see below |
| Persisted conversation/profile rehydration | Match at the code level; blocked on DB for a live-data check, see below |

**Non-DB test results**: 226 passed / 0 failed (after fixing one stale assertion in
`tests/test_health.py` that predated the `service` field being added to `/health`, and confirming
the SSE header addition doesn't break anything).

**DB-backed test results**: blocked. The shared Render Postgres instance was unreachable for the
entire duration of this session (`asyncpg.exceptions.ConnectionDoesNotExistError`), confirmed
external by an identical failure against the **JS reference repo's own Vitest suite** in the same
session (144 failures, all `PrismaClientInitializationError: Server has closed the connection` —
same root cause, not a Python-side regression). This blocks `test_customer_profile.py`,
`test_location_verification.py`, `test_product_catalog.py`, `test_order_history.py`,
`test_combination_analysis.py`, `test_recommendation_engine.py`, `test_recommendation_confirmation.py`,
`test_odoo_inventory.py`, `test_inventory_snapshot.py`, `test_legacy_preview_recovery.py`,
`test_tool_executor_flow.py`, `test_tool_executor_profile.py`, and two DB-touching cases inside
`test_chat_api.py` (`test_history_empty_for_unknown_conversation`,
`test_post_chat_streams_expected_sse_event_sequence`). All of these passed in the first full run
recorded earlier in this migration (see the parity table above) — this is a re-verification gap,
not a known regression. **Re-run this set the moment the DB is reachable again before treating
parity as re-confirmed.**

**End-to-end local test**: partially run. The full flow (profile update → candidate analysis →
Hybrid/Tribrid/Quadbrid generation → scoring → Odoo feasibility → persistence → `preview_ready` →
storefront preview page → Save Build/Add to Cart) needs the same live DB on both the Node and
Python sides (`resolveShopDomain()` itself is a Prisma read), so it could not be exercised while
the DB was down. What *was* verified live: a full `/internal/chat` request through FastAPI's real
ASGI stack (via `TestClient`, not mocks) correctly caught the DB outage mid-request and degraded to
a single in-band `error` SSE frame at HTTP 200 — no crash, no partial/malformed stream, no fallback
to any JS engine (there is no code path capable of that on the Python side).

**Failure-case results**:

| Case | Result |
|---|---|
| Database unavailable | **Live-verified.** Real `ConnectionDoesNotExistError` mid-request, caught by `chat_action`'s outer `try/except`, logged server-side with full traceback, single `error` SSE frame sent, HTTP 200. |
| OpenAI timeout/failure | Code-verified exact parity: `call_openai_once` returns `None` on `httpx.TimeoutException` or any other error (never raises, never logs the key), and `call_ai` returns the byte-identical fallback text `"Sorry, I'm having trouble reaching the fragrance engine right now."` that the JS reference (`chat.jsx` line 637, pre-Phase-8) also returns. |
| Invalid internal API key | **Live-verified** via `test_history_rejects_missing_or_wrong_internal_secret` / `test_post_chat_rejects_missing_internal_secret` — 401, no request processed. |
| Python service unavailable (from Node) | Code-verified: `chat.jsx`'s try/except around both `fetch()` calls degrades to an empty history (`{"messages": []}`) or a single `error` SSE frame — never a crash, never a call into any JS recommendation module. |
| Malformed Python response | Code-verified: Node passes Python's raw stream body straight through without parsing it; the widget's own per-line `JSON.parse` is wrapped in try/catch and skips unparseable lines without aborting the stream. |
| SSE stream interruption | Same mechanism as above — a truncated/malformed frame is skipped client-side, not fatal. |
| Odoo timeout / missing mapping / confirmed-insufficient inventory | Ported and previously verified in `test_odoo_inventory.py`/`test_tool_executor_auto_confirm.py` (WARN semantics: only `CONNECTED`+insufficient counts as confirmed-insufficient); blocked for re-verification by the same DB outage. |

**No silent fallback to the JS engine exists or was added.** Python has no import path to any JS
recommendation module (impossible across the process/language boundary), and Node's adapter has no
recommendation/scoring imports left after Phase 8 — every failure path returns either a real
partial-degradation message or a synthetic `error` event, never a call into `recommendationEngine.server.js`
or its siblings.

## Main migration risks (carried over, still relevant)

1. **Scoring constants are split across three files** (`fragranceScoring.js`/
   `fragranceCompatibility.js`/`recommendationEngine.server.js` in the reference) — treat all
   three as one scoring surface when auditing for drift, not just the file named "scoring."
2. **Odoo WARN semantics**: only a `CONNECTED` mapping with insufficient oil counts as
   confirmed-insufficient; `MISSING`/`SKU_NOT_FOUND`/`LOOKUP_FAILED` must never be treated as
   either confirmed-sufficient or confirmed-insufficient. Naively "fixing" this to a stricter
   check would break real production behavior.
3. **SQLAlchemy transaction state after a failed write** does not behave like Prisma — a failed
   `commit()`/flush poisons the session until an explicit `rollback()`. Already fixed once (in
   the inventory-snapshot "log and keep going" path); watch for the same pattern anywhere else a
   write is wrapped in try/except-and-continue.
4. **`ProductRegionSummary` is precomputed offline** — never derive it live from the 936k-row
   `OrderHistory` table on a chat request.
5. **`shop_domain` boundary** — this service cannot resolve the Shopify shop domain itself (no
   access to the `Session` table); it must always be supplied by the Node caller.
6. **Test suite runtime** — currently several minutes against the live shared Postgres instance
   with `NullPool` (required for pytest-asyncio's per-test event loop). Fine for correctness:
   revisit with a real connection pool once tests run against a local/CI database under one
   long-lived event loop.
7. **DB-backed re-verification is outstanding** — the Integration & Parity Gate (§9) ran while the
   shared Postgres instance was down; 13 DB-backed test files (previously all-passing) and the
   full profile→recommendation→Odoo→preview end-to-end flow still need one clean re-run against a
   reachable DB before staging deployment.

## 10. Staging Readiness Check (2026-08-24)

Attempted the full staging-readiness pass requested for this repo. The shared Render Postgres
instance was checked again at the start of this pass and was still unreachable (same
`ConnectionDoesNotExistError` as §9) — this is now confirmed down across two separate sessions, not
a brief blip; worth checking the instance's status directly in the Render dashboard (e.g. a
free-tier instance that's been suspended, or a credential/network change) rather than assuming it
will self-recover. Per instruction, no application code was changed to route around this, and
nothing DB-dependent below is marked passing on the strength of code review alone.

| Area | Status | Notes |
|---|---|---|
| Non-DB Python tests | PASS | 226/226 (unchanged since §9) |
| DB-backed Python tests (13 files) | BLOCKED | DB unreachable; last known-good run was 100% passing, needs re-run |
| JS reference DB-backed tests | BLOCKED | Same shared DB; 144 failures in §9 all traced to `PrismaClientInitializationError` |
| Full Python test suite | BLOCKED | Cannot run to completion without the DB |
| Prisma vs SQLAlchemy schema/model diff | NOT RUN | Needs a live DB round trip (write via one, read via the other) to confirm, not just static model comparison |
| Full local end-to-end flow (chat → preview_ready) | BLOCKED | Needs DB on both sides (`resolveShopDomain()` alone is a Prisma read) |
| Recommendation parity fixture sweep (preference/dislike/type/geography/season matrix) | BLOCKED | Needs the real catalog + order-history data in Postgres; the existing ported test suites (`test_recommendation_engine.py`, `test_order_history.py`, `test_compatibility.py`) already cover most of these dimensions with real fixtures and passed 100% in the last DB-reachable run, but a fresh fixture-by-fixture JS-vs-Python diff run was not possible this session |
| Odoo validation matrix | BLOCKED for the DB-touching cases | `test_odoo_inventory.py`/`test_tool_executor_auto_confirm.py` cover this exact matrix (WARN semantics, ratio-vs-oil-portion math) and passed previously; Odoo HTTP itself is mocked in these tests so an actual live-Odoo-timeout/auth-failure call was not separately exercised beyond what's mocked |
| Persistence compatibility (Conversation/Message/CustomerProfileState/FragranceRecommendation/RecommendationInventorySnapshot/RecommendationInventoryComponent) | BLOCKED | Needs a live write-then-read round trip; the earlier Phase 8 real round trip (before this repo's split) proved this once, but wasn't re-run here |
| Conversation rehydration across a process restart | NOT RUN | Needs DB |
| Node ↔ FastAPI integration/contract | PASS | Verified in §9 (this session only re-confirmed no regression since) |
| SSE parity | PASS | Verified in §9 |
| Secret scan (new repo) | PASS | No `.env`, keys, or credentials ever committed, in any commit — checked full history, not just HEAD |
| Fresh-clone/install/start/health check | PASS | Clean clone → fresh venv → `pip install -e ".[dev]"` → real `uvicorn app.main:app --host 0.0.0.0 --port $PORT` → `GET /health` → `{"status":"ok","service":"dua-scent-ai-python"}`, HTTP 200. No dependency on any dev-machine-only file. |
| Render config (`render.yaml`, `Dockerfile`, `.env.example`, `README.md`) | PASS (reviewed) | Matches the settings module field-for-field; `Dockerfile` reviewed but not container-built (Docker isn't available in this environment) |
| Live Shopify storefront integration (widget → App Proxy → Node → Python → SSE → widget) | BLOCKED — environment limitation, not a code gap | This environment has no Shopify dev store, Partner account, or tunnel; there is no supported way to exercise the real storefront here. This needs to be run from a machine with the actual Shopify CLI/dev-store/tunnel setup. |
| Save Build | BLOCKED | Depends on the live storefront integration above |
| Add to Cart | BLOCKED | Depends on the live storefront integration above |
| Failure chain through the full stack (Storefront → Node → Python) | BLOCKED for most cases | `resolveShopDomain()` in `chat.jsx` hits Postgres before Python is ever called, so almost every full-chain failure case needs the DB up; the Python-only failure paths (OpenAI timeout/malformed response, invalid `X-Internal-Api-Key`, a genuine DB outage caught mid-request) were live- or code-verified in §9 and still hold |

## 11. Full-standalone Shopify port (2026-08-24) — scope change

The Node app is no longer a permanent adapter; `dua-scent-ai-python` is becoming the complete
standalone backend, with `shop-chat-agent-python` as a read-only reference implementation only.
See the Phase A audit for the full remaining-responsibilities matrix, the authentication strategy
(Python reads the merchant's already-obtained offline `Session` token rather than reimplementing
OAuth), and the one open frontend decision (the preview page), now resolved: reproduce it as
Jinja2 + vanilla JS in FastAPI, not React.

### Phase 2 — Shopify integration foundation

| Node source | Python replacement | Status |
|---|---|---|
| `shopify.server.js` (config only) | `app/config.py` (Shopify settings added: `SHOPIFY_API_KEY`, `SHOPIFY_API_SECRET`, `SHOPIFY_APP_URL`, `SCOPES`, `SHOPIFY_API_VERSION`) | done |
| `PrismaSessionStorage` (read side) | `app/db/models.Session` (newly mirrored — previously deliberately excluded) + `app/shopify/sessions.py` | done |
| `authenticate.webhook`'s HMAC check | `app/shopify/hmac.py::verify_webhook_hmac` | done |
| `authenticate.public.appProxy`'s signature check | `app/shopify/hmac.py::verify_app_proxy_signature` + `app/shopify/app_proxy.py` (FastAPI dependency) | done |
| `admin.graphql(...)` | `app/shopify/admin_client.py::admin_graphql` (raw httpx, token from `Session`) | done |
| `api.webhooks.jsx` | `app/shopify/webhooks.py` (`POST /shopify/webhooks`, `app/uninstalled` only) | done |

**Finding**: `registerWebhooks` is exported by `shopify.server.js` but never called anywhere in the reference app, and `shopify.app.toml` has no `[[webhooks.subscriptions]]` block — there is no live webhook-registration behavior to port. Not built; would be manufactured scope.

### Phase 3 — Admin GraphQL product/metafield/publishing primitives

`app/shopify/products.py` (create product, attach media, get/set variant price, rename, get handle, get-product-for-pricing, create variant, untrack inventory item), `app/shopify/metafields.py` (note_composition/internal_components/customer-identity metafield builders, customer-email metafield definition), `app/shopify/publishing.py` (publish-to-all-channels). `app/services/fragrance_build.py` carries the pure/DB-read parts of `fragranceBuild.server.js` (note bucketing, default ratios, per-position $/5ml pricing) — kept out of `app/shopify/` since it has no HTTP in it, shared by Save Build and the preview route.

### Phase 4 — Save Build orchestration

`app/shopify/builds.py`: `create_shopify_build_product` (first-time creation, port of `fragranceBuild.server.js`) and `reprice_existing_build` (port of `api.save-build.jsx`'s ±3% tolerance-match/new-variant logic). `mark_recommendation_draft`/`mark_recommendation_saved` added to `recommendation_confirmation.py`.

### Phase 5 — Preview page migration

`apps.scent-library.fragrance-preview.jsx` (React/React-Router SSR + three.js) → `app/api/preview.py` (GET/POST, App-Proxy verified) + `app/templates/fragrance_preview.html` (Jinja2) + `app/static/css/fragrance_preview.css` (byte-identical stylesheet) + `app/static/js/fragrance_preview.js` (vanilla JS: `adjustRatios` slider redistribution ported verbatim, the three.js bottle ported near-verbatim, fetch-based recreate/save_build/add_to_cart submission with the same loading/error states). Same preview URL, same response shapes (`{status: "recreate"|"saved"|"added", ...}` / `{error}`), same visual design — no redesign.

**Packaging finding, fixed**: `pyproject.toml` had no `package-data` entry, so a real (non-editable) `pip install .` — what Render actually runs — would have silently shipped the app without `app/templates/` or `app/static/`, working locally under `-e` install but 404ing on Render. Added `[tool.setuptools.package-data]` and verified with a real (non-`-e`) fresh-clone install + a live `uvicorn` process serving `/static/css/...` and `/static/js/...` with real 200s, not just file-existence checks.

### Test results (Phases 2–5)

| Phase | New tests | Result |
|---|---|---|
| Phase 2 | 20 | all passing (mix of pure HMAC/signature logic and live-DB session/webhook tests) |
| Phase 3 | 22 | all passing (httpx mocked at the `admin_graphql` boundary) |
| Phase 4 | 11 | all passing (7 pure/mocked, 4 live-DB) |
| Phase 5 | 8 | all passing, against the live DB + `TestClient` (real page render, all three POST intents, one Shopify-failure path) |

Full-suite runs after each phase: 417 → 439 (Phase 3) passed, 0 failed each time. A combined Phase 3+4+5 full-suite run was in progress at the time of this update; report its result before treating this section as fully closed.

### Remaining before "Node required in production = NO"

- Phase 6 (direct storefront → Python chat integration, removing the Node hop) — not started.
- Merchant embedded-admin OAuth (`/auth`, `/auth/login`, the four `app.*` dashboard routes) — explicitly deferred; not part of the customer-facing Definition of Done, but still required before Node can retire completely.
- Odoo 401 — deferred by explicit instruction; `inventoryValidated=false` semantics preserved throughout, never fixed to false-positive "confirmed" during this phase.
- Live Shopify dev-store E2E (Save Build/Add to Cart against a real store) — still environment-blocked, same as §9/§10.

**Verdict: NOT READY FOR STAGING.** Every blocker above is external-infrastructure or external-environment (shared Postgres down; no Shopify dev store/tunnel available here) — nothing in this pass found a code-level migration regression. Once the DB is confirmed reachable, re-run the DB-backed suite and the full local end-to-end flow; the live Shopify/Save-Build/Add-to-Cart checks need to happen on a machine with real Shopify dev-store access, which this sandbox does not have.
