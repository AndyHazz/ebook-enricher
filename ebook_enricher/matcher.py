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


def is_confident_match(
    epub_title: str,
    epub_author: str,
    hc_title: str,
    hc_author: str,
) -> bool:
    # Empty fields on either side are always a hard miss — partial_ratio('', '')
    # returns 100 and would otherwise produce silent false accepts.
    if not (epub_title and epub_title.strip()):
        return False
    if not (epub_author and epub_author.strip()):
        return False
    if not (hc_title and hc_title.strip()):
        return False
    if not (hc_author and hc_author.strip()):
        return False

    title_score = max(
        fuzz.token_set_ratio(epub_title.lower(), hc_title.lower()),
        fuzz.partial_ratio(epub_title.lower(), hc_title.lower()),
    )
    author_score = max(
        fuzz.token_set_ratio(epub_author.lower(), hc_author.lower()),
        fuzz.partial_ratio(epub_author.lower(), hc_author.lower()),
    )
    return title_score >= TITLE_THRESHOLD and author_score >= AUTHOR_THRESHOLD
