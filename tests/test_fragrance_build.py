from app.services.fragrance_build import compute_default_ratios, compute_note_position_buckets


def test_compute_note_position_buckets_dedupes_and_classifies():
    products = [{"title": "Rose Oud", "notes": ["Rose", "Oud", "rose"]}, {"title": "Citrus Splash", "notes": ["Bergamot"]}]
    buckets = compute_note_position_buckets(products)
    all_notes = buckets["top"] + buckets["middle"] + buckets["base"]
    assert len(all_notes) == len(set(n.lower() for n in all_notes))  # deduped, case-insensitive
    assert "Bergamot" in buckets["top"]  # citrus is a real top note


def test_compute_default_ratios_weighted_by_note_counts():
    ratios = compute_default_ratios({"top": ["a", "b"], "middle": ["c"], "base": ["d"]})
    assert sum(ratios.values()) == 100
    assert ratios["top"] > ratios["middle"] == ratios["base"]


def test_compute_default_ratios_handles_empty_buckets_without_crashing():
    ratios = compute_default_ratios({"top": [], "middle": [], "base": []})
    assert ratios == {"top": 34, "middle": 33, "base": 33}
    assert sum(ratios.values()) == 100
