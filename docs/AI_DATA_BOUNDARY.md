# AI Data Boundary

Where the trust boundary between DUA's private data and the customer-facing language models
sits, what each model may receive, and how the tests keep it that way. Written for the next
engineer who touches `app/ai/`.

Philosophy (Phase 3): the model does not get information it must never reveal. Prompt wording and
the output validator are defense in depth, not the boundary.

---

## 1. Model types (every production model call)

| # | Call site | Purpose | Model | Receives | Tool access | Output goes to |
|---|---|---|---|---|---|---|
| 1 | `app/ai/conversation_flow.py::_extract_and_persist_profile_facts` | Structured extraction of profile facts from the newest customer message | `OPENAI_MODEL` | static extraction instruction; customer-context DATA pair; last 6 customer/assistant texts (max 6,000 chars). Never tool results, ids, catalog, evidence. | forced `record_profile_updates` | server (persists via the model-tool dispatcher) |
| 2 | `conversation_flow.py::call_ai` main loop | The conversational reply / tool decisions | `OPENAI_MODEL` | static system prompt; customer-context DATA pair; bounded recent history (40 msgs / 24k chars); results of the 4 model tools; `fragrance_studio_status` labels | the 4 model tools (1 in small-talk mode) | customer |
| 3 | `conversation_flow.py::_bridge_for_recommendation` | Explain a freshly built fragrance | `OPENAI_MODEL` | the same as #2 plus the `present_fragrance_recommendation` DATA pair carrying a `CustomerSafeRecommendation` | none | customer |
| 4 | `conversation_flow.py::_validate_and_repair_customer_text` | Rewrite a reply that tripped the output validator | `OPENAI_MODEL` | only the offending customer-facing text and a static rewrite instruction | none | customer |
| 5 | `app/services/copy_generation.py::call_copy_model` | Short description / why-it-suits copy for a proposal | `OPENAI_COPY_MODEL` | `CopyModelInput` only: notes grouped by role, the customer's stated likes/dislikes/style/occasion, matched/missing families, a confidence label, an evidence-scope label | none | customer (stored as customer-facing copy) |

Excluded, dev-only: `scripts/conversation_simulation.py` and `scripts/conversation_eval.py`
(simulated customers and a judge model; never run in production, `live_ai` marker).

## 2. Customer model input (allowed categories)

* CUSTOMER_PUBLIC: what the customer typed (bounded), their self-reported name, their own
  stated preferences.
* CUSTOMER_SAFE_DERIVED, explicitly allowlisted: verified city/country, weather condition and
  climate direction, the list of still-needed preference dimensions (short labels), the
  `CustomerSafeRecommendation` fields, `fragrance_studio_status` labels, copy-model labels.

## 3. Forbidden model input

PRIVATE_CATALOG (source product titles, handles, collections, inspiration identities, raw
candidate or component objects), PRIVATE_ANALYTICS (relevance/final/fit scores, confidence
breakdowns, order counts by city/state/country/season, cohort and repeat-purchase counts,
evidence objects, ranking mechanics), PRIVATE_OPERATIONAL (SKUs, Odoo mappings and quantities,
inventory snapshots, Shopify product/variant ids, recommendation/conversation ids, capability
tokens or hashes, profile workflow flags), SECRET (any credential or configuration secret),
and private exceptions or reason strings.

## 4. Private engine (behind the boundary)

`app/services/recommendation_pipeline.py` is the only entry the orchestrator calls. It runs, in
this order and unchanged from before: `analyze_customer_product_candidates` (order history,
`ProductRegionSummary`, catalog), `generate_new_product_combinations` (compatibility, scoring,
ratios, novelty), `evaluate_auto_confirm_eligibility`, `save_recommendation`,
`evaluate_candidate_inventory` (Odoo), `confirm_recommendation`, build-capability minting. Its
result is a `PipelineOutcome`: a status label, control data (recommendation id, preview URL),
and a `CustomerSafeRecommendation`.

Trigger: `should_generate()` is deterministic. Discovery mode, profile complete
(`is_profile_ready_for_analysis`), no recommendation selected yet, and this exact profile state
not already attempted. Refinement is the one model-initiated path
(`refine_fragrance_recommendation`), and it also returns only the safe result.

## 5. Safe DTOs (`app/ai/safe_views.py`)

`CustomerSafeRecommendation` (pydantic, `extra="forbid"`, built field by field):

| Field | Meaning |
|---|---|
| `name` | customer-facing fragrance name (draft name if the customer renamed it) |
| `character` | short scent character phrase |
| `whyItMatches` | why it suits the stated preferences |
| `bestFor` | occasion / use fit |
| `climateFit` | weather / season fit wording |
| `strength` | light / moderate / strong |
| `caveat` | customer-safe risk wording, if any |
| `matchLabel` | strong / balanced / adventurous match (derived from the private confidence) |
| `notes.top/middle/base` | note names of the finished scent (max 6 per layer) |
| `availability` | AVAILABLE / AVAILABILITY_UNCONFIRMED / UNAVAILABLE (derived from the private inventory snapshot) |

`CustomerSafeProfileView`: name, emailKnown (boolean only), likes, dislikes, preferredStyle,
occasion, giftRecipient, strength, requestedSeasonStyle, verified city/country, currentWeather,
climate, additionalPreferences, stillNeeded.

Both are the ONLY serializers: `to_customer_safe_recommendation` (internal API) and the stored
`customerFacingJson` are produced from the same allowlist; title-bearing and evidence-bearing
fields the engine still computes are stored in `evidenceJson` (internal).

## 6. Tool surface

Before (13, all model-callable): save_customer_profile_field, get_customer_profile,
analyze_customer_product_candidates, get_product_notes_and_combination_status,
find_existing_combinations_for_product, check_exact_combination_exists,
find_combinations_using_similar_notes, verify_customer_location, resolve_season_preference,
select_recommendation, generate_new_product_combinations, refine_combination_recommendations,
confirm_product_combination.

After (4, `app/ai/tools.py`, strict argument schemas): save_customer_profile_field,
verify_customer_location, resolve_season_preference, refine_fragrance_recommendation. In
small-talk mode only the first. Everything else is server-only through
`tool_executor.execute_fragrance_tool` and is refused by name in
`tool_executor.execute_model_tool`.

## 7. Control data (outside model text)

Recommendation ids, preview URLs, build capability tokens, conversation ids and tokens, Shopify
product/variant ids. They travel as SSE events (`preview_ready`, `id`), JSON responses, or page
data, are emitted by the server from its own state, and are never generated by or shown to a
model. The model's reply text is never parsed for control data.

## 8. Test guarantee

`tests/security/model_boundary.py` captures every outbound model request (chat, extraction,
bridge, repair, copy) and provides `assert_model_context_customer_safe`, which fails if any
sentinel canary (fake source title, handle, SKU, score, cohort marker, Odoo/Shopify markers,
recommendation id, profile control marker) or any forbidden private key name appears in any
request. `tests/security/test_model_boundary.py` drives real turns (initial, discovery,
recommendation, refinement, failure statuses, malicious customer) against a private engine
saturated with canaries. Structural tests assert the exact field sets of the safe DTOs and that
an unknown internal field added later is rejected. Any new field reaching a model must be added
to an allowlist deliberately, and any new private key name should be added to
`FORBIDDEN_MODEL_KEYS`.
