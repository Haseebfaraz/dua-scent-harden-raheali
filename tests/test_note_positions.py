from app.fragrance.note_positions import assign_note_positions


def test_classifies_spec_worked_example():
    notes = ["Bergamot", "Mandarin", "Sea Salt", "Green Tea", "Pear", "Sage", "Ambrette", "Musk", "Sandalwood", "Benzoin"]
    result = assign_note_positions(notes)
    assert {"Bergamot", "Mandarin", "Sea Salt"} <= set(result["top"])
    assert {"Green Tea", "Pear", "Sage"} <= set(result["middle"])
    assert {"Musk", "Sandalwood", "Benzoin"} <= set(result["base"])


def test_never_invents_a_note():
    notes = ["Bergamot", "Cardamom", "Patchouli", "Guaiac Wood", "Vanilla"]
    result = assign_note_positions(notes)
    all_returned = result["top"] + result["middle"] + result["base"]
    for returned in all_returned:
        assert returned in notes


def test_never_duplicates_a_note():
    notes = ["Bergamot", "Bergamot", "Musk", "Vanilla", "Musk"]
    result = assign_note_positions(notes)
    all_returned = result["top"] + result["middle"] + result["base"]
    assert len({n.lower() for n in all_returned}) == len(all_returned)


def test_caps_every_bucket_at_5():
    many_top = [f"Citrus Note {i}" for i in range(10)]
    result = assign_note_positions(many_top)
    assert len(result["top"]) <= 5


def test_tops_up_thin_bucket_to_3():
    notes = ["Bergamot", "Lemon", "Lime", "Mandarin", "Orange", "Grapefruit", "Musk"]
    result = assign_note_positions(notes)
    assert len(result["base"]) >= 1
    all_returned = result["top"] + result["middle"] + result["base"]
    for returned in all_returned:
        assert returned in notes


def test_unrecognized_note_defaults_to_middle():
    result = assign_note_positions(["Zzznotarealnoteatall"])
    assert "Zzznotarealnoteatall" in result["middle"]


def test_keeps_liked_notes_over_unrelated_ones_over_cap():
    notes = ["Lily of the Valley", "Ambrette", "Freesia", "Geranium", "Osmanthus", "Apple"]
    without_likes = assign_note_positions(notes)
    assert "Apple" not in without_likes["middle"]

    with_likes = assign_note_positions(notes, ["fruity"])
    assert "Apple" in with_likes["middle"]


def test_prioritizes_literal_named_note_over_same_family():
    notes = ["Mango", "Pear", "Blackcurrant", "Guava", "Apricot", "Peach", "Lily of the Valley"]
    family_only = assign_note_positions(notes, ["fruity"])
    assert "Peach" not in family_only["middle"]

    with_literal = assign_note_positions(notes, ["fruity"], ["peach"])
    assert "Peach" in with_literal["middle"]


def test_no_word_boundary_false_positive_apple_pineapple():
    notes = ["Mango", "Pear", "Blackcurrant", "Guava", "Apricot", "Pineapple"]
    without_literal_term = assign_note_positions(notes, ["fruity"])
    with_false_literal_term = assign_note_positions(notes, ["fruity"], ["apple"])
    assert with_false_literal_term == without_literal_term
