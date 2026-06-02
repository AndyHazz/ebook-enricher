# Canonical-Edition Match Preference — Design

**Date**: 2026-06-02
**Status**: Approved for implementation

## Problem

The first live `/backfill` of the series-normalisation feature mis-tagged two
books, both from the same root cause: when Hardcover's search returns several
hits with the same/closely-matching title, the matcher picks the wrong sibling.

Observed (verified against the live Hardcover API):

- **Small Gods** — three hits all titled "Small Gods":
  - `Discworld on Radio` #4 (radio adaptation) ← matcher picked this
  - `Discworld` #13 (the canonical novel) ← ignored
  - `Small Gods: A Discworld Graphic Novel` / `Discworld Graphic Novels` #4
- **Mort** — hits include:
  - `Mort: A Novel of Discworld` → `Discworld` #4 (canonical) ← ignored
  - a 5-book box set (`...novel series 1 to 5 books collection set: A / B / …`)
    → `Discworld` #1 ← matcher picked this

The current ranking key is `(title+author score, length_penalty)`. Same-titled
hits tie on score, and the length tie-break can't separate a novel from its
adaptation (identical titles) — so list order decides, and adaptations/box-sets
win by accident.

This bias affects **every field the enricher copies from the chosen hit** —
cover, description, genres — not only series. Series correction merely made it
visible because grouping is sensitive to series name/index. Fixing candidate
selection improves the whole enricher.

## Goal

Prefer the canonical novel edition when ranking confident candidates, using two
signals derived from data already in each hit:

1. **Existing-series match** — when the EPUB already has a series, prefer a
   candidate whose Hardcover series name matches it.
2. **Canonical edition** — de-prioritise adaptations (radio/graphic/audio) and
   collections (box sets/omnibuses), detected by keyword heuristics on the
   candidate's title and series name.

The 80% confidence gate is unchanged. Ranking only — never exclusion (soft), so
a book whose only Hardcover presence is an adaptation still gets matched.

## Non-goals

- **No extra Hardcover API calls.** Detection uses `HardcoverBook.title` and
  `.series_name`, both already fetched by `search_book`. Edition-format lookups
  were considered and rejected: the box-set's giveaway is its *title*, not its
  format (it may be a plain paperback like the novel), so format data would not
  catch the Mort case.
- **No change to the confidence gate** (`TITLE_THRESHOLD`/`AUTHOR_THRESHOLD`,
  both 80). This is purely about ranking among already-confident candidates.
- **No hard exclusion.** Non-canonical editions are ranked last, not dropped —
  a book with only an adaptation edition on Hardcover still enriches.
- **No new edition-picking for covers.** The existing editions-fallback for
  cover resolution is separate and unchanged.

## Constraints

- Operate only on fields already present on `HardcoverBook` (`title`,
  `series_name`) and the EPUB's `meta.series`.
- Keep `matcher.py` focused: it owns the pure detection predicate; `enrich.py`
  owns the ranking policy (it already holds the candidate loop and has
  `meta.series` in scope).
- Deterministic: equal keys must resolve the same way every run (the existing
  loop keeps the first candidate on a strict-greater comparison; preserve that).

## Architecture

```
ebook_enricher/
├── matcher.py   (MOD)  — add is_non_canonical(title, series_name) -> bool
│                         and a small series-name normaliser
└── enrich.py    (MOD)  — extend the candidate-ranking key with two terms
```

No new modules. No new dependencies. No API/schema change.

## `matcher.py` additions

```python
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


def normalise_series_name(name: str) -> str:
    """Lowercase, strip surrounding whitespace, drop a leading 'the '.
    So 'The Culture' and 'Culture' compare equal. Returns '' for falsy input."""
    if not name:
        return ""
    n = name.strip().lower()
    if n.startswith("the "):
        n = n[4:]
    return n


def is_non_canonical(title: str, series_name: str) -> bool:
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
    # Box-set contents lists: "A / B / C" (>= 2 separators => >= 3 titles).
    if hay_title.count(" / ") >= 2:
        return True
    return False
```

Note: `"N to M books"` from the Mort title is covered by `"collection set"` and
the ` / ` contents-list check, so no fragile numeric-range parsing is needed.

## `enrich.py` ranking change

Current loop (abridged):

```python
chosen = None
best_key = (-1, -(1 << 30))
for candidate in candidates:
    t_score, a_score = score_match(meta.title, meta.author, candidate.title, candidate.author)
    if t_score < TITLE_THRESHOLD or a_score < AUTHOR_THRESHOLD:
        continue
    total = t_score + a_score
    length_penalty = -abs(len(meta.title) - len(candidate.title))
    key = (total, length_penalty)
    if key > best_key:
        chosen = candidate
        best_key = key
```

New loop:

