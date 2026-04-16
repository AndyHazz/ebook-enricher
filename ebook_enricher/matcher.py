"""Fuzzy matching gate for Hardcover search results.

Returns True only when both title and author similarity exceed the threshold.
Title and author scores are gated independently (both must pass) and use the
same 80 threshold today. The thresholds are kept as separate constants so
they can be tuned independently if field-level calibration needs diverge.

Scoring uses ``max(token_set_ratio, partial_ratio)``:
  - ``token_set_ratio`` handles word-order variation (e.g. "Firstname Lastname"
    vs "Lastname, Firstname").
  - ``partial_ratio`` handles substring/subtitle containment (e.g. "Title" as
    a prefix of "Title: Subtitle").

Empty or whitespace-only inputs on either side are treated as a hard miss —
``partial_ratio('', '')`` returns 100 and would otherwise produce false
positives when metadata is sparse.
"""
from rapidfuzz import fuzz

TITLE_THRESHOLD = 80
AUTHOR_THRESHOLD = 80


def _has_content(s: str) -> bool:
    return bool(s and s.strip())


def score_match(
    epub_title: str,
    epub_author: str,
    hc_title: str,
    hc_author: str,
) -> tuple[int, int]:
    """Return (title_score, author_score) 0-100. Empty inputs score 0.

    Used both to gate a single candidate and to rank multiple candidates
    so we can pick the best match rather than the first acceptable one.
    """
    if not all(_has_content(s) for s in (epub_title, epub_author, hc_title, hc_author)):
        return 0, 0
    title_score = max(
        fuzz.token_set_ratio(epub_title.lower(), hc_title.lower()),
        fuzz.partial_ratio(epub_title.lower(), hc_title.lower()),
    )
    author_score = max(
        fuzz.token_set_ratio(epub_author.lower(), hc_author.lower()),
        fuzz.partial_ratio(epub_author.lower(), hc_author.lower()),
    )
    return title_score, author_score


def is_confident_match(
    epub_title: str,
    epub_author: str,
    hc_title: str,
    hc_author: str,
) -> bool:
    title_score, author_score = score_match(epub_title, epub_author, hc_title, hc_author)
    return title_score >= TITLE_THRESHOLD and author_score >= AUTHOR_THRESHOLD
