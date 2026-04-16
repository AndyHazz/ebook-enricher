from ebook_enricher.matcher import is_confident_match


def test_exact_match_passes():
    assert is_confident_match(
        "Dungeon Crawler Carl", "Matt Dinniman",
        "Dungeon Crawler Carl", "Matt Dinniman",
    ) is True


def test_subtitle_variant_passes():
    # EPUB has "All the Skills: A Deckbuilding LitRPG"
    # Hardcover has "All the Skills"
    assert is_confident_match(
        "All the Skills: A Deckbuilding LitRPG", "Honour Rae",
        "All the Skills", "Honour Rae",
    ) is True


def test_different_book_fails():
    assert is_confident_match(
        "All the Skills", "Honour Rae",
        "The Skills of Success", "Different Author",
    ) is False


def test_same_title_wrong_author_fails():
    assert is_confident_match(
        "The Expanse", "James S. A. Corey",
        "The Expanse", "Someone Else Entirely",
    ) is False


def test_minor_punctuation_passes():
    assert is_confident_match(
        "Sea of Tranquility: A novel", "Emily St. John Mandel",
        "Sea of Tranquility", "Emily St. John Mandel",
    ) is True
