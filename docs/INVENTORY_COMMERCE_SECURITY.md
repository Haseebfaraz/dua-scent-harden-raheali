# Inventory verification and commerce transaction safety (Phase 5, finding F9)

Status of F9: **PARTIAL** (unchanged by Phase 5A). Every commerce write this backend controls fails
closed unless every fact in section 2a is known and a fresh, complete answer covers the requirement.
With the inventory endpoint as integrated today those facts are NOT all available, so **real
commerce is expected to stay blocked** until the integration supplies them (section 10). Purchase
paths that do not pass through this backend (section 9) are not protected, and stock is never
reserved (section 7).

> **Phase 5A corrections (2026-09-21).** The Phase 5 text of this page over-claimed in three places
> and they are corrected here rather than silently rewritten:
> 1. It let a positive `on_hand_qty` plus a recorded unit become `VERIFIED_AVAILABLE` while the
>    queried location and the reservation semantics were unknown. An observation is not an approval
>    (sections 2a and 4).
> 2. It said of the 14 ml bound: "It never approves one it could not [make]." That does not follow.
>    A consumption bound says nothing about reserved or inaccessible stock, concurrent consumption
>    or purchase paths that bypass the check, and its own premise was only a code constant
>    (section 3).
> 3. It described the draft save as harmless before the lock. A refused concurrent request could
>    overwrite the draft of the operation in progress (section 7).

Four states that must not be confused:

| # | State | Established by | Meaning |
|---|---|---|---|
| 1 | The direction MATCHES the customer | recommendation engine | nothing about stock |
| 2 | The commerce inventory POLICY was satisfied | `app/services/commerce_inventory.py` | every required fact known, one lookup, one operation, one moment |
| 3 | Stock is RESERVED | **nothing in this system** | not supported by the Odoo API this app uses |
| 4 | A purchase / manufacturing commitment was ACCEPTED | Shopify checkout and whatever processes orders | outside this repository |

A recommendation's `status = "confirmed"` means the customer's identity and the recipe shape were
validated and the combination is not a duplicate of a real product. It does **not** mean stock was
verified or reserved. A build capability authorizes one caller for one build; it says nothing
about stock. A conversation capability is never build authority.

## 1. What existed before (verified by reading `8694338`)

* `odoo_inventory.evaluate_candidate_inventory` is called only while ranking candidates in the chat
  pipeline. Its documented semantics are lenient on purpose: missing mapping, item not found,
  lookup failure and any internal exception all return `buildable=True, inventoryValidated=False`.
  Only a confirmed shortage rejects a candidate. A 60 second in-process cache sits in front of it.
* No commerce path consulted inventory at all: `preview.py` (save_build, add_to_cart),
  `save_build.py`, `builds.create_shopify_build_product` and `builds.reprice_existing_build`
  contained no inventory call. `get_oil_inventory_for_product`, whose docstring says it is
  "intended for Save Build/Add to Cart", had no caller.
* So a build ranked under `buildable=True, inventoryValidated=False` (or ranked days earlier) went
  straight to product creation, publication to every sales channel, variant creation and a cart
  link.
* Partial failure: the product was created ACTIVE and published BEFORE its price was set; if the
  default variant could not be resolved the function still returned success with no price set.
* Duplicates: two overlapping first-time saves both saw no `shopifyProductId` and both created a
  product. A timeout during creation was reported as a failure and the next click created again.

## 2. Source of truth: facts and unknowns

Read from `app/integrations/odoo_client.py`, `app/services/odoo_inventory.py`, the models and the
configuration. No credential was inspected and Odoo was not queried to resolve any of this.

