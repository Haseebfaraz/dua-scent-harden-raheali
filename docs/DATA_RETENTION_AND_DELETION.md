# Customer data lifecycle: retention and deletion (Phase 6, finding F11)

Status of F11: **PARTIAL.** The backend mechanisms exist and are tested: an owner can delete their
conversation, retained commerce records are minimized, deleted data cannot be written back, and a
bounded dry-run-first retention command exists. NOT done, and outside this repository: the policy
below is **provisional and unreviewed**; destructive retention is **disabled** and unscheduled;
copies in Shopify, logs, backups and the other application that shares this database are not
removed by anything here.

This is engineering work. It is **not a statement of legal compliance** with any privacy law.

> **Phase 6A corrections (2026-09-21).** The Phase 6 version of this page described an
> accepted-but-pending deletion (`202`) whose completion depended on the in-flight request's
> `finally` block, on the customer retrying, or on the age-based retention command, which is
> disabled. That could strand an accepted deletion indefinitely while the customer had already
> discarded their credential. Deletion is now **synchronous and atomic**: `200` means the single
> transaction that removed everything promised locally has committed; `409` means nothing at all
> was written and the same credential can simply retry (section 4). The write guard is now a
> database-level lock rule, not a check-then-write (section 6). The customer route is **off by
> default** pending the shared-database review (section 2a). `CustomerAccountUrls` is no longer
> deleted (section 2). Migration 0004 was revised before any application outside the disposable
> test database.

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
| Customer account URLs | `CustomerAccountUrls` (written by the other application) | no (endpoint URLs) | legacy | not conversation-owned by this app | - | **left untouched** (Phase 6A) | n/a |
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

### 2a. The shared-database review, and the gate that stands in for it

Deleting rows from `Conversation`, `Message`, `CustomerProfileState` and `FragranceRecommendation`
is irreversible, and those tables are defined by the other application's Prisma schema. This
repository is a port of that application's chat and build services (the models "mirror
prisma/schema.prisma exactly"; the Node routes now call this backend's internal adapter). Whether
the Node application still reads or writes those rows directly **could not be verified from this
repository**. Unknown is not "verified absent".

Therefore EVERY destructive path is gated by `SHARED_DATA_DELETION_REVIEWED` (default `false`),
checked at the one boundary all of them pass through (`delete_conversation`), before any lock,
marker, revocation or purge. The customer route authorizes the caller as usual and then answers
`503 deletion_unavailable`; age-based retention degrades to a dry run and reports
`held.sharedDataReviewPending`, whatever `RETENTION_EXECUTION_ENABLED` says. Approval to run
retention is not evidence that deleting the shared rows is safe (Phase 7). **Setting the flag is
not the review.** It records that the following review was done:

| Table / rows | Owner | This app deletes | Review required before enabling |
|---|---|---|---|
| `Conversation` (name, email, timestamps) | Prisma schema; rows created by this app's bootstrap and by the legacy Node chat | yes | confirm no Node route, job or report reads a conversation after this app deleted it, and that no Node code re-inserts one from its own cache |
| `Message` (+ `MessageSecurityClassification` by cascade) | Prisma schema; rows written by this app | yes | same; confirm the Node widget only reads history through this app's routes |
| `CustomerProfileState` | Prisma schema; rows written by this app | yes | same |
| `FragranceRecommendation` without commerce footprint | Prisma schema; rows written by this app | yes | confirm nothing in Node keys off a recommendation id after preview (email, analytics, admin screens) |
| `FragranceRecommendation` with commerce footprint | as above | no (minimized, section 5) | confirm the Node admin / manufacturing views tolerate blanked customer-facing fields |
| `ConversationCapability`, `BuildCapability`, `RateLimitBucket`, `ConversationDeletion` | this app (own migrations) | yes | none |
| `CustomerAccountUrls` | written by Node's customer-account OAuth code | **no** (no personal data; left alone) | none needed now; if Node later stores PII there, revisit |
| `OrderHistory`, catalog tables, `Session` | Node / operator imports | no | out of scope; `OrderHistory` needs its own lifecycle decision |

Age-based retention (section 3) removes the same rows; `RETENTION_EXECUTION_ENABLED` is an
additional, separate approval to run it, and cannot substitute for the review above.

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

## 4. Deletion: authorization and contract (Phase 6A)

`POST /chat/delete`, body `{"conversation_id": "..."}`, header `X-Conversation-Token`.

* Authorized **only** by that conversation's capability. A conversation id, a name, an email, a
  shop or any extra body field authorizes nothing. There is no deletion by email and no bulk
  deletion. Tokens are never accepted from a URL. A different signed Shopify customer id is
  refused; the same id on another conversation is not ownership of it.
* Every refusal is the same `401 conversation_not_authorized` returned for an id that does not
  exist.

| Response | Meaning | Credential afterwards |
|---|---|---|
| `200 {"status": "deleted", "commerceRecordRetained": bool, "notCovered": "..."}` | the ONE transaction that removed everything promised locally has committed | gone (row deleted); a repeat is a plain `401` |
| `409 deletion_conflict` (+ `Retry-After`) | a chat turn, a build operation or a writer's transaction is in flight. **Nothing was written**: no marker, no revocation | still valid; repeat the request |
| `503 deletion_unavailable` | the gate in section 2a is off. Nothing was changed | still valid |
| `503 deletion_failed` | the transaction failed and was rolled back. Nothing was changed | still valid |
| `401` | not authorized, unknown, or already deleted | - |

