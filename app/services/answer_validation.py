from __future__ import annotations

import json
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
import sqlite3

from flask import current_app

from .normalization import normalize_text, singularize_phrase
from .wiki_verification import (
    WikiEntityAmbiguous,
    WikiEntityNotFound,
    WikiVerificationError,
    cached_entity_for_text,
    evaluate_category_membership,
    manual_alias_lookups,
    new_wiki_request_budget,
    recently_failed_live_verification,
    remember_failed_live_verification,
    resolve_and_cache_entity_for_category,
    upsert_category_element,
)


@dataclass(frozen=True)
class MatchResult:
    status: str
    element_id: int | None = None
    canonical_name: str | None = None
    normalized_text: str = ""
    matched_from: str | None = None
    message: str = ""


# These categories have complete source-of-truth element lists in JSON. Exact
# JSON names and aliases are still allowed to reach live verification so they can
# populate the wiki cache, but anything outside the JSON list should fail locally.
CLOSED_LIST_CATEGORY_SLUGS = {"countries", "us-states"}
_CLOSED_LIST_TEXT_CACHE: dict[str, set[str]] = {}


def _element_row(db: sqlite3.Connection, element_id: int) -> sqlite3.Row | None:
    return db.execute(
        "SELECT * FROM category_elements WHERE id = ?",
        (element_id,),
    ).fetchone()


def _canonical_name_for_row(db: sqlite3.Connection, row: sqlite3.Row) -> str:
    """Return the best player-facing name for a category element.

    Wikidata labels for produce can be scientific names while Wikipedia page
    titles are often the common names players expect, such as "Cucumber".
    Existing cached rows are updated lazily when we can improve that display.
    """

    wiki_entity_id = row["wiki_entity_id"] if "wiki_entity_id" in row.keys() else None
    if wiki_entity_id is None:
        return row["canonical_name"]
    wiki_row = db.execute(
        "SELECT page_title FROM wiki_entities WHERE id = ?",
        (int(wiki_entity_id),),
    ).fetchone()
    if not wiki_row or not wiki_row["page_title"]:
        return row["canonical_name"]
    page_title = wiki_row["page_title"]
    if page_title != row["canonical_name"]:
        db.execute(
            "UPDATE category_elements SET canonical_name = ?, normalized_name = ? WHERE id = ?",
            (page_title, normalize_text(page_title), int(row["id"])),
        )
    return page_title


def _candidate_map(db: sqlite3.Connection, category_id: int) -> dict[str, set[int]]:
    """Build a local candidate map from live-verified category elements.

    Old development databases may still contain pre-live category rows. Those
    rows are allowed to act as local cache hits so a refreshed dev database is
    not required just to keep playing, while fresh databases still start empty
    and populate this table through live verification.
    """

    candidates: dict[str, set[int]] = {}
    rows = db.execute(
        """
        SELECT id, normalized_name
        FROM category_elements
        WHERE category_id = ? AND is_playable_answer = 1
        """,
        (category_id,),
    ).fetchall()
    for row in rows:
        candidates.setdefault(row["normalized_name"], set()).add(int(row["id"]))
    canonical_texts = set(candidates)

    alias_rows = db.execute(
        """
        SELECT ea.normalized_alias, ea.element_id
        FROM element_aliases ea
        JOIN category_elements ce ON ce.id = ea.element_id
        WHERE ce.category_id = ? AND ce.is_playable_answer = 1
        """,
        (category_id,),
    ).fetchall()
    for row in alias_rows:
        if row["normalized_alias"] in canonical_texts:
            continue
        candidates.setdefault(row["normalized_alias"], set()).add(int(row["element_id"]))
    return candidates


def _resolve_exact(db: sqlite3.Connection, normalized: str, candidates: dict[str, set[int]]) -> MatchResult | None:
    ids = candidates.get(normalized)
    if not ids:
        return None
    if len(ids) > 1:
        return MatchResult(status="ambiguous", normalized_text=normalized, message="Too ambiguous. Try being more specific.")
    element_id = next(iter(ids))
    row = _element_row(db, element_id)
    if not row:
        current_app.logger.error("Candidate map pointed at missing category element id %s.", element_id)
        return None
    canonical_name = _canonical_name_for_row(db, row)
    return MatchResult(
        status="matched",
        element_id=element_id,
        canonical_name=canonical_name,
        normalized_text=normalized,
    )


def _threshold_for(value: str) -> float:
    length = len(value.replace(" ", ""))
    if length <= 3:
        return 2.0  # disable fuzzy matching for tiny words
    if length <= 6:
        return 0.92
    return 0.88


