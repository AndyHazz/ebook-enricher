# Canonical-Edition Match Preference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the enricher's match-selection prefer the canonical novel edition over adaptations (radio/graphic) and box-sets/omnibuses, fixing wrong series indices AND wrong covers (e.g. Small Gods, Mort, Colour of Magic all matched the Discworld box-set).

**Architecture:** Add a keyword detector + series-name normaliser to `matcher.py`; extend the candidate-ranking key in `enrich.py` from `(score, length)` to `(series_matches_existing, is_canonical, score, length)`. Pure re-ranking on data already fetched — no new API calls, no gate change, never excludes a candidate.

**Tech Stack:** Python 3.12, rapidfuzz, pytest + respx + unittest.mock. Spec: `docs/superpowers/specs/2026-06-02-canonical-edition-preference-design.md`. Branch: `feature/series-normalisation` (this builds on the just-merged-pending series work).

**Test env:** `pip install --break-system-packages --user respx pytest-asyncio rapidfuzz fastapi httpx` (once).

---

### Task 1: `matcher.py` — `normalise_series_name` + `is_non_canonical`

**Files:**
- Modify: `ebook_enricher/matcher.py` (append two functions + two marker tuples)
- Test: `tests/test_matcher.py` (append)

Context: `matcher.py` currently exposes `score_match`, `is_confident_match`, and constants `TITLE_THRESHOLD`/`AUTHOR_THRESHOLD` (both 80). It imports `from rapidfuzz import fuzz`. The two new functions are pure (no I/O, no fuzz needed) and operate on strings already present on `HardcoverBook` (`title`, `series_name`).

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_matcher.py` (it currently imports from `ebook_enricher.matcher`; add the two new names to that import or import inline):

```python
from ebook_enricher.matcher import is_non_canonical, normalise_series_name


def test_normalise_series_name_strips_leading_the():
    assert normalise_series_name("The Culture") == normalise_series_name("Culture")
    assert normalise_series_name("The Culture") == "culture"


def test_normalise_series_name_handles_none_and_blank():
    assert normalise_series_name(None) == ""
    assert normalise_series_name("   ") == ""


def test_is_non_canonical_flags_radio():
    # adaptation marker in the SERIES name (title is clean)
    assert is_non_canonical("Small Gods", "Terry Pratchett's Discworld on Radio") is True


def test_is_non_canonical_flags_graphic_novel():
    assert is_non_canonical("Small Gods: A Discworld Graphic Novel", "Discworld Graphic Novels") is True


def test_is_non_canonical_flags_collection_keyword():
    title = ("Terry pratchett discworld novel series 1 to 5 books collection set: "
             "The Colour of Magic / The Light Fantastic / Equal Rites / Mort / Sourcery")
    assert is_non_canonical(title, "Discworld") is True


def test_is_non_canonical_flags_contents_list_without_keyword():
    # >= 2 ' / ' separators (a box-set contents list) even without a keyword
    assert is_non_canonical("Book A / Book B / Book C", "Discworld") is True


def test_is_non_canonical_passes_plain_novel():
    assert is_non_canonical("Small Gods", "Discworld") is False
    assert is_non_canonical("Mort: A Novel of Discworld", "Discworld") is False


def test_is_non_canonical_handles_none_inputs():
    assert is_non_canonical(None, None) is False
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_matcher.py -k "non_canonical or normalise_series" -v`
Expected: FAIL — `ImportError: cannot import name 'is_non_canonical'`.

- [ ] **Step 3: Implement the two functions**

Append to `ebook_enricher/matcher.py`:

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
    if hay_title.count(" / ") >= 2:
        return True
    return False
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_matcher.py -k "non_canonical or normalise_series" -v`
Expected: PASS (8 tests).

- [ ] **Step 5: Run the full matcher suite for regressions**

Run: `python3 -m pytest tests/test_matcher.py -v`
Expected: PASS — existing matcher tests unaffected (pure additions).

- [ ] **Step 6: Commit**

