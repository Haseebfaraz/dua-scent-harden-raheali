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
| Chat/SSE (FastAPI endpoint) | `chat.jsx` | `test_chat_api.py` | Verified via TestClient + one real live round trip (real OpenAI call, real DB persistence, through the actual Node proxy) | Yes, pending Phase 9 |

**Phase 9 (genuine end-to-end against a live Shopify storefront — real customer login, real
theme, Save Build/Add to Cart)** has not been run by an agent in this environment; it requires a
live Shopify dev store and tunnel. Everything up to and including the Node↔Python HTTP boundary
has been proven with a real request/response round trip, not just mocks.

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