| Question | Answer |
|---|---|
| What identifies a component? | `recommendation.productsJson[].title` → `FragranceProduct.normalizedTitle` (unique) → `OdooOilMapping.fragranceProductId` (unique) → `odooSku`, matched against Odoo `default_code`. |
| Duplicate mappings? | Impossible per product (unique constraints). Several products MAY share one `odooSku` by design; demand is aggregated per item. |
| Missing mapping? | Discovery: tolerated. Commerce: UNKNOWN, no lookup. |
| Which company / warehouse / location? | **UNKNOWN.** The request is `GET <url>?skus=a,b` with no such parameter; the response has none. |
| Unit of the quantity? | **Not reported by the API.** Existing code assumes millilitres. The only recorded evidence is `OdooOilMapping.unitOfMeasure`, which an operator fills per mapping. Commerce requires it to say millilitres; anything else, or nothing, is UNKNOWN. No conversion is performed. |
| On-hand, available, forecast or unreserved? | `on_hand_qty` only. **Existing reservations are NOT accounted for.** A positive check can overstate usable stock. |
| Complete responses? | One batched request. No pagination marker exists, so truncation cannot be detected directly; every requested item absent from the answer makes the whole result UNKNOWN. Two rows for one item are ambiguous, also UNKNOWN. |
| How old can data be? | Discovery cache: `ODOO_INVENTORY_CACHE_TTL_SECONDS` (60 s) plus the age of the recommendation. Commerce: never cached; a positive result authorizes a write for at most `COMMERCE_INVENTORY_MAX_AGE_SECONDS` (30 s) and is taken immediately before the write. How stale Odoo's own figure is: **UNKNOWN**. |
| Timeout / auth failure / malformed / partial? | 8 s client timeout. All of them: SERVICE_UNAVAILABLE or UNKNOWN, never approval. |
| Does the integration support reservations? | **No.** The client is read-only (`ping`, `get-inventory`). |
| Can this repository verify final purchase or manufacturing availability? | **No.** There is no order webhook, no order processing and no manufacturing code here (the only webhook handled is `app/uninstalled`). |

## 2a. The inventory approval contract

Three things are kept apart in code (`StockObservation`, the facts, `InventoryDecision.state`):
what the source **reported**, what has actually been **verified**, and whether that satisfies
**policy**. Rule: if a fact necessary for the commerce decision is unknown, the decision is
UNCONFIRMED and the dependent write is blocked. When a declared fact is missing the source is not
even queried.

| Fact | Established by | If missing |
|---|---|---|
| F1 integration configured | `ODOO_INVENTORY_URL`, explicit https URL, no default | UNCONFIRMED, zero requests |
| F2 stock location | `ODOO_INVENTORY_LOCATION_SCOPE` declared by the operator **and** echoed identically in the response's top-level `location` | UNCONFIRMED |
| F3 reservation semantics | `ODOO_INVENTORY_QUANTITY_SEMANTICS=UNRESERVED_AVAILABLE` **and** every row carries `available_qty` (on hand minus existing reservations) | UNCONFIRMED |
| F4 unit | `OdooOilMapping.unitOfMeasure` says millilitres **and** any `uom` in the row agrees; no conversion | UNCONFIRMED |
| F5 manufacturing contract | `MANUFACTURING_MAX_OIL_ML_PER_BOTTLE` (section 3) | UNCONFIRMED |
| F6 component to item mapping | unique catalog row, unique active mapping | UNCONFIRMED |
| F7 complete valid response | each requested item exactly once, valid number | UNCONFIRMED / SERVICE_UNAVAILABLE |
| F8 sufficiency | every distinct item covers the bound | INSUFFICIENT |
| F9 freshness and binding | taken now, for this operation's fingerprint | blocked |

What the backend can establish alone: F1, F6, F7, F8, F9. What it cannot, and therefore needs an
integration contract for: F2, F3, F4 (partly), F5. The declared values are **operator facts with a
fixed meaning** (documented in `app/config.py`): they come only from server configuration, never
from a browser, a request or a model; none is boolean; none of them can approve anything without
the matching evidence in the live response; and none skips F7 or F8. There is no
`INVENTORY_APPROVED`-style switch.

**With today's endpoint** (`on_hand_qty` only, no `location`, no `uom`, no `available_qty`) the
truthful declaration is `ON_HAND_INCLUDES_RESERVED`, F2 cannot be echoed, and commerce is blocked
with `inventory_unconfirmed`. That is the intended state until the endpoint is extended.