```bash
git add ebook_enricher/matcher.py tests/test_matcher.py
GIT_COMMITTER_EMAIL="517731+AndyHazz@users.noreply.github.com" git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat(matcher): is_non_canonical detector + normalise_series_name

Keyword heuristics (title + series name) flag adaptations (radio/graphic/
audio) and collections (box set/omnibus/contents-lists). normalise_series_name
strips leading 'the' so 'The Culture' == 'Culture'. Pure, no API calls.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `enrich.py` — extend the candidate-ranking key

**Files:**
- Modify: `ebook_enricher/enrich.py` (imports near line 32; the candidate loop ~lines 103-116)
- Test: `tests/test_enrich.py` (append)

Context: `enrich.py` selects the best candidate with a ranking loop. Current form:

```python
    chosen: Optional[HardcoverBook] = None
    best_key: tuple[int, int] = (-1, -(1 << 30))
    for candidate in candidates:
        t_score, a_score = score_match(
            meta.title, meta.author, candidate.title, candidate.author
        )
        if t_score < TITLE_THRESHOLD or a_score < AUTHOR_THRESHOLD:
            continue
        total = t_score + a_score
        length_penalty = -abs(len(meta.title) - len(candidate.title))
        key = (total, length_penalty)
        if key > best_key:
            chosen = candidate
            best_key = key

    if chosen is None:
        return EnrichResult(status="low_confidence")
```

`meta` is the EPUB's `EpubMeta` (has `.series`, `.title`, `.author`). Each `candidate` is a `HardcoverBook` with `.title`, `.author`, `.series_name`. The matcher functions are imported at the top of `enrich.py` via `from ebook_enricher.matcher import (AUTHOR_THRESHOLD, TITLE_THRESHOLD, score_match)`.

The tests use `_make_hc_book(**overrides)` (defaults title "Test Book Title", author "Test Author", series_name "Test Series", series_position "1.5") and patch `ebook_enricher.enrich.search_book`. To exercise ranking, return MULTIPLE candidates whose title/author both clear the 80% gate against the fixture's title/author, differing in series_name / position / canonical-ness.

IMPORTANT for these tests: every candidate must clear the 80% gate against the EPUB's title+author. The `bare_epub`/`enriched_epub` fixtures have title "Test Book Title", author "Test Author". So give every candidate `title="Test Book Title"`, `author="Test Author"` and vary only `series_name`/`series_position` and (for canonical detection) append a marker to the candidate's *series_name* or *title*. Because all candidates share the same title here, the `length_penalty` is equal and the NEW terms decide — which is exactly what we want to test.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_enrich.py`:

```python
@pytest.mark.asyncio
async def test_match_prefers_canonical_over_adaptation_with_existing_series(enriched_epub: Path):
    """Existing series 'Existing Series'. Among same-title hits, the one whose
    series matches the existing tag and is canonical wins over an adaptation."""
    # enriched_epub already has series "Existing Series", index "2".
    adaptation = _make_hc_book(series_name="Existing Series on Radio", series_position="4")
    canonical = _make_hc_book(series_name="Existing Series", series_position="13")
    graphic = _make_hc_book(title="Test Book Title: A Graphic Novel",
                            series_name="Existing Series Graphic Novels", series_position="4")
    with patch("ebook_enricher.enrich.search_book",
               new=AsyncMock(return_value=[adaptation, canonical, graphic])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    meta = read_meta(enriched_epub)
    assert meta.series == "Existing Series"
    assert meta.series_index == "13"     # the canonical novel's position, not 4


@pytest.mark.asyncio
async def test_match_prefers_novel_over_boxset_on_series_tie(enriched_epub: Path):
    """Existing series 'Existing Series'. Two hits share that series name
    (series_match ties); the box-set is non-canonical so the novel wins."""
    novel = _make_hc_book(series_name="Existing Series", series_position="4")
    # The box-set title must contain the EPUB title ("Test Book Title") so it
    # still clears the 80% gate via partial_ratio (realistic: the box set lists
    # the book among its contents). It has "collection set" + >= 2 " / " so
    # is_non_canonical flags it; its series matches, so series_match ties with
    # the novel and is_canonical is what breaks the tie.
    boxset = _make_hc_book(
        title="Existing Series 1 to 5 books collection set: "
              "Test Book Title / Second / Third / Fourth / Fifth",
        series_name="Existing Series", series_position="1")
    with patch("ebook_enricher.enrich.search_book",
               new=AsyncMock(return_value=[boxset, novel])):
        result = await enrich_file(enriched_epub, token="fake", correct_series=True)
    assert result.status == "enriched"
    meta = read_meta(enriched_epub)
    assert meta.series_index == "4"      # the novel's position, not the box-set's 1


@pytest.mark.asyncio
async def test_match_prefers_canonical_when_no_existing_series(bare_epub: Path):
    """No existing series -> series_match is 0 for all; is_canonical breaks the
    tie toward the novel over the adaptation."""
    adaptation = _make_hc_book(series_name="Some Series on Radio", series_position="4")
    novel = _make_hc_book(series_name="Some Series", series_position="9")
    with patch("ebook_enricher.enrich.search_book",
               new=AsyncMock(return_value=[adaptation, novel])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"
    meta = read_meta(bare_epub)
    assert meta.series == "Some Series"
    assert meta.series_index == "9"


@pytest.mark.asyncio
async def test_match_falls_back_to_adaptation_when_only_option(bare_epub: Path):
    """Soft, not hard: if the ONLY confident hit is an adaptation, it is still
    chosen (no exclusion)."""
    only = _make_hc_book(series_name="Some Series on Radio", series_position="4")
    with patch("ebook_enricher.enrich.search_book", new=AsyncMock(return_value=[only])):
        result = await enrich_file(bare_epub, token="fake")
    assert result.status == "enriched"
    meta = read_meta(bare_epub)
    assert meta.series == "Some Series on Radio"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python3 -m pytest tests/test_enrich.py -k "prefers_canonical or prefers_novel or falls_back_to_adaptation" -v`
