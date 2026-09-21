# Customer data lifecycle: retention and deletion (Phase 6, finding F11)

Status of F11: **PARTIAL.** The backend mechanisms exist and are tested: an owner can delete their
conversation, retained commerce records are minimized, deleted data cannot be written back, and a
bounded dry-run-first retention command exists. NOT done, and outside this repository: the policy
below is **provisional and unreviewed**; destructive retention is **disabled** and unscheduled;
copies in Shopify, logs, backups and the other application that shares this database are not
removed by anything here.

This is engineering work. It is **not a statement of legal compliance** with any privacy law.

## 1. What is stored, why, who owns it

Everything a guest creates hangs off one conversation id. There is no customer account in this
backend; "owner" means "holder of that conversation's capability".

| Category | Where | Personal data? | Purpose | Retention trigger | Provisional period | On deletion | This phase enforces? |
|---|---|---|---|---|---|---|---|
| Raw customer messages | `Message` (role user) | **Yes** (free text) | conversation continuity | last customer message | 90 days inactive | deleted | yes |
| Assistant messages | `Message` (role assistant) | may echo a name | continuity | same | same | deleted | yes |
| Tool / system content | not persisted (only user and assistant text is saved) | - | - | - | - | - | n/a |
| Message security classification | `MessageSecurityClassification` | no (codes) | keep hostile turns out of model context | follows its message | - | deleted with the message (cascade) | yes |
| Self-reported name, email | `Conversation.customerName/Email`, profile JSON, recommendation profile JSON | **Yes** | contact data for a build; never authentication | conversation | 90 days inactive | deleted; blanked on retained records | yes |
| Verified Shopify customer id | `ConversationCapability` / `BuildCapability.verifiedShopifyCustomerId` | **Yes** (identifier) | bind a capability to a signed-in customer | capability | with the capability | deleted | yes |
| City, country, weather | profile JSON | **Yes** (location) | seasonal fit | conversation | 90 days inactive | deleted | yes |
| Preferences, free-text fields, workflow flags | `CustomerProfileState.profileJson` | free text can be | the design itself | conversation | 90 days inactive | deleted | yes |
| Recommendation: customer-facing name and copy | `FragranceRecommendation.customerFacingJson`, `draftName`, `draftExcludedNotes` | **can be** (a customer names their blend) | preview, build | conversation / commerce | see next rows | deleted, or blanked if the record is retained | yes |
| Recommendation: private recipe and evidence | `productsJson`, `ratiosJson`, `scoreJson`, `evidenceJson` | no (business-private) | build the blend | same | same | recipe keys kept on retained records; scores and evidence blanked | yes |
| Recommendation with NO commerce footprint | `FragranceRecommendation` | as above | preview | conversation | with the conversation | **deleted** (+ inventory snapshot by cascade) | yes |
| Recommendation WITH a commerce footprint (product id, or build status creating / pending_review / saved) | `FragranceRecommendation` | minimized | reconciliation, pending-review recovery, duplicate-creation prevention | creation | 365 days, never while unresolved | **minimized and detached**, then removed by retention | yes |
| Inventory snapshot | `RecommendationInventorySnapshot(+Component)` | no | evidence of the ranking-time check | recommendation | with it | cascade | yes |
| Conversation capability | `ConversationCapability` (hash only) | identifier | authorization | expiry / revocation | 7 days after it is dead | revoked at once, deleted at purge | yes |
| Build capability | `BuildCapability` (hash only) | identifier | authorization | same | same | same | yes |
| Rate-limit buckets | `RateLimitBucket` | conversation id; keyed hash of IP | abuse control | last update | 2 days | the conversation's buckets deleted | yes |
| Deletion tombstone | `ConversationDeletion` | no (SHA-256 of the id) | stop late writes re-creating deleted data | completion | 7 days | removed by retention | yes |
| In-memory history and scratch | process memory (`_CONVERSATIONS`, `_conversation_scratch`) | **Yes** | speed | process life, LRU 500 | - | evicted locally; harmless elsewhere (section 6) | yes |
| Customer account URLs | `CustomerAccountUrls` (written by the other application) | no | legacy | conversation | - | deleted (keyed to the conversation) | yes |
| Order history | `OrderHistory` | pseudonymous (salted hash of a name), city | recommendation evidence | **not conversation-owned** | **UNKNOWN, not set here** | **untouched** | **no** |
| Shopify product for a build | Shopify: title (the customer's blend name), metafields `custom.customer_name`, `custom.customer_email`, `custom.internal_components`, `custom.note_composition` (finding N7) | **Yes** | manufacturing / the order | - | Shopify's | **NOT deleted** | **no** |
| Application logs | hosting platform | request ids, conversation and recommendation ids; no content since this phase | diagnostics | platform | platform's | not deleted | no |
| Backups, exports | database host / operator | everything above | recovery | - | the host's backup window | not deleted; expires with the backup | no |

## 2. Shared database: what this repository owns

The schema is shared with another (Node / Prisma) application. Owned by this repository (its own
migrations): `BuildCapability`, `ConversationCapability`, `RateLimitBucket`,
`MessageSecurityClassification`, `ConversationDeletion`. Everything else is shared.

* No existing table, column, constraint or cascade is changed. Migration 0004 is additive.
* Deletion uses explicit `DELETE` statements per table. The only cascades relied on are ones the
  shared schema already declares (`Message → Conversation`, the inventory snapshot chain).
* Whether the other application reads conversations, profiles or recommendations that this
  backend deletes **could not be verified**. A deleted conversation will look to it like one that
  never existed. This is the main reason F11 is PARTIAL, together with `OrderHistory`.
* `OrderHistory`, the catalog tables and `Session` are not customer-owned by any conversation and
  are never touched.

## 3. Retention policy (PROVISIONAL, configurable, execution DISABLED)

None of these numbers is a legal retention period, and none has been reviewed by the business.
They are short engineering defaults so that "might be useful later" is not the policy.

| Setting | Default | Meaning |
|---|---|---|
| `RETENTION_EXECUTION_ENABLED` | **false** | while false the command can only dry-run |
| `RETENTION_INACTIVE_CONVERSATION_DAYS` | 90 | guest conversation with no customer activity |
| `RETENTION_DEAD_CAPABILITY_DAYS` | 7 | expired or revoked capability rows |
| `RETENTION_COMMERCE_RECORD_DAYS` | 365 | minimized commerce record, measured from its creation |
| `RETENTION_TOMBSTONE_DAYS` | 7 | completed deletion tombstone |
| `RETENTION_RATE_LIMIT_BUCKET_DAYS` | 2 | rate-limit bucket since its last update |
| `RETENTION_BATCH_SIZE` | 200 | conversations per batch |

Cutoff semantics: UTC; `cutoff = now - period`; a record is eligible only if its timestamp is
**strictly older** than the cutoff.

**Activity** = the timestamp of the conversation's last *customer* message (with none, the
conversation's creation). History reads, polling, assistant or server-written messages,
`Conversation.updatedAt` and capability `lastUsedAt` do **not** extend retention (tested).
Capability expiry (30 days) is authorization only; it deletes no customer data.

Commerce records in `creating` or `pending_review` are **held**: never removed automatically,
counted in every run under `held.unresolvedCommerceRecords`, and released only by an operator
resolving them (`docs/INVENTORY_COMMERCE_SECURITY.md` section 8). A hold keeps the minimized
record only, never the conversation.

## 4. Deletion: authorization and contract

`POST /chat/delete`, body `{"conversation_id": "..."}`, header `X-Conversation-Token`.

* Authorized **only** by that conversation's capability. A conversation id, a name, an email, a
  shop or any extra body field authorizes nothing. There is no deletion by email and no bulk
  deletion. Tokens are never accepted from a URL.
* A Shopify-signed customer id is not used by the chat routes (they are not App-Proxied). Where a
  capability is bound to a verified customer, the existing rule still applies: a different signed
  id is refused, and the same id on another conversation is not ownership of it.
* Every refusal is the same `401 conversation_not_authorized` returned for an id that does not
  exist.

| Response | Meaning |
|---|---|
| `200 {"status": "deleted", "commerceRecordRetained": bool, "notCovered": "..."}` | the purge finished |
| `202 {"status": "deletion_pending", ...}` | recorded durably, capabilities already dead, removal finishes when the step in progress ends. **Not** reported as deleted |
| `401` | not authorized (also: a repeat after completion) |
| `429` / `503` | rate limited / could not be processed |

The message never promises more than happened: `notCovered` says that anything already created in
the store and routine backups are not removed. Repeats: while pending, the same (now revoked)
token may repeat the request and gets `202` or `200`; after completion the capability row no
longer exists and a repeat is a plain `401`. No email or notification is sent.

## 5. Dependency graph and what remains

```
Conversation ─┬─ Message ── MessageSecurityClassification        deleted
              ├─ CustomerProfileState                             deleted
              ├─ CustomerAccountUrls                              deleted
              ├─ ConversationCapability                           revoked, then deleted
              ├─ RateLimitBucket (4 exact keys)                   deleted
              └─ FragranceRecommendation ─┬─ BuildCapability      revoked, then deleted
                                          ├─ InventorySnapshot    follows the recommendation
                                          └─ no commerce footprint → deleted
                                             commerce footprint   → MINIMIZED + detached
```

A retained commerce record keeps **only**: `id`, `productsJson` (title and notes of each
component), `ratiosJson`, `combinationType`, `status`, `buildStatus`, `shopifyProductId`,
`shopifyVariantId`, `createdAt`, `confirmedAt`. This is an allowlist in code
(`_RETAINED_RECOMMENDATION_FIELDS`); a test fails if a column exists without a rule. Its
`conversationId` is replaced with a random value, all its capabilities are deleted, so no route
can reach it through the old conversation or an old token.

It is called **minimized**, not anonymous: the Shopify product it points to still carries the
customer's name and email (N7), and its id appears in earlier logs.

## 6. Concurrency, caches and resurrection

1. **Durable first.** The tombstone and the revocation of every capability are one committed
   transaction, before anything is removed. From then on every instance refuses the old tokens.
2. **Write guard.** `ensure_conversation_writable` is called by every path that can create or
   re-create conversation data: conversation upsert, message save (plain and classified), profile
   upsert, recommendation save, build-token issue, draft save. A process holding a stale cached
   copy (another instance) is refused by the database, not by its own memory.
3. **Locks**, all non-blocking: conversation turn lock first, then each recommendation's build
   lock in id order. Nothing waits, so nothing can deadlock. No transaction is held open while
   locks are taken, and no external service is called.
4. **In flight.** If a chat turn or a build operation holds a lock, the deletion stays `deleting`
   and the caller gets `202`. The turn's own writes are refused by the guard; when it releases its
   lock it finishes the deletion itself. So does a build operation. If the process dies, the
   retention command finishes it.
5. **Caches.** The deleting process evicts its history and scratch caches. Other instances keep
   theirs until LRU eviction or restart, but cannot serve them (authorization fails) or persist
   them (write guard). The internal adapter routes check the tombstone explicitly.
6. **Tombstone lifecycle.** SHA-256 of the conversation id, state, counts, timestamps. While
   `deleting` it also holds the id it must finish (the conversation still exists then); that is
   cleared on completion. Removed 7 days after completion. After that, the only id-addressed route
   (the internal adapter) still refuses ids that do not exist.

## 7. Retention command

`python -m scripts.data_retention` (dry run) / `--execute` (needs `RETENTION_EXECUTION_ENABLED=true`).

* Dry run is the default and performs zero writes. Output: one JSON object of counts (eligible,
  deleted, minimized, held, failed, batches, moreRemaining, UTC cutoffs). Never an id, a name, a
  message, a token or a connection string, including on failure.
* One run at a time across workers (advisory lock class 3); a second worker reports
  `anotherRetentionRunActive` and exits 2.
* Bounded: `batch_size x max_batches` conversations per run, oldest first, each in its own
  transaction; a processed conversation drops out of the query, so there is no offset to drift.
  A failed item is counted as failed, never as deleted, and the run continues. Re-running is safe.
* It applies exactly the same deletion service as a customer request, so the same ownership,
  minimization and hold rules apply.
* Not an HTTP endpoint. **Not scheduled by this repository.** It has only ever been run against the
  disposable test database.

**Operator steps before it may run on real data:** review section 3 with whoever owns the policy;
confirm the other application's dependencies (section 2); apply migration 0004; run the dry run
and review the counts; only then set `RETENTION_EXECUTION_ENABLED=true` and schedule it (daily is
sufficient) with alerting on a non-zero exit code.

## 8. External copies: what this backend cannot delete

| Copy | Status |
|---|---|
| Shopify build product (title, `custom.customer_name`, `custom.customer_email`, internal components) | not touched. Needs a separate, authenticated Shopify deletion or redaction workflow (N7). This backend can only tell the operator that a commerce record was retained |
| Shopify orders, customers, carts | outside this repository entirely |
| Odoo / manufacturing | this backend sends no customer data to Odoo |
| Model provider | conversation content was sent to the model provider during chat; its retention is governed by that provider's terms, not by this code |
| Application logs on the hosting platform | not deleted; since Phase 6 they carry no message content, names, emails, tokens or private catalog data |
| Database backups | not deleted; a deleted record persists until the backup expires. A restore would bring it back |
| The other application sharing the database | unknown caches or copies |

## 9. Failure and recovery

* A failed purge rolls back; the tombstone stays `deleting`, the capabilities stay revoked, the
  caller gets `503`, and nothing is reported as deleted. A retry or the retention command
  completes it.
* A tombstone can never exist alongside usable capabilities (same transaction).
* Restoring a backup resurrects deleted data and removes tombstones younger than the backup; the
  operator must re-apply deletions made since that backup. No tooling exists for this.

## 10. Limitations

* Provisional, unreviewed policy; execution disabled and unscheduled.
* No deletion of external copies (section 8); N7 unchanged.
* `OrderHistory` lifecycle is undefined and untouched.
* Verified-customer ("delete everything for my account") deletion does not exist: conversations
  are not linked to an account, and linking them by email would be authentication by email.
* Conversations created before capabilities existed have no token, so their owners cannot use the
  route; only retention removes them.
