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

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]


settings = Settings()  # type: ignore[call-arg]  -- populated from env/.env at import time