def match_answer(db: sqlite3.Connection, category_id: int, submitted_text: str) -> MatchResult:
    normalized = normalize_text(submitted_text)
    if not normalized:
        return MatchResult(status="invalid", normalized_text="", message="Type an answer first.")

    category = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not category:
        return MatchResult(status="invalid", normalized_text=normalized, message="Category not found.")

    candidates = _candidate_map(db, category_id)

    exact = _resolve_exact(db, normalized, candidates)
    if exact:
        return exact

    singular = singularize_phrase(normalized)
    if singular != normalized:
        exact = _resolve_exact(db, singular, candidates)
        if exact:
            return MatchResult(
                status="matched",
                element_id=exact.element_id,
                canonical_name=exact.canonical_name,
                normalized_text=normalized,
                matched_from=normalized,
            )

    closed_list_invalid_message = _closed_list_invalid_message(category["slug"], normalized)
    if closed_list_invalid_message:
        return MatchResult(
            status="invalid",
            normalized_text=normalized,
            message=closed_list_invalid_message,
        )

    threshold = _threshold_for(normalized)
    best: list[tuple[float, str, int]] = []
    for candidate_text, ids in candidates.items():
        ratio = SequenceMatcher(None, normalized, candidate_text).ratio()
        if ratio >= threshold:
            for element_id in ids:
                best.append((ratio, candidate_text, element_id))

    if not best:
        live = _match_live_wiki_answer(db, category, submitted_text, normalized)
        if live:
            return live
        return MatchResult(status="invalid", normalized_text=normalized, message="That is not a valid answer for this category.")

    best.sort(reverse=True, key=lambda item: item[0])
    top_ratio = best[0][0]
    top_close = [item for item in best if top_ratio - item[0] <= 0.03]
    top_element_ids = {item[2] for item in top_close}

    if len(top_element_ids) > 1:
        return MatchResult(status="ambiguous", normalized_text=normalized, message="Too ambiguous. Try being more specific.")

    element_id = best[0][2]
    row = _element_row(db, element_id)
    if not row:
        current_app.logger.error("Fuzzy match pointed at missing category element id %s.", element_id)
        return MatchResult(status="invalid", normalized_text=normalized, message="That is not a valid answer for this category.")

    canonical_name = _canonical_name_for_row(db, row)
    return MatchResult(
        status="matched",
        element_id=element_id,
        canonical_name=canonical_name,
        normalized_text=normalized,
        matched_from=normalized,
        message=f"Accepted as {canonical_name}.",
    )


def _closed_list_invalid_message(category_slug: str, normalized: str) -> str | None:
    """Return an immediate invalid result for closed-list misses.

    Some categories, such as U.S. states and countries, have a complete
    canonical list in the category JSON. For those categories, an answer that is
    not an exact JSON name or alias should stay local instead of spending
    network time on Wikipedia search, where typo correction can resolve to an
    unrelated page. Near misses get a spelling-specific message; unrelated
    misses get the normal invalid-category message.
    """

    if category_slug not in CLOSED_LIST_CATEGORY_SLUGS:
        return None
    reference_texts = _closed_list_reference_texts(category_slug)
    if not reference_texts:
        current_app.logger.error("Closed-list category %r did not provide reference texts.", category_slug)
        return None
    # This may still need live verification on a fresh database, so exact JSON
    # matches are allowed through rather than accepted outright.
    if normalized in reference_texts:
        return None
    for reference in reference_texts:
        if _looks_like_closed_list_typo(normalized, reference):
            return "Check the spelling and try again."
    return "That is not a valid answer for this category."


def _looks_like_closed_list_typo(normalized: str, reference: str) -> bool:
    """Return whether ``normalized`` is probably a typo of ``reference``.

    The normal fuzzy threshold is tuned for accepting playable answers, which is
    intentionally strict. Closed-list rejection has a different job: keep likely
    typos local and explain them as spelling issues. A tiny bounded edit-distance
    check catches common one- or two-character mistakes like "new hampsher"
    without allowing unrelated answers to look like misspellings.
    """

    threshold = _threshold_for(reference)
    if SequenceMatcher(None, normalized, reference).ratio() >= threshold:
        return True
    max_distance = _closed_list_typo_distance(reference)
    if max_distance <= 0 or abs(len(normalized) - len(reference)) > max_distance:
        return False
    return _bounded_edit_distance(normalized, reference, max_distance) <= max_distance


