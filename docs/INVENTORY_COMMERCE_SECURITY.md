# Inventory verification and commerce transaction safety (Phase 5, finding F9)

Status of F9 after this phase: **PARTIAL**. Every commerce write this backend controls now fails
closed on anything other than a fresh, complete, positive on-hand check. Purchase paths that do not
pass through this backend (section 9) are not protected, stock is never reserved (section 7), and
several facts about the source of truth are unknown (section 2).

Four states that must not be confused:

| # | State | Established by | Meaning |
|---|---|---|---|
| 1 | The direction MATCHES the customer | recommendation engine | nothing about stock |
| 2 | Manufacturing inputs were VERIFIED on hand | `app/services/commerce_inventory.py` | one lookup, one build, one moment |
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

## 3. Requirement calculation

Known, from `app/fragrance/formulas.py`: a finished bottle is 34 ml; 12 to 14 ml of it is oil
(default 13 ml); the recommendation's per-product `ratiosJson` percentages apply to the oil only;
quantities round to 0.01 ml.

**Unknown:** how the customer's final Top/Middle/Base slider changes the per-oil quantities the lab
pours. The slider drives display quantities and price (`builds.py`), and the module that owns the
manufacturing formula says it never touches Top/Middle/Base. No rule connecting the two exists in
this repository, and none was invented.

What the gate does instead: a bound that holds for every valid ratio. A bottle holds at most
`MAX_OIL_ML` (14 ml) of oil in total, so no single oil, and no group of components sharing one
Odoo item, can need more than 14 ml per bottle. **Every distinct Odoo item in the build must have
on hand at least `14.00 ml x quantity`.** Arithmetic is `Decimal`, rounded to 0.01 ml; exactly 14.00
passes, 13.99 fails. This is a sufficient condition, not the exact requirement: it can refuse a
build the lab could make from a nearly empty oil. It never approves one it could not.

* Components and the per-product formula come only from the stored recommendation; the formula must
  be coherent (same products, finite, positive, totalling 100%) or requirements are UNKNOWN.
* Phase 1 ratio rules are unchanged (integers 1..98, three layers, sum 100).
* Quantity: the product offers exactly one bottle per action (the cart link is always `:1`). The
  gate accepts 1..10 for service callers and scales linearly; the routes always pass 1.
* Bottle size: only 34 ml exists.
* The browser's claims about availability, quantities, timestamps, mappings, prices, product ids or
  source products are never read.

## 4. Inventory states

`InventoryState` (typed, in `commerce_inventory.py`):

| State | Meaning | Commerce |
|---|---|---|
| `VERIFIED_AVAILABLE` | complete valid answer; every item's on-hand quantity covers the bound; taken just now | allowed, for exactly this build, for 30 s |
| `VERIFIED_INSUFFICIENT` | complete valid answer; at least one item is short | blocked |
| `UNKNOWN` | requirements, mapping, unit, quantity or response cannot be established | blocked |
| `SERVICE_UNAVAILABLE` | no usable answer (timeout, auth, 5xx, malformed) | blocked |

Internal reason codes (logs only): `REQUIREMENTS_UNKNOWN`, `MAPPING_MISSING`, `MAPPING_INACTIVE`,
`UNIT_UNCONFIRMED`, `SERVICE_ERROR`, `RESPONSE_MALFORMED`, `RESPONSE_INCOMPLETE`,
`RESPONSE_AMBIGUOUS`, `QUANTITY_INVALID`, `INSUFFICIENT`, `STALE`, `FINGERPRINT_MISMATCH`.

The discovery pair `buildable=True, inventoryValidated=False` still exists for ranking. It is never
read by any commerce path. There is no setting that turns unknown availability into approval.

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
5. draft save (the customer's design; not a commerce state)
6. per-recommendation lock; recommendation re-read inside the lock; pending-review check
7. existing build only: read product, verify it is this recommendation's own build (Phase 1)
8. deterministic price computed and checked finite and positive (Phase 1)
9. **fresh inventory verification for exactly these components, ratios and quantity**
10. writes: create → media → price → publish (first time), or untrack / create variant → rename last

A build capability is not evidence of stock. Inventory success never overrides a failed ownership
check, because it is never reached.

## 7. Freshness, concurrency and reservations

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

* No idempotency key exists. Nothing a caller sends selects or reuses an earlier result.
* First-time creation is tracked in the existing `FragranceRecommendation.buildStatus` column (no
  migration): `draft → creating → saved`, or `→ pending_review`.
* A refusal before any write (inventory, validation, a definitive 4xx or GraphQL rejection) returns
  the build to its previous status; the customer can simply try again.
* A creation whose outcome is unknown (timeout, transport error, 5xx), or a product that was
  created but whose price could not be set, becomes `pending_review`. It is reported as such, never
  as success, never retried automatically, and nothing is "rolled back": there is no transaction
  across Shopify and PostgreSQL. A `creating` marker left by a crashed or cancelled process is
  treated the same way. A draft save never erases either marker.
* The product is now published LAST, after its price is set, so a partial failure cannot leave a
  purchasable product without its computed price.
* Repricing needs no marker: a retry re-reads the product's variants and reuses the one that was
  created.
* **Operator recovery for `pending_review`:** in Shopify admin, look for a product whose
  `custom.note_composition` metafield carries the recommendation id. If one exists and is correct,
  set `shopifyProductId` and `buildStatus = 'saved'` on the recommendation (or archive the product);
  if none exists, set `buildStatus = 'draft'`. No automated reconciliation was built: searching
  Shopify for a just-created product is eventually consistent and could not be validated here.

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

Checking inventory before returning a variant does not stop that variant being bought later. A
disabled button or theme JavaScript is not enforcement. Nothing in this repository can close these
paths: it has no order webhook, no checkout validation and no manufacturing integration.

## 10. External enforcement required for full closure

1. Populate `OdooOilMapping.unitOfMeasure` with `ml` for every mapping whose Odoo item really is in
   millilitres. Until then every commerce action returns `inventory_unconfirmed`.
2. Confirm which company / warehouse / location the inventory endpoint reports, and whether
   `on_hand_qty` includes reserved stock. Ideally expose an unreserved quantity.
3. Define the manufacturing rule that turns the customer's Top/Middle/Base into per-oil quantities,
   so the conservative bound can be replaced by the exact requirement.
4. Enforce availability where purchases are accepted: a Shopify checkout validation (Shopify
   Functions), or an order-created handler that re-checks and holds or cancels, or true reservation
   in Odoo at order time.
5. Decide the life cycle of published build products (unpublish or archive after purchase or after
   a period), since each one remains purchasable indefinitely.
6. Theme: handle the new response codes (section 11).

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