Expected: FAIL — without the ranking change, the adaptation/box-set (first in list) wins, so the index assertions fail (e.g. `assert '4' == '13'`). If a box-set test instead errors because its long title fails the 80% gate, that is a test-data problem — adjust the box-set title to keep "Test Book Title" as a clear prefix/token (e.g. prepend it) so `partial_ratio`/`token_set_ratio` ≥ 80; the goal is for ALL candidates to clear the gate so the new ranking terms are what decide.

- [ ] **Step 3: Add the matcher imports**

In `ebook_enricher/enrich.py`, extend the matcher import:

```python
from ebook_enricher.matcher import (
    AUTHOR_THRESHOLD,
    TITLE_THRESHOLD,
    score_match,
    is_non_canonical,
    normalise_series_name,
)
```

- [ ] **Step 4: Extend the ranking loop**

Replace the loop shown in the Context above with:

```python
    existing_series_norm = normalise_series_name(meta.series)  # "" if no series

    chosen: Optional[HardcoverBook] = None
    # (series_match, is_canonical, total, length_penalty)
    best_key: tuple[int, int, int, int] = (-1, -1, -1, -(1 << 30))
    for candidate in candidates:
        t_score, a_score = score_match(
            meta.title, meta.author, candidate.title, candidate.author
        )
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

    if chosen is None:
        return EnrichResult(status="low_confidence")
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `python3 -m pytest tests/test_enrich.py -k "prefers_canonical or prefers_novel or falls_back_to_adaptation" -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the full enrich + matcher suites for regressions**

Run: `python3 -m pytest tests/test_enrich.py tests/test_matcher.py -v`
Expected: PASS — all prior tests still green. (The new terms are neutral when no series exists and no markers are present, so existing single-candidate and `test_second_match_wins_if_first_is_low_confidence` behaviour is preserved.)

- [ ] **Step 7: Commit**

