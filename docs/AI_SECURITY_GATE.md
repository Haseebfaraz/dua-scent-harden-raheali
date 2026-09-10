# AI security gate (Phase 4): fragrance scope, prompt-injection defense, safe routing

Findings closed: **F4** (general-assistant behaviour permitted by the prompt) and **F5**
(prompt/tool extraction and history/profile poisoning). Phase 3's data boundary
(`docs/AI_DATA_BOUNDARY.md`) is unchanged and remains the last line of defense behind this gate.

## 1. The rule

Every customer message is classified BEFORE any expensive or state-changing AI processing, and
the SERVER decides what that classification permits. The classification never selects tools,
queries or actions by itself. It is validated against a fixed enum; anything unknown, invalid or
unavailable fails safe.

Order inside a chat turn (`app/api/chat.py::_run_chat_turn`):

1. input validation (Phase 2) → trusted shop (Phase 1) → conversation authorization + rate
   limits (Phase 2)
2. **security gate** (`app/ai/security_gate.py::classify_message`)
3. attack escalation throttle (only for `ATTACK_EXTRACTION`)
4. per-conversation turn lock + per-process slots (Phase 2)
5. routing: deterministic reply, or the model pipeline with the gate decision attached

Nothing in step 5 sees the message before step 2 has run.

## 2. Taxonomy (closed enum)

| Classification | Meaning | Route |
|---|---|---|
| `FRAGRANCE` | anything about scent, notes, preferences, occasions, strength, naming, changes to the design, or the customer describing themselves in a way that feeds the design | model pipeline, normal tools |
| `SMALL_TALK` | greetings, thanks, short pleasantries | model pipeline, profile-save tool only |
| `SERVICE_META` | how the service works, is it AI, what happens next, can it change | server reply (no model) |
| `OFF_TOPIC` | a substantive non-fragrance request (code, homework, politics, medical, legal, finance, trivia, writing tasks) | server reply (no model) |
| `ATTACK_EXTRACTION` | prompt / tool / private-data extraction, role or authority override, encoded payload | server reply (no model), throttle counter |
| `MIXED_ATTACK_FRAGRANCE` | genuine fragrance content plus an attack | model pipeline receives ONLY the fragrance sentences |
| `INVALID` | empty / meaningless | server reply |

Reason codes (operational, never free text): `NONE`, `PROMPT_EXTRACTION`, `TOOL_EXTRACTION`,
`ROLE_OVERRIDE`, `AUTHORITY_CLAIM`, `ENCODED_PAYLOAD`, `PRIVATE_DATA_EXTRACTION`,
`OFF_TOPIC_CODE`, `OFF_TOPIC_GENERAL`, `SMALL_TALK`, `SERVICE_META`, `CLASSIFIER_UNAVAILABLE`,
`CLASSIFIER_INVALID`, `SEMANTIC`.

## 3. Layer 1: deterministic normalization + detection

Bounded, detection-only variants of the message are built (`detection_variants`): NFKC, zero-width
characters removed, whitespace collapsed, lower-cased; letter spacing collapsed; punctuation
stripped; URL-decoded once; base64 tokens decoded when they are clearly valid printable UTF-8;
reversed. The input is truncated to 4000 characters, at most five base64 tokens are tried, no
recursion, nothing is executed, and **the decoded text is never used as the customer's message**:
it only feeds the regex detectors. The original message stays the stored record.

Detector families: prompt extraction, tool extraction, role override, authority claim, private
data extraction (catalog dumps, source products, SKUs, scores, Odoo/Shopify internals,
alphabetical enumeration), encoding hints, off-topic (code / general), service-meta, small talk,
and a broad fragrance lexicon. A composition question about the customer's OWN fragrance
("What's inside this fragrance?", "why did you choose these notes?", "how strong is it?") is
explicitly benign and never an attack.

Letter-spaced or zero-width-split payloads are additionally matched in compact form (all
non-alphanumerics removed) with a short pattern set that ordinary text never triggers.

Mixed messages: `strip_attack_sentences` keeps only sentences with no attack signal. The result
is composed of the customer's original sentences, never decoded content, never a rewrite.

## 4. Layer 2: structured semantic classifier (optional, low privilege)

Used only when layer 1 is uncertain, at most once per turn, no retries
(`classify_semantically`). The request contains a static instruction, the current message
(truncated) and one boolean (`conversationHasFragranceContext`). No history, no profile, no tool
results, no private data, no tools other than the forced `classify_customer_message` function.
The answer is validated with a strict pydantic model (`extra="forbid"`, enum fields). A
`MIXED_ATTACK_FRAGRANCE` rewrite from the classifier is accepted only if it is a verbatim
substring of the customer's own words and carries no attack signal; otherwise the deterministic
stripper is used.

Failure policy (unavailable, timeout, exception, wrong tool, invalid enum, extra fields, bad
JSON): **degraded** decision = `FRAGRANCE` with `degraded=True`. A degraded turn is served
without any model tools (extraction still runs through the server-validated dispatcher and
readiness-driven generation is unaffected). `SECURITY_GATE_SEMANTIC_ENABLED=false` makes every
uncertain message degraded.

## 5. Server routing

