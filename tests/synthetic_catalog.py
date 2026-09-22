"""A small SYNTHETIC catalog for deterministic tests that only need "a product", "a hybrid" or an
order-history row to exist (Phase 7, F12).

It is not the production catalog and proves nothing about it: ranking tests that assert what the
real catalog contains stay behind the `reference_data` marker (docs/PLATFORM_MODERNIZATION.md).

Titles are the public storefront names the affected tests already hard-code, with the note lists
those tests already carry; nothing here comes from a database export. Rows are created per test
and removed afterwards; none of the titles contains "pytest", because one legacy test filters that
substring out on purpose.
"""

from datetime import timedelta

from sqlalchemy import delete

from app.db.ids import new_id
from app.db.models import ExistingCombination, FragranceProduct, OrderHistory, ProductRegionSummary
from app.db.session import SessionLocal
from app.db.time import utcnow
from app.fragrance.combination_key import create_combination_key
from app.fragrance.normalization import normalize_product_name

PRODUCTS = [
    {"title": "The Opera", "notes": ["Rose", "Fruity Notes", "Ambergris", "Leather", "Nutmeg", "Cedar", "Vanilla", "Musk"], "family": "Amber", "price": 22.0},
    {"title": "Water of Arabia", "notes": ["Mandarin", "Bergamot", "Blackcurrant", "Green Tea", "Sandalwood"], "family": "Fresh", "price": 18.0},
    {"title": "Arabian Amber Nuit", "notes": ["Amber", "Rose", "Grapefruit", "Bergamot", "Pink Pepper"], "family": "Amber", "price": 20.0},
    {"title": "Leather Oud", "notes": ["Leather", "Suede", "Raspberry", "Amber"], "family": "Woody", "price": 24.0},
    {"title": "Green Vetiver Grove", "notes": ["Vetiver", "Moss", "Galbanum", "Green Tea"], "family": "Green", "price": 19.0},
    {"title": "Citrus Morning", "notes": ["Lemon", "Bergamot", "Neroli", "White Musk"], "family": "Citrus", "price": 17.0},
]
# One real-shaped HYBRID that exists already (so "exists regardless of order" and "appears as a
# component in" can be tested) built from the two products no other test tries to recommend.
EXISTING_HYBRID = {"title": "Green Morning", "components": ["Green Vetiver Grove", "Citrus Morning"]}
ORDER_CITY = {"city": "Los Angeles", "stateName": "California", "countryName": "United States"}


async def seed_synthetic_catalog() -> dict:
    """Insert the rows and return the ids to remove. Existing rows with the same normalized title
    (a developer's local database, for example) are left alone and NOT removed afterwards."""
    created = {"products": [], "combinations": [], "orders": [], "summaries": []}
    now = utcnow()
    async with SessionLocal() as session:
        from sqlalchemy import select

        for product in PRODUCTS:
            normalized = normalize_product_name(product["title"])
            if await session.scalar(select(FragranceProduct.id).where(FragranceProduct.normalizedTitle == normalized)):
                continue
            row = FragranceProduct(id=new_id(), title=product["title"], normalizedTitle=normalized, notesJson=product["notes"], notesRaw=", ".join(product["notes"]),
                                   fragranceFamily=product["family"], pricePer5ml=product["price"], createdAt=now, updatedAt=now)
            session.add(row)
            created["products"].append(row.id)
        # In the catalog a hybrid is also a product row of its own (its notes are the union).
        hybrid_normalized = normalize_product_name(EXISTING_HYBRID["title"])
        if not await session.scalar(select(FragranceProduct.id).where(FragranceProduct.normalizedTitle == hybrid_normalized)):
            union = [n for p in PRODUCTS if p["title"] in EXISTING_HYBRID["components"] for n in p["notes"]]
            row = FragranceProduct(id=new_id(), title=EXISTING_HYBRID["title"], normalizedTitle=hybrid_normalized, notesJson=list(dict.fromkeys(union)), notesRaw=", ".join(union),
                                   fragranceFamily="Green", pricePer5ml=21.0, createdAt=now, updatedAt=now)
            session.add(row)
            created["products"].append(row.id)
        key = create_combination_key(EXISTING_HYBRID["components"])
        if not await session.scalar(select(ExistingCombination.id).where(ExistingCombination.componentKey == key)):
            row = ExistingCombination(id=new_id(), title=EXISTING_HYBRID["title"], normalizedTitle=normalize_product_name(EXISTING_HYBRID["title"]), type="HYBRID",
                                      componentProductsJson=list(EXISTING_HYBRID["components"]), componentKey=key, createdAt=now)
            session.add(row)
            created["combinations"].append(row.id)
        for index, product in enumerate(PRODUCTS[:3]):
            for n in range(3):
                row = OrderHistory(id=new_id(), orderDate=(now - timedelta(days=30 * n)).date().isoformat(), season="Summer", classification="fresh",
                                   notes=", ".join(product["notes"]), productName=product["title"], normalizedProductName=normalize_product_name(product["title"]),
                                   customerKeyHash=f"synthetic-customer-{index}-{n}", createdAt=now, **ORDER_CITY)
                session.add(row)
                created["orders"].append(row.id)
            row = ProductRegionSummary(id=new_id(), normalizedProductName=normalize_product_name(product["title"]), scope="season", scopeValue="Summer",
                                       orderCount=3, distinctCustomerCount=3, repeatCustomerCount=0, updatedAt=now)
            session.add(row)
            created["summaries"].append(row.id)
        await session.commit()
    return created


async def remove_synthetic_catalog(created: dict) -> None:
    async with SessionLocal() as session:
        await session.execute(delete(ProductRegionSummary).where(ProductRegionSummary.id.in_(created["summaries"] or ["-"])))
        await session.execute(delete(OrderHistory).where(OrderHistory.id.in_(created["orders"] or ["-"])))
        await session.execute(delete(ExistingCombination).where(ExistingCombination.id.in_(created["combinations"] or ["-"])))
        await session.execute(delete(FragranceProduct).where(FragranceProduct.id.in_(created["products"] or ["-"])))
        await session.commit()
