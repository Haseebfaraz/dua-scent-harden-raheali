from app.ai.refinement import derive_refinement_adjustments


def test_negated_family_goes_to_dislikes_not_likes():
    result = derive_refinement_adjustments("less sweet")
    assert "Sweet" in result["addDislikes"]
    assert "Sweet" not in result["addLikes"]


def test_each_clause_resolves_its_own_negation():
    result = derive_refinement_adjustments("less woody, more fruity")
    assert "Woody" in result["addDislikes"]
    assert "Fruity" in result["addLikes"]
    assert "Fruity" not in result["addDislikes"]
    assert "Woody" not in result["addLikes"]


def test_generic_negation_phrasing():
    assert "Musk" in derive_refinement_adjustments("no musk please")["addDislikes"]
    assert "Powdery" in derive_refinement_adjustments("I don't want it too powdery")["addDislikes"]
    assert "Strong" in derive_refinement_adjustments("can you take out the heavy notes")["addDislikes"]


def test_recognizes_literal_note_names():
    result = derive_refinement_adjustments("remove patchouli, vanilla, sandalwood")
    assert any("wood" in f.lower() for f in result["addDislikes"])
    assert any("sweet" in f.lower() for f in result["addDislikes"])
    assert len(result["addLikes"]) == 0


def test_negation_carries_forward_across_clauses():
    result = derive_refinement_adjustments("remove patchouli vanilla, Sandal wood")
    assert any("wood" in f.lower() for f in result["addDislikes"])
    assert not any("wood" in f.lower() for f in result["addLikes"])


def test_persists_literal_note_not_family_label():
    result = derive_refinement_adjustments("dont want sandalwood")
    assert "sandalwood" in result["addDislikeTerms"]
    assert "woody" not in result["addDislikeTerms"]


def test_falls_back_to_family_label_for_pure_style_word():
    result = derive_refinement_adjustments("less woody")
    assert any("wood" in t.lower() for t in result["addDislikeTerms"])


def test_keeps_each_literal_note_separate_in_a_list():
    result = derive_refinement_adjustments("remove patchouli, vanilla, sandalwood")
    assert "patchouli" in result["addDislikeTerms"]
    assert "sandalwood" in result["addDislikeTerms"]


def test_exclusion_wins_over_incidental_positive_mention():
    result = derive_refinement_adjustments("less woody, more fruity")
    assert not any("wood" in t.lower() for t in result["addLikeTerms"])


def test_current_notes_recognizes_note_with_no_family_entry():
    result = derive_refinement_adjustments("dont want jackfruit", ["Gin", "Mojito", "Jackfruit", "Pear"])
    assert "jackfruit" in result["addDislikeTerms"]


def test_current_notes_ignores_unmentioned_entry():
    result = derive_refinement_adjustments("dont want jackfruit", ["Gin", "Mojito", "Jackfruit", "Pear"])
    assert "pear" not in result["addDislikeTerms"]


def test_works_without_current_notes():
    result = derive_refinement_adjustments("dont want sandalwood")
    assert "sandalwood" in result["addDislikeTerms"]


def test_allowed_types_from_type_keyword():
    assert derive_refinement_adjustments("give me a Hybrid only")["allowedTypes"] == ["HYBRID"]


def test_allowed_types_none_when_no_type_named():
    assert derive_refinement_adjustments("make it fresher")["allowedTypes"] is None
