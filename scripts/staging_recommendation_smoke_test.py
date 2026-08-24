"""Staging persistence round trip (Conversation -> Message -> CustomerProfileState ->
FragranceRecommendation -> RecommendationInventorySnapshot -> reload) plus one real
recommendation run through the actual engine/Odoo code -- not fabricated fixtures.

Uses the app's own DATABASE_URL setting, so point it at staging before running:
    $env:DATABASE_URL = $env:STAGING_DATABASE_URL
    python scripts/staging_recommendation_smoke_test.py

Refuses to run unless the live connection's current_database() matches
EXPECTED_STAGING_DATABASE (same guard as the seeder -- never trusts the env var name alone).
Deletes everything it creates when done (pass --keep to leave the rows in place for inspection).
"""

import argparse
import asyncio
import logging
import os

from sqlalchemy import delete, text

from app.db.ids import new_id
from app.db.models import Conversation, CustomerProfileState, FragranceRecommendation, Message
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.services.conversation import create_or_update_conversation, get_conversation_history, save_message
from app.services.customer_profile import get_customer_profile, get_missing_required_fields, save_customer_profile_fields
from app.services.inventory_snapshot import get_inventory_snapshot, save_inventory_snapshot
from app.services.odoo_inventory import evaluate_candidate_inventory
from app.services.order_history import analyze_customer_product_candidates
from app.services.recommendation_confirmation import get_recommendation, save_recommendation
from app.services.recommendation_engine import generate_new_product_combinations

logger = logging.getLogger("staging_smoke_test")

# Step 8's requested profile: fresh + slightly sweet, summer, work/daily, no dislikes.
SMOKE_TEST_PROFILE = {
    "city": "Miami", "stateRegion": "Florida", "country": "United States", "locationVerified": True,
    "preferredStyle": "fresh, slightly sweet", "occasion": "work / daily", "occasionAsked": True,
    "dislikes": [], "dislikesAsked": True, "requestedSeasonStyle": "Summer",
}


async def _assert_expected_database(session, expected_db: str) -> None:
    actual = (await session.execute(text("SELECT current_database()"))).scalar()
    if actual != expected_db:
        raise SystemExit(f'ABORT IMMEDIATELY: DATABASE_URL points at "{actual}", expected "{expected_db}". Refusing to write.')


async def _cleanup(session, conversation_id: str, recommendation_id: str | None) -> None:
    # Deleting FragranceRecommendation/Conversation cascades their snapshot/components and
    # messages via the real Postgres FK constraints -- nothing to delete manually beyond this.
    if recommendation_id:
        await session.execute(delete(FragranceRecommendation).where(FragranceRecommendation.id == recommendation_id))
    await session.execute(delete(Conversation).where(Conversation.id == conversation_id))
    await session.execute(delete(CustomerProfileState).where(CustomerProfileState.conversationId == conversation_id))
    await session.commit()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-database", default=os.environ.get("EXPECTED_STAGING_DATABASE", "dua_scent_ai_staging"))
    parser.add_argument("--keep", action="store_true", help="Leave the created rows in place instead of deleting them.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    conversation_id = new_id()
    recommendation_id = None

    async with SessionLocal() as session:
        await _assert_expected_database(session, args.expected_database)

        # ---- persistence round trip ----
        await create_or_update_conversation(session, conversation_id, customer_email="smoketest@example.invalid", customer_name="Smoke Test")
        await save_message(session, conversation_id, "user", "smoke test message")
        logger.info("PASS: Conversation + Message created")

        profile = await save_customer_profile_fields(session, conversation_id, dict(SMOKE_TEST_PROFILE))
        missing = get_missing_required_fields(profile)
        if missing:
            logger.error("FAIL: profile not ready for analysis, missing: %s", missing)
            await _cleanup(session, conversation_id, recommendation_id)
            return 1
        logger.info("PASS: CustomerProfileState saved and ready for analysis")

        history = await get_conversation_history(session, conversation_id)
        reloaded_profile = await get_customer_profile(session, conversation_id)
        if len(history) != 1 or reloaded_profile.get("city") != "Miami":
            logger.error("FAIL: Conversation/Message/CustomerProfileState did not reload as written")
            await _cleanup(session, conversation_id, recommendation_id)
            return 1
        logger.info("PASS: Conversation/Message/CustomerProfileState reload verified")

        try:
            # ---- recommendation smoke test (real engine, real Odoo call, no fixtures) ----
            queried_profile = {**profile, "season": profile.get("requestedSeasonStyle") or "Summer"}

            candidates = await analyze_customer_product_candidates(session, queried_profile)
            if not candidates:
                logger.error("FAIL: no candidate products returned for this profile")
                return 1
            logger.info("PASS: %d candidate products returned (real FragranceProduct rows)", len(candidates))

            combinations = await generate_new_product_combinations(session, profile=queried_profile, candidate_products=candidates)
            if not combinations:
                logger.error("FAIL: no combinations generated from these candidates")
                return 1
            logger.info(
                "PASS: %d combination(s) generated (types: %s)",
                len(combinations), sorted({c["type"] for c in combinations}),
            )

            best = combinations[0]
            inventory = await evaluate_candidate_inventory(session, best)
            logger.info(
                "Odoo feasibility: buildable=%s inventoryValidated=%s status=%s",
                inventory["buildable"], inventory["inventoryValidated"], inventory["status"],
            )

            recommendation_id = await save_recommendation(session, conversation_id=conversation_id, profile=queried_profile, combination=best)
            logger.info("PASS: FragranceRecommendation persisted (id=%s, type=%s)", recommendation_id, best["type"])

            snapshot = await save_inventory_snapshot(
                session, recommendation_id=recommendation_id, inventory_validated=inventory["inventoryValidated"],
                buildable=inventory["buildable"], checked_at=utcnow(), oil_total_ml=inventory["oilTotalMl"] or 0.0,
                alcohol_ml=inventory["alcoholMl"] or 0.0, request_status=inventory["status"],
                max_buildable_bottles=inventory["maxBuildableBottles"], limiting_sku=inventory["limitingSku"],
                components=inventory["components"],
            )
            logger.info("PASS: RecommendationInventorySnapshot persisted (id=%s)", snapshot.id)

            if await get_recommendation(session, recommendation_id) is None or await get_inventory_snapshot(session, recommendation_id) is None:
                logger.error("FAIL: could not reload the recommendation/snapshot just created")
                return 1
            logger.info("PASS: recommendation + inventory snapshot reload verified")
        finally:
            if not args.keep:
                await _cleanup(session, conversation_id, recommendation_id)
                logger.info("Cleanup done -- smoke-test rows removed.")
            else:
                logger.info("--keep set -- left conversation_id=%s recommendation_id=%s in place.", conversation_id, recommendation_id)

    logger.info("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
