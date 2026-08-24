from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import chat, health, preview, recommendations
from app.config import settings
from app.logging_config import RequestContextMiddleware, configure_logging
from app.shopify import webhooks as shopify_webhooks

configure_logging(settings.log_level)

app = FastAPI(title="DUA Scent AI Core Backend")

# Nothing in the current architecture calls this service from a browser (Node's Shopify adapter
# is the only caller, server to server, where CORS doesn't apply) -- allowed_origins defaults to
# empty (no browser origin allowed) rather than "*", configurable via ALLOWED_ORIGINS should that
# ever change.
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
app.mount("/static", StaticFiles(directory=str(Path(__file__).resolve().parent / "static")), name="static")
