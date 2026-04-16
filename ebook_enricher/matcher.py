"""Fuzzy matching gate for Hardcover search results.

Returns True only when both title and author similarity exceed the threshold.
Separate thresholds are deliberate: a wrong-author match is a worse failure
than a wrong-subtitle match.
"""
from rapidfuzz import fuzz

TITLE_THRESHOLD = 80
AUTHOR_THRESHOLD = 80


def is_confident_match(
    epub_title: str,
    epub_author: str,
    hc_title: str,
    hc_author: str,
) -> bool:
    title_score = max(
        fuzz.token_set_ratio(epub_title.lower(), hc_title.lower()),
        fuzz.partial_ratio(epub_title.lower(), hc_title.lower()),
    )
    author_score = max(
        fuzz.token_set_ratio(epub_author.lower(), hc_author.lower()),
        fuzz.partial_ratio(epub_author.lower(), hc_author.lower()),
    )
    return title_score >= TITLE_THRESHOLD and author_score >= AUTHOR_THRESHOLD