Even `POLICY_SATISFIED` is not a reservation, is valid for 30 seconds for one operation, and says
nothing about purchases that do not pass through this backend.

## 3. Requirement calculation

Known, from `app/fragrance/formulas.py`: a finished bottle is 34 ml; 12 to 14 ml of it is oil
(default 13 ml); the recommendation's per-product `ratiosJson` percentages apply to the oil only;
quantities round to 0.01 ml.

**Unknown:** how the customer's final Top/Middle/Base slider changes the per-oil quantities the lab
pours. The slider drives display quantities and price (`builds.py`), and the module that owns the
manufacturing formula says it never touches Top/Middle/Base. No rule connecting the two exists in
this repository, and none was invented.

**What the repository supports, and what it does not.** `formulas.py` (a port of the original
service, used for ranking feasibility) states a 34 ml bottle holding 12 to 14 ml of oil, with
alcohol as the remainder. That is a code constant with a comment. It is not a manufacturing
contract: nothing in the repository documents loss, overfill or dilution, nothing says the 14 ml
maximum holds for a customised Top/Middle/Base build, and alcohol and packaging are never checked
as inventory at all. So the premise of any bound is **not established by this repository**.

**Decision.** The bound is kept as a method, but its premise must be declared explicitly as the
manufacturing contract, `MANUFACTURING_MAX_OIL_ML_PER_BOTTLE = M`:

> Producing ONE finished bottle draws at most M millilitres of fragrance oil from inventory in
> total, across all of its oils, including any loss or overfill, for every build this product
> offers. Fragrance oils are the only inventory-constrained inputs this gate is responsible for.

Validation: a finite number with `14 <= M <= 34` (below the formula's own maximum it would
contradict the repository; above the bottle it is impossible). **Without it the requirement is
UNKNOWN and commerce is blocked.** No formulation rule was invented.

**Invariant proved, given that premise.** If one bottle draws at most M in total, then no single
item, and no group of components sharing one item, can need more than M. Therefore "every distinct
item has at least `M x 1` available" is a **sufficient consumption condition**. Arithmetic is
`Decimal`, rounded to 0.01 ml; exactly M passes, M minus 0.01 fails. A test checks the invariant
against the repository formula for every split and every allowed oil volume.

**Intentional false rejections.** A build whose real need from an oil is 3 ml is refused when that
oil has 13 ml left. This is the cost of not knowing the allocation rule.

**Stock semantics it requires.** The quantity compared must be unreserved stock at the location
manufacturing draws from (F2, F3). Compared against a bare on-hand figure the bound proves nothing.

**What it does not prove.** That the stock is physically accessible; that it will still be there at
purchase time; that another customer, channel or the lab is not consuming it concurrently; that
alcohol, bottles or packaging exist; or anything about purchases outside this backend.

* Components and the per-product formula come only from the stored recommendation; the formula must
  be coherent (same products, finite, positive, totalling 100%) or requirements are UNKNOWN.
* Phase 1 ratio rules are unchanged (integers 1..98, three layers, sum 100).
* Quantity: the product offers exactly one bottle per action (the cart link is always `:1`). The
  gate accepts exactly 1. (Phase 5 accepted 1..10 for service callers; that broadened the product
  contract and was removed.)
* Bottle size: only 34 ml exists.
* The browser's claims about availability, quantities, timestamps, mappings, prices, product ids or
  source products are never read.

## 4. Inventory states

Policy decision, `InventoryState`:

| State | Meaning | Commerce |
|---|---|---|
| `POLICY_SATISFIED` | F1 to F9 all hold, just now, for exactly this operation | allowed for that operation, for 30 s |
| `INSUFFICIENT` | every fact known; a complete valid answer says an item is short | blocked |
| `UNCONFIRMED` | at least one necessary fact is unknown | blocked |
| `SERVICE_UNAVAILABLE` | no usable answer (timeout, auth, 5xx, malformed, not reachable) | blocked |

Observation, `ReportedStock` (logged, never a permission): `NOT_OBSERVED`, `SUFFICIENT_REPORTED`,
`SHORTAGE_REPORTED`. A decision can be `UNCONFIRMED` while the observation is
`SUFFICIENT_REPORTED`: the source said plenty, and the policy still says no.