There is no accepted-but-pending state and no status endpoint: nothing is ever "in progress"
across requests, so there is nothing to poll. A lost `200` response is the only ambiguity. A
repeat then returns `401`, and **a `401` is not proof of deletion** (Phase 7 correction): it is
also what an expired, revoked or wrong token gets. The only confirmation of completion is the
`200` itself. The widget must treat a `401` on a repeat as "completion could not be confirmed"
and say so; the customer may still clear local state. No email or notification is sent.

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

## 6. Concurrency, caches and resurrection (Phase 6A)

Two rules, enforced in code and pinned by tests that use real PostgreSQL sessions and barriers:

**Rule W (writers).** Every write of conversation-owned data runs inside a transaction that first
takes the SHARED write-guard advisory lock for that conversation (`pg_advisory_xact_lock_shared`,
class 4), then checks that no tombstone exists (`ensure_conversation_writable`), then writes, then
commits before any external call. A new transaction means a new guard call (the message writers
commit the conversation upsert first and guard again before inserting the message). Applied by:
conversation upsert, both message saves, the profile upsert, recommendation save, build-token
issue, draft save. A minimized (detached) commerce record is refused outright.

**Rule D (deletion).** Try the conversation turn lock (class 1) and every build lock of the
conversation (class 2, by id); then, in ONE transaction, try the EXCLUSIVE write-guard lock
(class 4), insert the completed tombstone, purge, commit. Any lock that cannot be taken means
CONFLICT and nothing written.

Why this closes the race: a writer holds the shared lock from its check to its commit; deletion
needs the exclusive lock for its whole transaction. So a writer either committed before deletion
began (its rows are purged) or takes its lock after deletion committed (it sees the tombstone).
There is no window in which a writer that saw no tombstone can commit after the tombstone
exists. This is a database guarantee that holds for direct service callers, other instances and
stale caches alike. The turn and build locks add conflict semantics so an in-flight operation
finishes cleanly instead of failing half way (tested: a chat turn in flight gets a `409`, the
turn completes normally, the retry deletes).

Lock order is fixed (1, then 2 by id, then 4) and every deletion-side lock is a try-lock, so
nothing waits and nothing can deadlock. Writers' shared requests wait only for the milliseconds
of a purge. No transaction is held open while session-level locks are taken and no external
service is called under any lock.

**Caches.** The deleting process evicts its history and scratch caches. Another instance may keep
a stale copy until LRU eviction or restart; it cannot serve it (authorization fails; the internal
routes also verify the row exists and evict) and cannot persist it (rule W).

**Tombstone lifecycle and expiry.** A tombstone records a COMPLETED deletion only: SHA-256 of the
conversation id, origin, timestamps, held count. Retention removes it after
`RETENTION_TOMBSTONE_DAYS`. After that, what prevents recreation is structural, not the
tombstone: every entry point that takes a conversation id either requires a live capability
(deleted with the conversation) or verifies the `Conversation` row exists and otherwise mints a
fresh, server-controlled id; no route calls the insert-capable upsert for an id the server did
not mint; and a minimized commerce record refuses every customer write for ever. Tested with an
advanced clock.

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
  minimization and hold rules apply. A conversation whose operation is in flight is counted as
  `held.operationInFlight` and picked up by the next run.
* A customer's explicit deletion never depends on this command: it completes in its own request
  or it is refused with nothing written.
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

## 9. Failure and recovery (Phase 6A)

| Situation | Behaviour |
|---|---|
| Process crash during deletion | the transaction never committed: no marker, no revocation, no partial purge; the credential still works; retry |
| Database interruption mid-purge | rolled back; `503 deletion_failed`; nothing changed |
| Missing migration 0004 | every guarded write fails closed (`ProgrammingError`), the delete route answers `503 deletion_failed`; nothing changed |
| One record failing minimization | the whole transaction rolls back; nothing changed; the failure is logged by type only |
| Blocked shared dependency | the gate (section 2a) answers `503 deletion_unavailable` before any irreversible step |
| Another worker deleting the same conversation | it holds the locks: this request gets `409`; a retry finds the tombstone and answers `200` (already deleted) |
| Application restart | nothing to resume: there is no in-progress state |
| Completion, then tombstone cleanup | section 6 |
| Backup restore | resurrects deleted rows and removes tombstones younger than the backup; deletions since that backup must be re-applied by hand (no tooling) |

Counts: the route logs `state` (completed / conflict) and the failure type; retention reports
eligible, deleted, minimized, held (`operationInFlight`, `unresolvedCommerceRecords`,
`anotherRetentionRunActive`) and failed. Never an id, a name, a message, a token or a
connection string.

## 10. Limitations

* Provisional, unreviewed policy; execution disabled and unscheduled.
* No deletion of external copies (section 8); N7 unchanged.
* `OrderHistory` lifecycle is undefined and untouched.
* Verified-customer ("delete everything for my account") deletion does not exist: conversations
  are not linked to an account, and linking them by email would be authentication by email.
* Conversations created before capabilities existed have no token, so their owners cannot use the
  route; only retention removes them.