```python
from ebook_enricher.matcher import (
    AUTHOR_THRESHOLD, TITLE_THRESHOLD, score_match,
    is_non_canonical, normalise_series_name,   # NEW imports
)

existing_series_norm = normalise_series_name(meta.series)  # "" if no series

chosen = None
# 4-tuple now: (series_match, is_canonical, total, length_penalty)
best_key = (-1, -1, -1, -(1 << 30))
for candidate in candidates:
    t_score, a_score = score_match(meta.title, meta.author, candidate.title, candidate.author)
    if t_score < TITLE_THRESHOLD or a_score < AUTHOR_THRESHOLD:
        continue
    total = t_score + a_score
    length_penalty = -abs(len(meta.title) - len(candidate.title))
    series_match = int(
        bool(existing_series_norm)
        and normalise_series_name(candidate.series_name) == existing_series_norm
    )
    is_canonical = int(not is_non_canonical(candidate.title, candidate.series_name))
    key = (series_match, is_canonical, total, length_penalty)
    if key > best_key:
        chosen = candidate
        best_key = key
```

Lexicographic comparison gives the intended precedence: existing-series match
first, then canonical edition, then text score, then title-length closeness.
The strict-`>` comparison preserves first-wins-on-tie determinism. The 80% gate
and the `chosen is None -> low_confidence` path are unchanged.

### Why this resolves both cases

- **Small Gods** (existing series "Discworld"): the radio hit's series
  `Discworld on Radio` normalises to `discworld on radio` ≠ `discworld`, so its
  `series_match=0`; the canonical `Discworld` hit gets `series_match=1` and
  wins outright.
- **Mort** (existing series "Discworld"): both novel and box-set have series
  `Discworld` → `series_match=1` tie; the box-set title contains
  `collection set` and a ` / ` contents list → `is_canonical=0`, the novel is
  `is_canonical=1` and wins.
- **No existing series**: `series_match=0` for all; `is_canonical` breaks ties
  toward the novel — improving cover/description selection too.
- **Only an adaptation available**: all candidates share `is_canonical=0`,
  selection falls through to `total`/`length_penalty` — still matched (soft).

## Error handling

| Situation | Behaviour |
|---|---|
| `meta.series` is None/empty | `existing_series_norm=""`, `series_match=0` for all — selection driven by canonical + score |
| `candidate.series_name` is None | `normalise_series_name` returns "", never matches a non-empty existing series; `is_non_canonical` handles None as empty string |
| All candidates non-canonical | none excluded; best by score/length still chosen |
| No candidate clears 80% gate | unchanged: `low_confidence` |

## Testing

**`tests/test_matcher.py`** — new:
- `test_is_non_canonical_flags_radio` — series "Discworld on Radio" → True
- `test_is_non_canonical_flags_graphic_novel` — title "... A Discworld Graphic Novel" → True
- `test_is_non_canonical_flags_box_set_contents_list` — title "A / B / C …" → True
- `test_is_non_canonical_flags_collection_keyword` — "... 1 to 5 books collection set: …" → True
- `test_is_non_canonical_passes_plain_novel` — "Small Gods" / "Discworld" → False
- `test_normalise_series_name_strips_leading_the` — "The Culture" == norm("Culture")
- `test_normalise_series_name_handles_none` — None → ""

**`tests/test_enrich.py`** — new (mock `search_book` with multi-hit lists,
mirroring the live data; assert which candidate is chosen via the resulting
`series`/`series_index` on disk):
- `test_match_prefers_canonical_over_adaptation_with_existing_series` — Small
  Gods replay (radio #4, Discworld #13, graphic) + EPUB series "Discworld" →
  chosen is Discworld #13.
- `test_match_prefers_novel_over_boxset_on_series_tie` — Mort replay (novel
  Discworld #4, box-set Discworld #1) + EPUB series "Discworld" → chosen #4.
- `test_match_prefers_canonical_when_no_existing_series` — adaptation vs novel,
  bare EPUB → novel chosen.
- `test_match_falls_back_to_adaptation_when_only_option` — single adaptation
  hit clears the gate → still chosen (soft), status `enriched`.

All existing matcher/enrich tests must continue to pass (the new ranking terms
are additive and default to neutral when signals are absent).

## Deployment

Standard plexypi rsync of `ebook_enricher/` + rebuild. After deploy, re-run
`/backfill`; confirm Small Gods → `Discworld` #13 and Mort → `Discworld` #4 in
the logs, and that the `series_corrected` summary no longer includes those as
mis-corrections. Then merge the branch.

## Out of scope / future

- **Tuning the marker lists** as new adaptation/collection shapes appear —
  additive, low-risk.
- **A confidence margin** (e.g. require the winner to beat the runner-up by N)
  — not needed; the two ranking signals resolve the observed ambiguity.
- **Applying canonical preference to the cover editions-fallback** — that path
  already filters by aspect/format/language; revisit only if a bad cover edition
  is observed.