Phase 5 called the first state `VERIFIED_AVAILABLE`. This is not a rename: the permission changed.
The old state needed only a recorded unit and a positive `on_hand_qty`.

Internal reason codes (logs only): `INTEGRATION_NOT_CONFIGURED`, `SOURCE_SCOPE_UNDECLARED`,
`SOURCE_SCOPE_UNCONFIRMED`, `RESERVATION_SEMANTICS_UNDECLARED`, `RESERVATION_SEMANTICS_INSUFFICIENT`,
`MANUFACTURING_CONTRACT_MISSING`, `MANUFACTURING_CONTRACT_INVALID`, `REQUIREMENTS_UNKNOWN`,
`MAPPING_MISSING`, `MAPPING_INACTIVE`, `UNIT_UNCONFIRMED`, `UNIT_INCONSISTENT`, `SERVICE_ERROR`,
`RESPONSE_MALFORMED`, `RESPONSE_INCOMPLETE`, `RESPONSE_AMBIGUOUS`, `QUANTITY_INVALID`,
`INSUFFICIENT`, `STALE`, `FINGERPRINT_MISMATCH`, `NOT_ISSUED_BY_GATE`.

The discovery pair `buildable=True, inventoryValidated=False` still exists for ranking. It is never
read by any commerce path.

## 5. Recommendation policy versus commerce policy

| | Discovery (chat, ranking, preview page) | Commerce (save, reprice, add to cart) |
|---|---|---|
| Function | `evaluate_candidate_inventory` | `require_commerce_inventory` |
| Unknown / outage | recommendation still offered, labelled `AVAILABILITY_UNCONFIRMED` | blocked |
| Cache | 60 s | none |
| Stored evidence | `RecommendationInventorySnapshot` (history only) | none reused; every action looks up again |
| Requirement | recommendation formula at 13 ml | worst-case bound, section 3 |

## 6. Authorization and write ordering

Both browser entry points call `app/services/build_commerce.execute_build_commerce`; the gate itself
lives one layer lower, inside `app/shopify/builds.py`, so a future service caller cannot skip it.

1. request shape and bounded input (Phase 2)
2. App Proxy signature where applicable; trusted shop (Phase 1)
3. build capability for exactly this recommendation (Phase 1/2). **Unauthorized callers stop here:
   an id alone never triggers an inventory lookup.**
4. name and ratio validation (Phase 1)
5. per-recommendation lock; recommendation re-read inside the lock; pending-review check
6. draft save, INSIDE the lock (Phase 5A; it used to precede the lock)
7. existing build only: read product, verify it is this recommendation's own build (Phase 1)
8. deterministic price computed and checked finite and positive (Phase 1)
9. **fresh inventory verification for exactly these components, ratios and quantity**
10. writes, first time: create as DRAFT → record the product id → media → set price → read back that
    EVERY variant carries that price → re-check inventory freshness → activate → publish.
    Existing build: untrack / create variant (priced in the same mutation) → rename last

A build capability is not evidence of stock. Inventory success never overrides a failed ownership
check, because it is never reached.

## 7. Freshness, concurrency and reservations

* **Binding.** A decision carries an operation fingerprint over: recommendation id, component set,
  the recommendation's per-product formula, the customer's ratios, quantity, bottle size, the
  declared bound, the component-to-item mapping actually used, the declared location and the
  declared quantity semantics. Change any of them and an earlier decision does not apply. Decisions
  are sealed inside the gate; the Shopify write layer accepts none from a caller and always
  obtains its own.
* **Expiry.** 30 seconds, tested with a controlled clock. It is checked at the first write. For
  the multi-step first-time creation it is checked again immediately before activation (the step
  that makes the build purchasable): expired evidence is not silently reused, one new verification
  is made, and if that is not satisfied the product stays a DRAFT and the build goes to
  `pending_review`. Nothing here is atomic across Odoo, Shopify and PostgreSQL.