```bash
git add ebook_enricher/enrich.py tests/test_enrich.py
GIT_COMMITTER_EMAIL="517731+AndyHazz@users.noreply.github.com" git -c user.email="517731+AndyHazz@users.noreply.github.com" commit -m "feat(enrich): prefer canonical edition in candidate ranking

Ranking key becomes (series_matches_existing, is_canonical, score, length):
prefer a hit matching the EPUB's existing series, then a canonical edition
(non-adaptation, non-collection), then text score. Fixes Small Gods/Mort/
Colour of Magic matching the Discworld box-set (wrong index AND wrong cover).
Soft: 80% gate unchanged, nothing excluded.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Deploy, re-run backfill, verify, merge

**Files:** none (deploy + operational verification + branch finish)

Context: container at `/opt/stacks/ebook-enricher/` on plexypi; reach it via its `plexypi_default` IP (no `curl` inside the container). Books live under `/mnt/data/media/ebooks`. The earlier backfill mis-tagged Small Gods, Mort, Colour of Magic (box-set match → wrong index + squashed box-set cover). Re-running backfill after this fix should re-match them to the canonical novel, rewriting correct index AND correct cover. Note the copy-once pipeline ledger does NOT gate `/backfill` (backfill always walks every epub), so a re-run reprocesses everything.

- [ ] **Step 1: Full local suite**

Run: `python3 -m pytest tests/ -q`
Expected: PASS (all, including the new matcher + enrich tests).

- [ ] **Step 2: Deploy changed modules + tests**

```bash
cd /home/andyhazz/projects/ebook-enricher
scp ebook_enricher/matcher.py ebook_enricher/enrich.py plexypi:/opt/stacks/ebook-enricher/ebook_enricher/
scp tests/test_matcher.py tests/test_enrich.py plexypi:/opt/stacks/ebook-enricher/tests/
```

- [ ] **Step 3: Rebuild + restart**

```bash
ssh plexypi 'cd /opt/stacks/ebook-enricher && docker compose build ebook-enricher && docker compose up -d ebook-enricher'
```
Expected: `Container ebook-enricher Started`.

- [ ] **Step 4: Health check**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); curl -s "http://$IP:8000/health"'
```
Expected: `{"status":"ok"}`.

- [ ] **Step 5: Spot-check the three known-bad books via single-file `/enrich`**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); \
for f in "13. Small Gods - Terry Pratchett.epub" "04. Mort - Terry Pratchett.epub" "01. The Colour of Magic - Terry Pratchett.epub"; do
  echo "== $f =="; \
  curl -s -X POST -H "Content-Type: application/json" -d "{\"path\": \"/data/media/ebooks/$f\"}" "http://$IP:8000/enrich"; echo; \
done'
```
Then verify on disk:
```bash
ssh plexypi 'for f in "13. Small Gods - Terry Pratchett.epub" "04. Mort - Terry Pratchett.epub" "01. The Colour of Magic - Terry Pratchett.epub"; do
  echo "== $f =="; unzip -p "/mnt/data/media/ebooks/$f" "*.opf" 2>/dev/null | grep -oE "calibre:series[^/]*content=\"[^\"]*\""; done'
```
Expected: Small Gods → `Discworld` index `13`; Mort → `Discworld` index `4`; Colour of Magic → `Discworld` index `1`. (None should be `Discworld on Radio` or a box-set position.)

- [ ] **Step 6: Re-run the library-wide backfill**

```bash
ssh plexypi 'IP=$(docker inspect ebook-enricher --format "{{(index .NetworkSettings.Networks \"plexypi_default\").IPAddress}}"); curl -s -X POST --max-time 1800 "http://$IP:8000/backfill"'
```
(Run backgrounded; ~5-10 min.) Expected: a `BackfillSummary`. Then scan the log for any remaining adaptation/box-set name changes:
```bash
ssh plexypi 'docker logs ebook-enricher --since 20m 2>&1 | grep "series corrected" | grep -iE "on radio|graphic|collection|box set"'
```
Expected: empty (no canonical book pushed into an adaptation/collection series).

- [ ] **Step 7: Push the branch**

```bash
git push origin feature/series-normalisation
```

- [ ] **Step 8: Finish the branch**

Use superpowers:finishing-a-development-branch to merge `feature/series-normalisation` into `main` (it carries the series-normalisation feature + this canonical-preference fix) and push.

---

## Notes for the executor

- The covers fix for free: choosing the canonical novel over the box-set means the cover-replacement step pulls the *novel's* cover, so the "squashed box-set cover" on Colour of Magic / Mort is corrected by the same re-backfill — no separate cover work.
- If a box-set test fails the 80% gate (long title), prepend the EPUB title so it stays a clear token/prefix — the intent is for all candidates to clear the gate so the NEW ranking terms decide.
- Keep `matcher.py` pure (no I/O); the ranking *policy* stays in `enrich.py`.
