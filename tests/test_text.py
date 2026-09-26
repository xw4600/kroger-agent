from kroger.text import normalize_name, similarity


def test_normalize_strips_size_and_units():
    assert normalize_name("a2 Milk® Vitamin D Whole Milk, 59 fl oz") == "a2 milk vitamin d whole milk"
    assert normalize_name("Kroger 2% Reduced Fat Milk, 1 gal") == "kroger 2 reduced fat milk"
    assert normalize_name("Greek Yogurt, 32 oz") == "greek yogurt"


def test_normalize_keeps_digits_that_are_not_sizes():
    """Stripping the number+unit pair must not eat brand digits."""
    assert "a2" in normalize_name("a2 Milk Whole, 59 fl oz")
    assert "5" in normalize_name("5-hour energy, 2 fl oz")
    assert "40" not in normalize_name("40 oz tub")
    assert "2" in normalize_name("Kroger 2% Milk")


def test_normalize_collapses_adjacent_repeats_only():
    # Adjacent runs are receipt noise.
    assert normalize_name("Kroger Colby Cheese Cheese Block") == "kroger colby cheese block"
    # Non-adjacent duplicates carry meaning and must survive.
    assert "chicken" in normalize_name("Chicken Chicken Broth 32 oz")


def test_normalize_handles_empty():
    assert normalize_name("") == ""
    assert normalize_name(None) == ""


def test_similarity_ranks_near_matches_higher():
    target = "a2 Milk Vitamin D Whole Milk 59 fl oz"
    close = similarity(target, "a2 Milk Whole 59 oz")
    far = similarity(target, "Sourdough Bread Loaf")
    assert close > 0.5
    assert far < 0.2


def test_similarity_separates_milks():
    """The failure mode that matters: do not merge whole and skim milk."""
    assert similarity("Whole Milk 1 gal", "Skim Milk 1 gal") < 0.6