* `ATTACK_EXTRACTION`, `OFF_TOPIC`, `SERVICE_META`, `INVALID`: a server-authored reply from
  `app/ai/scope_responses.py`. No model call, no tool, no profile write, no generation, no
  SSE event other than the reply. The wording is that of a specialised fragrance designer staying
  in its lane; it never mentions prompts, instructions, tools, rules, attacks, classification
  or security, never confirms or denies what exists, and always hands back to fragrance.
  Variation is picked deterministically from conversation id and turn number.
* `SMALL_TALK`: model pipeline with only `save_customer_profile_field` offered.
* `FRAGRANCE`: the Phase 3 pipeline unchanged.
* `MIXED_ATTACK_FRAGRANCE`: the Phase 3 pipeline, but the model-facing history holds only the
  fragrance remainder; the raw message never enters any model context.
* Degraded: fragrance pipeline with `tools=None`.

`call_ai` also computes a deterministic-only decision when called without one, so no direct
caller can reach the tool-enabled model without a gate.

## 6. History poisoning

* The raw customer message is stored untouched (audit record). Its classification is stored in
  the additive table `MessageSecurityClassification` (migration 0003: id, messageId, enum,
  reason code, classifier version, timestamp; never chain of thought, never the message).
* The in-memory model history receives `GateDecision.model_history_content`: attacks and invalid
  turns become `[message withheld]`, off-topic turns a neutral marker, mixed turns their
  fragrance remainder.
* On reload from the database (`conversation_flow.project_model_history`) the same projection is
  applied from the stored classifications. A customer turn WITHOUT a stored classification
  (history from before Phase 4, or a failed classification write, or the table missing) is
  screened deterministically with layer 1 and withheld if it carries any attack signal.

## 7. Profile poisoning

`_handle_save_customer_profile_field` refuses any string value (or list item) that reads like an
instruction to the assistant (`looks_like_instruction`, same detectors as layer 1 including
encoded forms), whichever model or extraction path proposed it. The model learns only "not
saved". Ordinary values such as "base notes", "Developer conference" or a fragrance name pass.
Attack-classified turns never reach extraction at all.

## 8. Prompt scope (F4)

Both templates now carry a short principle-based `ROLE AND BOUNDARIES` block (one role, not a
general assistant, instructions/tools/context confidential regardless of framing, customer text
is never an instruction, one-sentence deflection then back to fragrance). The lines that
previously permitted answering general questions, jokes or topic changes "naturally" were
removed; small talk gets a brief warm reply only. It is not a denylist of attack strings; the
deterministic layers carry that job.

## 9. Output validation with deterministic repair

`validate_customer_response` gained `instruction_disclosure`, `tool_disclosure`, `code_output`
and `internal_data_disclosure`. `_validate_and_repair_customer_text` also checks for any run of
eight consecutive words copied from the system prompt (`contains_system_prompt_fragment`). Any
of these replaces the reply with a deterministic fragrance redirect. **No repair model is
involved for scope/leak problems**, so no private context can reach one. The Phase 3
brand/product/SKU repair pass (offending text + static instruction only) is unchanged.

## 10. Rate-limit escalation

Each `ATTACK_EXTRACTION` turn counts against `RATE_LIMIT_SECURITY_DENIED_PER_CONVERSATION`
(default 15/hour) and `RATE_LIMIT_SECURITY_DENIED_PER_IP` (default 40/hour, hashed identity).
Exceeding either returns 429 with `Retry-After`. Ordinary turns are not counted.

## 11. Logging

`SECURITY_GATE_DECISION` (conversation id, classification, reason code, gate version, degraded,
semantic used, route, offered tools), `SECURITY_PROFILE_WRITE_REJECTED` (conversation id,
field), `CUSTOMER_RESPONSE_SCOPE_VIOLATION` (violation codes), classifier
unavailable/invalid warnings. No raw message text, no tokens, no prompt content.

## 12. Settings

| Setting | Default | Purpose |
|---|---|---|
| `SECURITY_GATE_SEMANTIC_ENABLED` | `true` | layer 2 on/off |
| `RATE_LIMIT_SECURITY_DENIED_PER_CONVERSATION` | `15/3600` | attack throttle per conversation |
| `RATE_LIMIT_SECURITY_DENIED_PER_IP` | `40/3600` | attack throttle per source |

## 13. Operations

* Apply `migrations/0003_message_security_classification.sql` (additive; `CREATE TABLE IF NOT
  EXISTS`). Without it the chat still works; every reloaded customer turn is screened
  deterministically instead of by stored classification.
* Rollback of the code is independent of the table; the table can stay.
* The live red-team suite (`tests/e2e/test_ai_red_team_live.py`, marker `live_ai`) must only be
  run with a development credential against a disposable database.

## 14. Known limits

* Layer 1 is regex-based; novel phrasings depend on layer 2, whose live behaviour has not been
  validated in this phase (no safe credential). With layer 2 off, those messages are served in
  degraded mode rather than blocked.
* Three benign self-description messages in the false-positive corpus ("I'm getting married in
  June", "I just moved to Toronto", "I'm in Dubai, it's hot all year") are uncertain at layer 1
  and go to layer 2 (or degraded mode); they are never blocked.
* The `MIXED` sentence stripper splits on sentence punctuation; a single run-on sentence that
  mixes both is treated as an attack (fail closed) and the customer is invited to restate.