* **The draft race (Phase 5A).** `preview.py` used to save the draft and only then reach the lock,
  so an authorized second request could overwrite the stored name and ratios of the operation in
  progress and still be refused. The operation itself was unaffected (it uses its own validated
  inputs, never the stored draft), but the stored draft no longer matched the saved product. The
  draft is now written inside the lock, after the pending-review check, for save, add to cart and
  recreate alike. A refused request changes nothing. Each operation works from its own snapshot.
* Every commerce action performs its own lookup. A changed recipe, ratio or quantity has a
  different fingerprint and cannot reuse anything. A failed refresh after an earlier success blocks.
* One commerce operation per recommendation at a time, across instances: PostgreSQL advisory lock
  (class 2), the same mechanism as the chat turn lock, so it matches the multi-instance deployment.
  A second overlapping request gets a conflict; it is not queued. Different builds never block each
  other.
* **This lock serializes this application only. It does not reserve anything in Odoo, and it does
  not stop another customer's different build, another sales channel or the lab from consuming the
  same oil between the check and the purchase.** Two customers whose builds share an oil can both
  pass the check on the last 14 ml.
* **Stock is never reserved.** No local stock counter was introduced; Odoo stays the only source of
  truth. The check-to-purchase race can only be closed downstream (section 10).

## 8. Retries, duplicates, partial and ambiguous completion

Three different outcomes, described differently to the customer:

| Outcome | What is known | Status afterwards | Customer wording |
|---|---|---|---|
| **Preflight failure** (inventory, validation, a definitive 4xx or GraphQL rejection of the create) | no Shopify object exists | back to what it was; retry freely | "...so we haven't created it. Your design is saved." |
| **Failure after a write began** (product created; price, read-back, freshness or activation failed) | a DRAFT product exists; its id is stored | `pending_review` | "We couldn't confirm whether this blend finished saving..." |
| **Ambiguous completion** (timeout, transport error, 5xx, interruption on the create call) | a product may or may not exist | `pending_review` (or `creating` after a crash) | same; never "we haven't created it" |

* No idempotency key exists. Nothing a caller sends selects or reuses an earlier result.
* **Durable retry protection.** The advisory lock dies with its connection, so it is not what
  prevents a duplicate after a crash. `buildStatus = creating` is committed BEFORE the creation
  request can be sent, and the Shopify product id is committed the moment Shopify returns it. A
  process that dies before the external call, after Shopify accepted but before the id was stored,
  or after the id was stored but before completion, leaves `creating`; every later attempt
  (save, add to cart, recreate, an ordinary draft save) refuses and preserves the marker. No
  migration was needed; the existing free-text column carries the two new values.
* The product is created as a **DRAFT**, which cannot be bought on any channel whatever its
  publication state. It becomes ACTIVE only after the computed price is set, a read-back shows
  EVERY variant carries exactly that price (a priced variant must never hide one left at the
  default), and the inventory evidence is still fresh. Publication comes last and stays best
  effort. Nothing is deleted or "rolled back" automatically.
* Repricing needs no marker: a retry re-reads the product's variants and reuses the one created.

### Operator recovery for `creating` / `pending_review`

Never reset the status first. A reset without reconciliation is exactly what produces a duplicate
product. Do not run any of this against real infrastructure without authorisation.

1. **Read-only reconciliation.** In Shopify admin, search products by vendor "The Dua Brand",
   template `custom-scent`, including DRAFT status, created around the recommendation's
   timestamp. Open each candidate and read its `custom.note_composition` metafield; the build is
   the one whose `recommendationId` equals the stuck recommendation's id. If the recommendation row
   already stores a `shopifyProductId`, look that product up directly.
2. **Exactly one matching product, DRAFT, correctly priced on every variant:** activate and publish
   it by hand, then set `shopifyProductId`, `shopifyVariantId` and `buildStatus = 'saved'` on the
   recommendation.
3. **A matching product that is incomplete or wrongly priced:** archive it, then set
   `shopifyProductId = NULL`, `shopifyVariantId = NULL` and only then `buildStatus = 'draft'`.
4. **More than one matching product:** keep none automatically; archive the extras first, then
   apply step 2 or 3 to what remains.
