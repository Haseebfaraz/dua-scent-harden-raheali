from app.fragrance.combination_key import create_combination_key


def test_order_independent_hybrid():
    a = create_combination_key(["The Opera", "Water of Arabia"])
    b = create_combination_key(["Water of Arabia", "The Opera"])
    assert a == b


def test_order_independent_tribrid():
    a = create_combination_key(["A", "B", "C"])
    b = create_combination_key(["C", "A", "B"])
    c = create_combination_key(["B", "C", "A"])
    assert a == b == c


def test_order_independent_quadbrid():
    a = create_combination_key(["A", "B", "C", "D"])
    b = create_combination_key(["D", "C", "B", "A"])
    assert a == b


def test_normalizes_case_and_punctuation_before_keying():
    a = create_combination_key(["The Opera!", "water of arabia"])
    b = create_combination_key(["  Water Of Arabia  ", "THE OPERA"])
    assert a == b


def test_different_component_sets_produce_different_keys():
    a = create_combination_key(["The Opera", "Water of Arabia"])
    b = create_combination_key(["The Opera", "Gone Swimming"])
    assert a != b
