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
from typing import Optional

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


# Markers that identify a hit as an adaptation or a multi-book collection
# rather than the canonical single novel. Matched case-insensitively as
# substrings of the title and/or the Hardcover series name.
_ADAPTATION_MARKERS = (
    "graphic novel", "graphic novels",
    "on radio", "radio drama",
    "audio drama", "audiobook", "audio book",
)
_COLLECTION_MARKERS = (
    "omnibus", "box set", "boxed set",
    "collection set", "books collection",
    "complete series", "complete novels",
)


def normalise_series_name(name: Optional[str]) -> str:
    """Lowercase, strip surrounding whitespace, drop a leading 'the '.
    So 'The Culture' and 'Culture' compare equal. Returns '' for falsy input."""
    if not name:
        return ""
    n = name.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    return n


def is_non_canonical(title: Optional[str], series_name: Optional[str]) -> bool:
    """True if this hit looks like an adaptation (radio/graphic/audio) or a
    multi-book collection (box set/omnibus), based on keyword markers in the
    title or series name, OR a title that enumerates several books (>= 2
    ' / '-separated segments — the shape of a box-set contents list).

    Heuristic and deliberately conservative: only used to RANK candidates
    lower, never to exclude them."""
    hay_title = (title or "").lower()
    hay_series = (series_name or "").lower()
    for marker in _ADAPTATION_MARKERS:
        if marker in hay_title or marker in hay_series:
            return True
    for marker in _COLLECTION_MARKERS:
        if marker in hay_title or marker in hay_series:
            return True
    if hay_title.count(" / ") >= 2:
        return True
    return False