5. **No matching product after the search index has had time to catch up:** set
   `buildStatus = 'draft'`.
6. Record what was found. No automated reconciliation was built: searching Shopify for a
   just-created product is eventually consistent and could not be validated without live access.

## 9. Cart and checkout bypass analysis

Verified from code: every build variant is created with `inventoryItem.tracked = false`, tracked
variants are switched to untracked, the product is `ACTIVE` and published to all sales channels,
and the customer is sent to the product URL or a cart permalink. **Shopify therefore does not track
or enforce availability for these builds at all.**

| Path | Goes through this backend's gate? |
|---|---|
| Preview "Save build" / "Add to cart" | yes |
| Theme slider `POST /api/save-build` | yes |
| A variant id returned earlier, added to the cart later | **no** |
| Direct storefront cart request (`/cart/add.js`, `/cart/<variant>:<qty>`) | **no** |
| Editing the permalink or the cart to a larger quantity | **no** (the backend only ever checks one bottle) |
| A saved cart or a checkout link | **no** |
| The published product page of an existing build, days later | **no** |
| A stale preview page | the POST is checked again; a cart link it already produced is not |
| Another sales channel the product was published to | **no** |

Draft products created since Phase 5A are not purchasable until activated; that narrows the window
inside a creation, it does not close any path below once a build is active.

Checking inventory before returning a variant does not stop that variant being bought later. A
disabled button or theme JavaScript is not enforcement. Nothing in this repository can close these
paths: it has no order webhook, no checkout validation and no manufacturing integration.

## 10. External requirements

**To unblock controlled commerce at all (section 2a):**

1. Extend the inventory endpoint to return, per response, the `location` it reports for, and per
   item `available_qty` (unreserved) and `uom`. Then declare `ODOO_INVENTORY_LOCATION_SCOPE` and
   `ODOO_INVENTORY_QUANTITY_SEMANTICS=UNRESERVED_AVAILABLE` to match.
2. Record `unitOfMeasure = ml` on every oil mapping whose Odoo item really is in millilitres.
3. Have manufacturing state the contract behind `MANUFACTURING_MAX_OIL_ML_PER_BOTTLE`, including
   loss and overfill, or better, the rule from Top/Middle/Base to per-oil quantities so the bound
   can be replaced by the exact requirement.

**For full F9 closure, a decision is still needed between controls that are NOT interchangeable:**

| Control | What it actually guarantees |
|---|---|
| Pre-purchase validation (for example Shopify checkout validation) | can REJECT a purchase before it is accepted, for every cart path; still a check, not a hold |
| Reservation in the stock system at order or cart time | ALLOCATES stock, if and only if that system genuinely enforces reservations against every consumer |
| Post-order webhook that re-checks | can HOLD, FLAG or cancel an order that was ALREADY ACCEPTED; it cannot prevent the acceptance or the payment |

Also outstanding: the life cycle of published build products (each stays purchasable
indefinitely); theme handling of the response codes in section 11; and N3's theme update.
Nothing in this list was configured, created or mutated in this phase.

## 11. Customer-facing failure responses

Stable codes, no operational detail, no retry durations (none has a defined basis). The preview
POST keeps its existing convention (HTTP 200 with `error` and `code`); `POST /api/save-build` uses
status codes.

| Situation | Code | Status (`/api/save-build`) |
|---|---|---|
| verified shortage | `inventory_insufficient` | 409 |
| cannot be established | `inventory_unconfirmed` | 409 |
| inventory service outage | `inventory_unavailable` | 503 |
| overlapping operation | `build_in_progress` | 409 |
| outcome could not be confirmed | `build_pending_review` | 409 |

Each failure: no premature write, no success event, no commerce-success state stored, the
recommendation and the customer's draft name and ratios kept. The conversational model receives a
server-authored `availabilityGuidance` sentence with each recommendation (never to promise stock,
never to say anything is reserved or guaranteed). No item codes, quantities, source products,
Odoo identifiers, locations, raw responses or exception text reach the browser, any model or the
commerce decision log.
