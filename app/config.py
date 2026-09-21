from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_env: str = "development"
    log_level: str = "INFO"

    database_url: str

    openai_api_key: str
    # No default: model choice must come from the environment, never hardcoded.
    openai_model: str
    openai_temperature: float = 0.3
    openai_timeout_seconds: int = 30
    # Note-aware copy generation's own model -- kept as its own env-overridable setting rather
    # than collapsed into openai_model, so the two can diverge again later if needed. This default
    # only applies when OPENAI_COPY_MODEL is unset; always prefer setting it explicitly.
    openai_copy_model: str = "gpt-5.6-terra"
    openai_copy_temperature: float = 0.5

    # Phase 5A: NO implicit destination. These used to default to a real Odoo sandbox hostname,
    # so an unset variable silently pointed development and tests at a live external service.
    # Empty means "integration not configured": the client makes no request at all, discovery
    # treats availability as unconfirmed, and commerce is blocked. Server-side configuration only;
    # no request or browser value can ever select an inventory destination.
    odoo_ping_url: str = ""
    odoo_inventory_url: str = ""
    odoo_inventory_api_key: str = ""
    odoo_inventory_cache_ttl_seconds: int = 60
    # Phase 5 (F9): a positive commerce inventory verification authorizes a write only for this
    # long. Commerce never uses the recommendation cache above. There is deliberately NO setting
    # that lets unknown or unavailable inventory authorize a commerce write.
    commerce_inventory_max_age_seconds: int = 30

    # ---- Phase 6 (F11): data lifecycle. PROVISIONAL defaults pending operator / policy review ----
    # None of these is a legal retention period. They are engineering defaults for a guest chat
    # product, chosen to be short; docs/DATA_RETENTION_AND_DELETION.md section 3 explains each.
    # All cutoffs are UTC and strictly "older than": a record exactly at the cutoff is kept.
    #
    # Destructive retention runs are OFF until an operator reviews the policy. With this false the
    # maintenance command can only ever dry-run, whatever flags it is given.
    retention_execution_enabled: bool = False
    # A guest conversation is inactive when its LAST CUSTOMER MESSAGE (or, with none, its creation)
    # is older than this. Reads, polling, assistant or server writes never extend it.
    retention_inactive_conversation_days: int = 90
    # Expired or revoked capability rows (hashes only) are kept this long for abuse investigation.
    retention_dead_capability_days: int = 7
    # A commerce record reduced to its operational minimum is removed this long after creation.
    # Records in `creating` / `pending_review` are never removed automatically (they are reported).
    retention_commerce_record_days: int = 365
    # How long a completed deletion tombstone keeps blocking late writes before it is removed.
    retention_tombstone_days: int = 7
    retention_rate_limit_bucket_days: int = 2
    retention_batch_size: int = 200

    # ---- Phase 5A: the inventory SOURCE CONTRACT (docs/INVENTORY_COMMERCE_SECURITY.md section 2a) ----
    # Facts about the inventory source that this backend cannot discover by itself. Each one is an
    # explicit operator declaration with a documented meaning, none has a default, and none of
    # them can approve anything alone: the live response must ALSO carry matching evidence. While
    # any is missing, commerce stays blocked (recommendations are unaffected). None of these is a
    # bypass: there is no value that skips the stock comparison.
    #
    # The stock location/warehouse the endpoint reports for, exactly as the endpoint echoes it in
    # its top-level "location" field. Undeclared, or not echoed identically -> unconfirmed.
    odoo_inventory_location_scope: str = ""
    # What the quantity means. Only UNRESERVED_AVAILABLE satisfies the commerce policy, and then
    # the per-item field read is "available_qty" (on hand minus existing reservations). The
    # endpoint as integrated today reports only "on_hand_qty"; the truthful value for that is
    # ON_HAND_INCLUDES_RESERVED, which does NOT satisfy the policy.
    odoo_inventory_quantity_semantics: str = ""
    # The manufacturing contract behind the conservative requirement bound: the most fragrance
    # oil, in millilitres, that producing ONE finished bottle can draw from inventory IN TOTAL
    # across all of its oils, including any loss or overfill, for every build this product offers
    # (any Top/Middle/Base ratio). Setting it also declares that fragrance oils are the only
    # inventory-constrained inputs this gate is responsible for (alcohol and packaging are managed
    # elsewhere). Must be between the formula's own maximum oil volume and the bottle size.
    manufacturing_max_oil_ml_per_bottle: float | None = None

    customer_key_hash_salt: str = ""

    # Real names from the reference app's shopify.server.js / shopify.app.toml -- not invented.
    # Empty defaults so import never fails where these aren't needed yet (e.g. non-Shopify tests);
    # every real call site must check for a real value before trusting it.
    shopify_api_key: str = ""
    shopify_api_secret: str = ""
    # Phase 1 (security): the ONE shop this single-store app may ever send its client credentials
    # or an Admin access token to, e.g. "your-store.myshopify.com". Mandatory: there is no
    # hard-coded fallback in any environment, and every Shopify Admin call fails closed until this
    # is set. Validated and allowlist-compared in app/shopify/trusted_shop.py.
    shopify_shop_domain: str = ""
    shopify_app_url: str = ""
    scopes: str = ""
    # Node hardcodes ApiVersion.October25; shopify.app.toml's [webhooks] separately says 2025-04 --
    # picking one canonical value here rather than porting that mismatch.
    shopify_api_version: str = "2025-04"

    # Shared secret for the Node Shopify adapter -> this service hop (never customer-facing).
    # Enforced only when set, so local dev without it configured still works.
    internal_api_key: str = ""

    # Comma-separated list of browser origins allowed to call this API directly. In the current
    # architecture nothing calls this service from a browser (Node is the only caller, server to
    # server, where CORS doesn't apply) -- this exists so that changes, should this ever be
    # fronted directly, default to "none" instead of "*".
    allowed_origins: str = ""

    # ---- Phase 2 (security): public chat trust boundary, limits, and abuse controls ----
    # Guest conversation session secret lifetime (idle sessions expire; the stored history stays).
    conversation_token_ttl_days: int = 30
    # Hard input limits, enforced at the schema/application layer before any DB or OpenAI work.
    chat_max_message_chars: int = 4000
    chat_max_name_chars: int = 100
    chat_max_email_chars: int = 254
    # JSON request bodies above this are refused with 413 before being read.
    max_request_body_bytes: int = 65536
    # Model-context budget: the most recent messages within BOTH bounds are sent to the model;
    # everything older stays in the database untouched.
    chat_context_max_messages: int = 40
    chat_context_max_chars: int = 24000
    # Public history endpoint returns at most this many (most recent) customer-visible messages.
    chat_history_max_messages: int = 100
    # Per-turn cost ceilings.
    chat_max_tool_turns: int = 10
    chat_max_tool_calls_per_turn: int = 6
    chat_turn_deadline_seconds: int = 90
    openai_max_output_tokens: int = 700
    openai_copy_max_output_tokens: int = 200
    # Per-process cap on simultaneous model-bearing chat turns (multiplied by instance count).
    chat_max_concurrent_turns: int = 8
    # Rate limits: "<count>/<window seconds>". Storage is PostgreSQL (multi-instance safe).
    rate_limit_conversation_create_per_ip: str = "10/3600"
    rate_limit_chat_turn_per_conversation: str = "12/60"
    rate_limit_chat_turn_per_conversation_daily: str = "200/86400"
    rate_limit_chat_turn_per_ip: str = "30/60"
    rate_limit_history_read_per_conversation: str = "60/60"
    rate_limit_history_read_per_ip: str = "120/60"
    # Number of trusted reverse-proxy hops in front of this service. 0 = use the socket peer
    # address and ignore X-Forwarded-For entirely; 1 = Render's edge proxy (see render.yaml).
    trusted_proxy_hops: int = 0
    # Keyed hash for abuse identifiers (IP addresses are never stored raw). Falls back to
    # CUSTOMER_KEY_HASH_SALT, then to an unkeyed hash, if unset.
    abuse_identity_hash_key: str = ""
    # ---- Phase 4 (security): scope / security gate ----
    # Layer 2 (structured semantic classifier) runs only when layer 1 is uncertain. Off means
    # uncertain messages are UNRESOLVED: a server-authored invitation to restate, nothing else runs.
    security_gate_semantic_enabled: bool = True
    # Hard ceiling for the single classifier call; exceeding it leaves the turn UNRESOLVED.
    security_gate_classifier_timeout_seconds: float = 8.0
    # Temporary per-conversation / per-IP throttle after repeated attack-classified messages.
    rate_limit_security_denied_per_conversation: str = "15/3600"
    rate_limit_security_denied_per_ip: str = "40/3600"
    # Server-owned welcome line for a fresh conversation (opt-in per request; the browser can no
    # longer supply assistant text).
    chat_welcome_message: str = "Hi! I help people design a fragrance that feels like their own. What brings you here today?"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]  -- populated from env/.env at import time
