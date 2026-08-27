from app.ai.prompt import (
    _BRAND_NAME_PATTERN,
    _EARLY_PHASE_TEMPLATE,
    _FULL_DISCOVERY_TEMPLATE,
    has_concrete_context,
    validate_customer_response,
)


def test_bare_mood_filler_replies_have_no_context():
    assert has_concrete_context("great yours?") is False
    assert has_concrete_context("testing") is False
    assert has_concrete_context("good") is False
    assert has_concrete_context("not well") is False
    assert has_concrete_context("") is False
    assert has_concrete_context(None) is False


def test_generic_routine_mention_has_no_context():
    assert has_concrete_context("going to the gym later") is False
    assert has_concrete_context("big meeting today") is False
    assert has_concrete_context("just going to office and working") is False
    assert has_concrete_context("school as usual") is False


def test_specific_occasion_gift_relationship_fragrance_word_has_context():
    assert has_concrete_context("it's my wife's birthday") is True
    assert has_concrete_context("need a gift for my husband") is True
    assert has_concrete_context("I want to buy a perfume") is True
    assert has_concrete_context("I have a job interview tomorrow") is True


# ---------------------------------------------------------------------------
# Strict customer-facing brand + product-title privacy
# ---------------------------------------------------------------------------

def test_validator_flags_brand_name_mention():
    assert "brand_name_mention" in validate_customer_response("I can build you a DUA fragrance.")


def test_validator_ignores_words_that_merely_contain_the_brand_letters():
    # Word-boundary matched -- must not false-positive on an unrelated word.
    assert "brand_name_mention" not in validate_customer_response("This is a dual-purpose scent for day and night.")


def test_validator_flags_sku_like_value():
    assert "sku_like_value" in validate_customer_response("The blend uses OIL-PYTEST-BATCH-0 as its base.")


def test_validator_flags_blocked_product_titles():
    text = "I combined Midnight Saffron Reserve with something else for a bold direction."
    violations = validate_customer_response(text, blocked_product_titles=["Midnight Saffron Reserve"])
    assert any(v.startswith("blocked_product_title:") for v in violations)


def test_validator_clean_recommendation_explanation_has_no_privacy_violations():
    text = "I kept the opening crisp and lively, then gave it enough depth underneath to stay noticeable through the day without becoming too sweet."
    violations = validate_customer_response(text, blocked_product_titles=["Midnight Saffron Reserve"])
    assert "brand_name_mention" not in violations
    assert "sku_like_value" not in violations
    assert not any(v.startswith("blocked_product_title:") for v in violations)


def test_persona_templates_never_name_the_brand():
    # Word-boundary matched -- "individually" legitimately contains the substring "dua" and must
    # not be treated as a leak.
    assert _BRAND_NAME_PATTERN.search(_EARLY_PHASE_TEMPLATE) is None
    assert _BRAND_NAME_PATTERN.search(_FULL_DISCOVERY_TEMPLATE) is None
