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

    # These fallbacks are only ever used if the env var itself is unset -- matches the JS
    # reference's odooClient.server.js own hardcoded fallback exactly; always prefer setting
    # ODOO_PING_URL/ODOO_INVENTORY_URL in the real environment over relying on this. Named after
    # the actual REST endpoints this integration calls (bearer-token auth against one specific
    # URL) -- not the generic odoo_url/database/username/password shape of a direct XML-RPC Odoo
    # connection, which this app does not use.
    odoo_ping_url: str = "https://the-dua-brand-sandbox-12aug-36292701.dev.odoo.com/api/v1/dua-ai/ping"
    odoo_inventory_url: str = "https://the-dua-brand-sandbox-12aug-36292701.dev.odoo.com/api/get-inventory"
    odoo_inventory_api_key: str = ""
    odoo_inventory_cache_ttl_seconds: int = 60

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
