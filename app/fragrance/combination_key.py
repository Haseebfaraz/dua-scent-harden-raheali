"""Port of app/utils/combinationKey.js -- order-independent identity key for a set of component
products, so ExistingCombination.componentKey stays a real unique constraint.
"""

from app.fragrance.normalization import normalize_product_name


def create_combination_key(product_titles: list[str]) -> str:
    normalized = [normalize_product_name(t) for t in product_titles]
    return "||".join(sorted(n for n in normalized if n))
