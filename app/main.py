from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat, health, preview, recommendations, save_build
from app.api.request_limits import RequestBodyLimitMiddleware
from app.config import settings
from app.logging_config import RequestContextMiddleware, configure_logging
from app.shopify import webhooks as shopify_webhooks
from app.shopify.trusted_shop import UntrustedShopError, trusted_shop

configure_logging(settings.log_level)

app = FastAPI(title="DUA Scent AI Core Backend")
@app.get("/")
def read_root():
    return {"status": "ok", "message": "DUA Scent AI Core Backend is running"}


def trusted_browser_origins() -> set[str]:
    """Exact browser origins allowed to call this API cross-origin: ALLOWED_ORIGINS plus the
    trusted shop's own storefront origin. Never a wildcard."""
    origins = set(settings.allowed_origins_list)
    try:
        origins.add(f"https://{trusted_shop()}")
    except UntrustedShopError:
        pass
    return origins


class TrustedOriginCORSMiddleware(CORSMiddleware):
    """Starlette's CORSMiddleware with the origin allowlist evaluated per request from settings
    (so the trusted shop is honoured without an import-time snapshot). Exact match only."""

    def is_allowed_origin(self, origin: str) -> bool:
        return origin in trusted_browser_origins()


# Phase 1 (security): the storefront (chat widget and theme sliders) is the only browser caller.
# Both the global middleware and app/api/save_build.py's own per-response headers reflect the
# exact trusted origin; the previous wildcard on the privileged save-build route is gone.
app.add_middleware(
    TrustedOriginCORSMiddleware,
    allow_origins=[],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)
# Phase 2 (F6): oversized JSON bodies are refused (413) before they are read.
app.add_middleware(RequestBodyLimitMiddleware)

app.include_router(health.router)
app.include_router(chat.router)
app.include_router(recommendations.router)
app.include_router(shopify_webhooks.router)
app.include_router(preview.router)
app.include_router(save_build.router)
_static_dir = str(Path(__file__).resolve().parent / "static")
app.mount("/static", StaticFiles(directory=_static_dir), name="static")
# The preview page is only ever loaded through Shopify's App Proxy at /apps/scent-library/... --
# the browser resolves the page's own asset URLs against that path, not the backend's real host,
# so its CSS/JS must be reachable under the same prefix or the proxy never forwards those requests
# (they'd hit the storefront's own domain root instead, 404ing there -- exactly what produced the
# unstyled/non-interactive preview). Same directory, second mount point; nothing removed.
app.mount("/apps/scent-library/static", StaticFiles(directory=_static_dir), name="static_via_app_proxy")
