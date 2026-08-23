from app.ai.prompt import has_concrete_context


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
