import asyncio
import json
import logging

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.session import get_session
from app.shopify.trusted_shop import UntrustedShopError, trusted_shop

logger = logging.getLogger(__name__)
router = APIRouter()

# Tables the running service reads or writes: the shared base schema plus migrations 0001-0004.
REQUIRED_TABLES = (
    "Session", "Conversation", "Message", "Note", "OrderHistory", "FragranceProduct", "OdooOilMapping", "ExistingCombination",
    "ProductRegionSummary", "CustomerProfileState", "FragranceRecommendation", "RecommendationInventorySnapshot",
    "RecommendationInventoryComponent", "BuildCapability", "ConversationCapability", "RateLimitBucket",
    "MessageSecurityClassification", "ConversationDeletion",
)
_DB_TIMEOUT_SECONDS = 3.0


@router.get("/health")
async def health() -> dict:
    """Liveness only: never touches the database or any dependency."""
    return {"status": "ok", "service": "dua-scent-ai-python"}


def _config_checks() -> dict[str, bool]:
    try:
        trusted_shop()
        shop_ok = True
    except UntrustedShopError:
        shop_ok = False
    return {
        "config.trusted_shop": shop_ok,
        "config.shopify_app_credentials": bool(settings.shopify_api_key and settings.shopify_api_secret),
        "config.openai": bool(settings.openai_api_key and settings.openai_model),
    }


def _informational() -> dict[str, bool]:
    """Reported, never required: commerce stays blocked (fail closed) while any inventory item is
    false. Uses the commerce gate's own checks, so this can never disagree with what it enforces."""
    from app.services.commerce_inventory import RequirementsUnknown, manufacturing_bound_ml, source_contract_gaps

    gaps = source_contract_gaps()
    try:
        manufacturing_bound_ml()
        bound_ok = True
    except RequirementsUnknown:
        bound_ok = False
    checks = {f"inventory.{gap.lower()}": False for gap in gaps}
    checks["inventory.source_contract_satisfied"] = not gaps
    checks["inventory.manufacturing_bound_valid"] = bound_ok
    checks["data.customer_key_hash_salt"] = bool(settings.customer_key_hash_salt)
    checks["shopify.internal_adapter_key"] = bool(settings.internal_api_key)
    return checks


async def _schema_checks(session: AsyncSession) -> dict[str, bool]:
    checks: dict[str, bool] = {}
    try:
        await asyncio.wait_for(session.execute(text("SELECT 1")), _DB_TIMEOUT_SECONDS)
        checks["db.connect"] = True
    except Exception as err:  # noqa: BLE001 -- reported as a failed check, type only
        logger.error("READINESS_DB_FAILED %s", json.dumps({"errorType": type(err).__name__}))
        return {"db.connect": False, "db.schema": False}
    found = set((await session.execute(
        text("SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema() AND table_name = ANY(:names)"),
        {"names": list(REQUIRED_TABLES)},
    )).scalars().all())
    missing = [t for t in REQUIRED_TABLES if t not in found]
    bound_column = await session.scalar(text(
        "SELECT count(*) FROM information_schema.columns WHERE table_schema = current_schema() AND table_name = 'BuildCapability' AND column_name = 'verifiedShopifyCustomerId'"
    ))
    checks["db.schema"] = not missing and bool(bound_column)
    if missing:
        checks.update({f"db.table.{t}": False for t in missing})
    return checks


async def readiness_report(session: AsyncSession) -> dict:
    required = {**_config_checks(), **await _schema_checks(session)}
    ready = all(required.values())
    return {"status": "ready" if ready else "not_ready", "checks": required, "informational": _informational()}


@router.get("/health/ready")
async def ready(session: AsyncSession = Depends(get_session)) -> JSONResponse:
    """Readiness: required configuration present, database reachable, schema migrated. Answers
    with check NAMES and booleans only -- never values, URLs or error text."""
    report = await readiness_report(session)
    failing = sorted(k for k, ok in report["checks"].items() if not ok)
    if failing:
        logger.info("READINESS_NOT_READY %s", json.dumps({"failing": failing}))
    return JSONResponse({"status": report["status"], "failing": failing}, status_code=200 if not failing else 503, headers={"Cache-Control": "no-store"})
