from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat, health, preview, recommendations, save_build
from app.config import settings
from app.logging_config import RequestContextMiddleware, configure_logging
from app.shopify import webhooks as shopify_webhooks

configure_logging(settings.log_level)

app = FastAPI(title="DUA Scent AI Core Backend")

# Nothing else in the current architecture calls this service from a browser (Node's Shopify
# adapter is the only other caller, server to server, where CORS doesn't apply) -- allowed_origins
# defaults to empty (no browser origin allowed) rather than "*", configurable via ALLOWED_ORIGINS
# should that ever change. save_build.router is the one deliberate exception: the live storefront
# theme's note sliders call it directly cross-origin, so it sets its own wildcard CORS headers
# per-response instead of going through this global allowlist (matches the Node route it replaces).
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_origins_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.add_middleware(RequestContextMiddleware)

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
