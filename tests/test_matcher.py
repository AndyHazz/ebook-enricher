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


def test_different_title_same_author_fails():
    # Title gate must reject a clearly different book by the same author.
    assert is_confident_match(
        "Dungeon Crawler Carl", "Matt Dinniman",
        "Carl's Really Different Adventure", "Matt Dinniman",
    ) is False


def test_empty_epub_author_fails():
    # An EPUB missing dc:creator must never match, even with a perfect title.
    assert is_confident_match(
        "Dungeon Crawler Carl", "",
        "Dungeon Crawler Carl", "Matt Dinniman",
    ) is False


def test_empty_hc_author_fails():
    # A Hardcover result with no contributions must never match.
    assert is_confident_match(
        "Dungeon Crawler Carl", "Matt Dinniman",
        "Dungeon Crawler Carl", "",
    ) is False


def test_empty_everything_fails():
    # All four empty: the worst-case degenerate input must not return True.
    assert is_confident_match("", "", "", "") is False