def _closed_list_typo_distance(reference: str) -> int:
    """Choose a conservative edit-distance allowance for closed-list names."""

    compact_length = len(reference.replace(" ", ""))
    if compact_length <= 4:
        return 0
    if compact_length <= 8:
        return 1
    if compact_length <= 14:
        return 2
    return 3


def _bounded_edit_distance(left: str, right: str, max_distance: int) -> int:
    """Compute edit distance, stopping once the answer cannot be close enough.

    Closed-list typo checks run over small category lists, but this bound keeps
    the helper cheap and makes the intended behavior explicit: we only care
    whether two strings are within a very small number of edits.
    """

    previous = list(range(len(right) + 1))
    for left_index, left_char in enumerate(left, start=1):
        current = [left_index]
        row_min = current[0]
        for right_index, right_char in enumerate(right, start=1):
            substitution_cost = 0 if left_char == right_char else 1
            current.append(
                min(
                    previous[right_index] + 1,
                    current[right_index - 1] + 1,
                    previous[right_index - 1] + substitution_cost,
                )
            )
            row_min = min(row_min, current[-1])
        if row_min > max_distance:
            return max_distance + 1
        previous = current
    return previous[-1]


def _closed_list_reference_texts(category_slug: str) -> set[str]:
    """Return normalized canonical names and aliases for a closed-list category."""

    cached = _CLOSED_LIST_TEXT_CACHE.get(category_slug)
    if cached is not None:
        return cached
    path = Path(current_app.root_path) / "data" / "categories" / f"{category_slug}.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        current_app.logger.error("Could not load closed-list category %r from %s: %s", category_slug, path, exc)
        _CLOSED_LIST_TEXT_CACHE[category_slug] = set()
        return set()
    reference_texts: set[str] = set()
    for element in data.get("elements", []):
        name = normalize_text(element.get("name", ""))
        if name:
            reference_texts.add(name)
        for alias in element.get("aliases", []):
            normalized_alias = normalize_text(alias)
            if normalized_alias:
                reference_texts.add(normalized_alias)
    _CLOSED_LIST_TEXT_CACHE[category_slug] = reference_texts
    return reference_texts


def _match_live_wiki_answer(
    db: sqlite3.Connection,
    category: sqlite3.Row,
    submitted_text: str,
    normalized: str,
) -> MatchResult | None:
    """Verify an uncached answer through the live wiki cache path.

    The function returns ``None`` only when verification should fall through to
    local fuzzy matching against already verified answers. Hard failures return
    invalid/ambiguous results so gameplay can record the attempt normally.
    """

    lookup_text = submitted_text
    lookup_normalized = normalized
    manual_lookups = manual_alias_lookups(db, int(category["id"]), normalized)
    if len(set(manual_lookups)) > 1:
        return MatchResult(status="ambiguous", normalized_text=normalized, message="Too ambiguous. Try being more specific.")
    if manual_lookups:
        lookup_text = manual_lookups[0]
        lookup_normalized = normalize_text(lookup_text)

    category_id = int(category["id"])
    budget = new_wiki_request_budget()
    try:
        entity = cached_entity_for_text(db, lookup_normalized)
        if recently_failed_live_verification(db, category_id, lookup_normalized):
            return MatchResult(
                status="invalid",
                normalized_text=normalized,
                message="Could not verify that answer right now.",
            )
        if not entity:
            entity = resolve_and_cache_entity_for_category(db, lookup_text, category, budget=budget)
        membership = evaluate_category_membership(db, category, entity, budget=budget)
    except WikiEntityAmbiguous as exc:
        return MatchResult(status="ambiguous", normalized_text=normalized, message=str(exc))
    except WikiEntityNotFound:
        remember_failed_live_verification(db, category_id, lookup_normalized)
        return MatchResult(status="invalid", normalized_text=normalized, message="That is not a valid answer for this category.")
    except WikiVerificationError as exc:
        remember_failed_live_verification(db, category_id, lookup_normalized)
        return MatchResult(status="invalid", normalized_text=normalized, message=str(exc))

    if not membership.is_member:
        return MatchResult(status="invalid", normalized_text=normalized, message="That is not a valid answer for this category.")

    element_id = upsert_category_element(db, category, entity, budget=budget)
    element = _element_row(db, element_id)
    canonical_name = element["canonical_name"] if element else entity.canonical_name
    return MatchResult(
        status="matched",
        element_id=element_id,
        canonical_name=canonical_name,
        normalized_text=normalized,
        matched_from=lookup_text if normalize_text(lookup_text) != normalized else None,
        message=f"Accepted as {canonical_name}.",
    )
