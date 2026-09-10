"""The single authoritative validator for customer-controlled Save Build input (Phase 1, N1).

Every path that turns browser or model influenced input into a Shopify product, variant, price,
or title must go through these two functions: first-time creation, re-pricing on an existing
build, the preview page's recreate / save_build / add_to_cart intents, and the direct theme
slider endpoint. There are deliberately no weaker per-caller variants.

Ratios
------
The preview page (app/static/js/fragrance_preview.js) uses integer percentage sliders
(min 0 / max 100 in the markup, `Math.round` on every change, and `adjustRatios` never lets a
layer drop below MIN_PCT = 5 while dragging). The server contract is therefore INTEGER
percentages. A layer must be present in the bottle (>= MIN_LAYER_PERCENT) and the three layers
must total exactly 100: the bottle is always exactly one complete composition, which is also what
makes the price computation impossible to shrink by omitting or under-filling a layer.

MIN_LAYER_PERCENT is 1 rather than the UI's 5 because the server's own default split
(app/services/fragrance_build.py::compute_default_ratios) can legitimately produce a layer below
5% for a heavily base-weighted blend, and a customer who saves that default unchanged must not
be rejected. Zero layers are not allowed: the product's option values name every layer.

Name
----
Input-integrity only (not content policy): trimmed, Unicode NFC-normalized, internal whitespace
collapsed, no control/format/line-separator characters, bounded length. Shopify's own title
limit is 255; MAX_NAME_LENGTH is well under that.
"""

import math
import unicodedata
from typing import Any

POSITIONS = ("top", "middle", "base")
MIN_LAYER_PERCENT = 1
MAX_LAYER_PERCENT = 100 - MIN_LAYER_PERCENT * (len(POSITIONS) - 1)
TOTAL_PERCENT = 100

MAX_NAME_LENGTH = 80
_REJECTED_NAME_CATEGORIES = ("Cc", "Cf", "Cs", "Co", "Cn", "Zl", "Zp")


class InvalidRatios(ValueError):
    """Customer-safe message; never echoes the raw input."""


class InvalidCustomName(ValueError):
    """Customer-safe message; never echoes the raw input."""


def _as_integer_percent(value: Any) -> int:
    # bool is a subclass of int in Python -- reject it explicitly.
    if isinstance(value, bool):
        raise InvalidRatios("Each layer percentage must be a whole number.")
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise InvalidRatios("Each layer percentage must be a finite whole number.")
        if value != int(value):
            raise InvalidRatios("Each layer percentage must be a whole number.")
        return int(value)
    # Strings, Decimals, None, lists, nested dicts: never coerced.
    raise InvalidRatios("Each layer percentage must be a whole number.")


def validate_ratios(raw: Any) -> dict[str, int]:
    """Return {"top": int, "middle": int, "base": int} or raise InvalidRatios."""
    if not isinstance(raw, dict):
        raise InvalidRatios("Ratios must be an object with top, middle, and base percentages.")
    keys = set(raw.keys())
    if keys != set(POSITIONS):
        raise InvalidRatios("Ratios must contain exactly the top, middle, and base layers.")
    validated: dict[str, int] = {}
    for position in POSITIONS:
        pct = _as_integer_percent(raw[position])
        if pct < MIN_LAYER_PERCENT or pct > MAX_LAYER_PERCENT:
            raise InvalidRatios(f"Each layer must be between {MIN_LAYER_PERCENT}% and {MAX_LAYER_PERCENT}%.")
        validated[position] = pct
    if sum(validated.values()) != TOTAL_PERCENT:
        raise InvalidRatios("Top, middle, and base percentages must add up to exactly 100%.")
    return validated


def validate_custom_name(raw: Any, *, required: bool = False) -> str | None:
    """Return the cleaned name, or None when no name was supplied (and not required)."""
    if raw is None:
        if required:
            raise InvalidCustomName("A fragrance name is required.")
        return None
    if not isinstance(raw, str):
        raise InvalidCustomName("The fragrance name must be text.")
    # Bound the work before normalizing: a multi-kilobyte title is rejected outright.
    if len(raw) > MAX_NAME_LENGTH * 4:
        raise InvalidCustomName(f"The fragrance name must be at most {MAX_NAME_LENGTH} characters.")
    normalized = unicodedata.normalize("NFC", raw).strip()
    if not normalized:
        # Whitespace-only (including newlines) means "no name given", not an attack.
        if required:
            raise InvalidCustomName("A fragrance name is required.")
        return None
    if any(unicodedata.category(ch) in _REJECTED_NAME_CATEGORIES for ch in normalized):
        raise InvalidCustomName("The fragrance name contains characters that cannot be used.")
    cleaned = " ".join(normalized.split())
    if len(cleaned) > MAX_NAME_LENGTH:
        raise InvalidCustomName(f"The fragrance name must be at most {MAX_NAME_LENGTH} characters.")
    return cleaned
