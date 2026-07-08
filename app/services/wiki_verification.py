from __future__ import annotations

import gzip
import json
import socket
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass
from typing import Any

from flask import current_app

from ..db import utc_now_iso
from .normalization import normalize_text


# These are the structured Wikidata properties that are most useful for
# category-style game checks. The cache still stores every claim whose value is
# another Wikidata entity, but these constants document the relationships that
# the current rules actively walk.
INSTANCE_OF = "P31"
SUBCLASS_OF = "P279"
PARENT_TAXON = "P171"
PART_OF = "P361"
USE = "P366"
SEX_OR_GENDER = "P21"
OCCUPATION = "P106"
POSITION_HELD = "P39"
AWARD_RECEIVED = "P166"


CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "animals": {
        "targets": {"Q729", "Q7377", "Q5113", "Q10811", "Q152", "Q10908", "Q1390", "Q1358", "Q25326", "Q25364"},
        "properties": {PARENT_TAXON},
        "max_depth": 24,
        "mode": "taxonomy",
        "reason": "taxon ancestry reaches Animalia or a configured animal branch",
    },
    "countries": {
        "targets": {"Q3624078", "Q6256"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches country or sovereign state",
    },
    "fruits": {
        "targets": {"Q3314483", "Q1364", "Q1470762"},
        "properties": {INSTANCE_OF, SUBCLASS_OF, PART_OF, USE},
        "mode": "flat",
        "reason": "structured claims connect to culinary or botanical fruit",
    },
    "us-states": {
        "targets": {"Q35657"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches U.S. state",
    },
    "women": {
        "targets": {"Q6581072"},
        "properties": {SEX_OR_GENDER},
        "max_depth": 2,
        "mode": "flat",
        "reason": "sex or gender claim reaches female",
    },
    "men": {
        "targets": {"Q6581097"},
        "properties": {SEX_OR_GENDER},
        "max_depth": 2,
        "mode": "flat",
        "reason": "sex or gender claim reaches male",
    },
    "foods": {
        "targets": {"Q2095", "Q746549", "Q25403900"},
        "properties": {INSTANCE_OF, SUBCLASS_OF, PART_OF, USE},
        "mode": "flat",
        "reason": "structured claims connect to food, dish, or food ingredient",
    },
    "cities": {
        "targets": {"Q515", "Q1093829", "Q1549591", "Q62049", "Q2154459", "Q15127012"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches city or compatible U.S. municipality type",
    },
    "movies": {
        "targets": {"Q11424"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches film",
    },
    "tv-shows": {
        "targets": {"Q5398426", "Q15416"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches television series or television program",
    },
    "video-games": {
        "targets": {"Q7889"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches video game",
    },
    "musicians": {
        "targets": {"Q639669"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches musician",
    },
    "dog-breeds": {
        "targets": {"Q39367"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches dog breed",
    },
    "pokemon": {
        "targets": {"Q3966183"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"pokemon species"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a Pokemon species",
    },
    "harry-potter-characters": {
        "category_patterns": {"harry potter characters"},
        "mode": "flat",
        "reason": "page categories identify a Harry Potter character",
    },
    "lord-of-the-rings-characters": {
        "category_patterns": {"lord of the rings characters", "the lord of the rings characters", "middle earth characters"},
        "mode": "flat",
        "reason": "page categories identify a Lord of the Rings character",
    },
    "gods-deities": {
        "targets": {"Q178885", "Q22989102"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"deities", "gods"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a god or deity",
    },
    "biblical-figures": {
        "category_patterns": {"biblical people", "biblical figures", "people in the hebrew bible", "people in the new testament"},
        "mode": "flat",
        "reason": "page categories identify a Biblical figure",
    },
    "currencies": {
        "targets": {"Q8142"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches currency",
    },
    "languages": {
        "targets": {"Q34770", "Q20162172", "Q33742"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches language or human language",
    },
    "rivers": {
        "targets": {"Q4022"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches river",
    },
    "mountains": {
        "targets": {"Q8502"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches mountain",
    },
    "islands": {
        "targets": {"Q23442"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches island",
    },
    "us-presidents": {
        "targets": {"Q11696"},
        "properties": {POSITION_HELD},
        "max_depth": 2,
        "mode": "flat",
        "reason": "position-held claim reaches president of the United States",
    },
    "world-leaders": {
        "targets": {"Q48352", "Q2285706"},
        "properties": {POSITION_HELD, INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {
            "heads of government",
            "heads of state",
            "monarchs",
            "presidents of",
            "prime ministers of",
        },
        "mode": "flat",
        "reason": "position-held claims or page categories identify a national leader",
    },
    "philosophers": {
        "targets": {"Q4964182"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches philosopher",
    },
    "painters": {
        "targets": {"Q1028181"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches painter",
    },
    "authors": {
        "targets": {"Q482980"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches author",
    },
    "poets": {
        "targets": {"Q49757"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches poet",
    },
    "anime": {
        "targets": {"Q1107"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"anime"},
        "mode": "flat",
        "reason": "structured claims or page categories identify anime",
    },
    "actors": {
        "targets": {"Q33999"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches actor",
    },
    "albums": {
        "targets": {"Q482994"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"albums"},
        "mode": "flat",
        "reason": "structured claims or page categories identify an album",
    },
    "athletes": {
        "targets": {"Q2066131"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"sportspeople", "athletes", "players"},
        "mode": "flat",
        "reason": "occupation path or page categories identify an athlete",
    },
    "chemical-elements": {
        "targets": {"Q11344"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches chemical element",
    },
    "dinosaurs": {
        "targets": {"Q430"},
        "properties": {PARENT_TAXON},
        "max_depth": 24,
        "mode": "taxonomy",
        "reason": "taxon ancestry reaches Dinosauria",
    },
    "classical-composers": {
        "targets": {"Q36834"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"classical composers"},
        "mode": "flat",
        "reason": "occupation path or page categories identify a classical composer",
    },
    "novels": {
        "targets": {"Q8261"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"novels"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a novel",
    },
    "nobel-prize-winners": {
        "targets": {"Q7191"},
        "properties": {AWARD_RECEIVED},
        "max_depth": 2,
        "category_patterns": {"nobel laureates", "nobel prize winners"},
        "mode": "flat",
        "reason": "award-received claim or page categories identify a Nobel Prize winner",
    },
    "olympic-sports": {
        "targets": {"Q212434"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"olympic sports", "sports at the olympics"},
        "mode": "flat",
        "reason": "structured claims or page categories identify an Olympic sport",
    },
    "paintings": {
        "targets": {"Q3305213"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"paintings"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a painting",
    },
    "scientists": {
        "targets": {"Q901"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"scientists"},
        "mode": "flat",
        "reason": "occupation path or page categories identify a scientist",
    },
    "sports-franchises": {
        "targets": {"Q847017"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"sports clubs", "sports teams", "sports franchises"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a sports franchise",
    },
    "fonts": {
        "targets": {"Q17451"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"typefaces", "fonts"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a typeface or font",
    },
    "religions": {
        "targets": {"Q9174"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches religion",
    },
    "medical-specialties": {
        "targets": {"Q930752"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"medical specialties", "medical specialities"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a medical specialty",
    },
    "units-of-measurement": {
        "targets": {"Q47574"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"units of measurement"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a unit of measurement",
    },
}


_WIKIDATA_ACTION_API_THROTTLED = False


class WikiVerificationError(RuntimeError):
    """Raised when the remote wiki APIs fail before an answer can be verified."""


class WikiEntityNotFound(RuntimeError):
    """Raised when submitted text cannot be resolved to a usable wiki entity."""


class WikiEntityAmbiguous(RuntimeError):
    """Raised when the submitted text resolves to an ambiguous page or cache hit."""


@dataclass(frozen=True)
class CachedWikiEntity:
    id: int
    qid: str
    page_title: str
    canonical_name: str
    description: str | None


@dataclass(frozen=True)
class MembershipResult:
    is_member: bool
    reason: str


def cached_entity_for_text(db: sqlite3.Connection, normalized: str) -> CachedWikiEntity | None:
    """Find a previously cached wiki entity by any normalized title or alias.

    The alias cache is global because a Wikidata entity can be valid in many
    categories. If a normalized alias points at more than one entity, the answer
    is treated as ambiguous rather than silently choosing the first match.
    """

    rows = db.execute(
        """
        SELECT we.*
        FROM wiki_entity_aliases wea
        JOIN wiki_entities we ON we.id = wea.wiki_entity_id
        WHERE wea.normalized_alias = ?
        ORDER BY we.sitelinks DESC, we.canonical_name ASC
        """,
        (normalized,),
    ).fetchall()
    if not rows:
        return None
    qids = {row["qid"] for row in rows}
    if len(qids) > 1:
        raise WikiEntityAmbiguous("Too ambiguous. Try being more specific.")
    return _entity_from_row(rows[0])


def manual_alias_lookups(db: sqlite3.Connection, category_id: int, normalized: str) -> list[str]:
    """Return category-specific lookup replacements for terse player aliases.

    These are hints such as "USA" -> "United States" or "CA" -> "California".
    Multiple rows are intentionally allowed so inputs like "football" can remain
    ambiguous in categories where the local category data says they are.
    """

    rows = db.execute(
        """
        SELECT lookup_text
        FROM category_manual_aliases
        WHERE category_id = ? AND normalized_alias = ?
        ORDER BY lookup_text ASC
        """,
        (category_id, normalized),
    ).fetchall()
    return [row["lookup_text"] for row in rows]


def resolve_and_cache_entity(db: sqlite3.Connection, submitted_text: str) -> CachedWikiEntity:
    """Resolve submitted text through Wikipedia, then cache the linked Wikidata item."""

    title = _opensearch_title(submitted_text)
    page = _fetch_page_metadata(title)
    if page.get("disambiguation"):
        raise WikiEntityAmbiguous("Too ambiguous. Try being more specific.")

    qid = page.get("qid")
    if not qid:
        current_app.logger.error(
            "Wikipedia page %r resolved from %r did not include a linked Wikidata item.",
            page.get("title"),
            submitted_text,
        )
        raise WikiEntityNotFound("Could not verify that answer.")

    entity = _fetch_wikidata_entity(qid)
    entity_id = _upsert_wiki_entity(db, entity, page, submitted_text)
    _replace_wiki_entity_claims(db, entity_id, entity)
    _replace_wikipedia_categories(db, entity_id, page.get("categories", []))
    _insert_entity_aliases(db, entity_id, entity, page, submitted_text)
    return get_entity_by_id(db, entity_id)


def ensure_qid_cached(db: sqlite3.Connection, qid: str) -> CachedWikiEntity | None:
    """Make sure an ancestor/class QID exists in the local structured cache.

    Category checks walk from the submitted entity through its class, subclass,
    and taxonomy claims. When a claim points at an uncached QID, this function
    fetches just enough Wikidata data for that node to continue the walk.
    """

    row = db.execute("SELECT * FROM wiki_entities WHERE qid = ?", (qid,)).fetchone()
    if row:
        return _entity_from_row(row)

    try:
        entity = _fetch_wikidata_entity(qid)
    except WikiVerificationError:
        raise
    except Exception as exc:
        current_app.logger.error("Could not cache related Wikidata entity %s: %s", qid, exc)
        return None

    entity_id = _upsert_wiki_entity(db, entity, page=None, submitted_text=None)
    _replace_wiki_entity_claims(db, entity_id, entity)
    _insert_entity_aliases(db, entity_id, entity, page=None, submitted_text=None)
    return get_entity_by_id(db, entity_id)


def get_entity_by_id(db: sqlite3.Connection, entity_id: int) -> CachedWikiEntity:
    row = db.execute("SELECT * FROM wiki_entities WHERE id = ?", (entity_id,)).fetchone()
    if not row:
        current_app.logger.error("Expected wiki entity id %s to exist after cache upsert.", entity_id)
        raise WikiVerificationError("Could not verify that answer right now.")
    return _entity_from_row(row)


def evaluate_category_membership(
    db: sqlite3.Connection,
    category: sqlite3.Row,
    entity: CachedWikiEntity,
) -> MembershipResult:
    """Check and cache whether one wiki entity belongs to one game category."""

    cached = db.execute(
        """
        SELECT is_member, reason
        FROM category_entity_memberships
        WHERE category_id = ? AND wiki_entity_id = ?
        """,
        (int(category["id"]), entity.id),
    ).fetchone()
    if cached:
        if bool(cached["is_member"]):
            return MembershipResult(True, cached["reason"])

    slug = category["slug"]
    rule = CATEGORY_RULES.get(slug)
    properties = set(rule.get("properties", set())) if rule else set()
    if not rule:
        current_app.logger.error("Category %r has no live wiki membership rule.", slug)
        result = MembershipResult(False, "No live verification rule is configured for this category.")
    elif properties and _has_any_path(db, entity.qid, set(rule.get("excluded", set())), properties, max_depth=2):
        result = MembershipResult(False, "Excluded by category rule.")
    elif properties and _has_any_path(
        db,
        entity.qid,
        set(rule["targets"]),
        properties,
        max_depth=int(rule.get("max_depth", 10)),
    ):
        result = MembershipResult(True, rule["reason"])
    elif _has_any_matching_category(db, entity.id, set(rule.get("category_patterns", set()))):
        result = MembershipResult(True, rule["reason"])
    else:
        result = MembershipResult(False, "That is not a valid answer for this category.")

    db.execute(
        """
        INSERT INTO category_entity_memberships
            (category_id, wiki_entity_id, is_member, reason, checked_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category_id, wiki_entity_id) DO UPDATE SET
            is_member = excluded.is_member,
            reason = excluded.reason,
            checked_at = excluded.checked_at
        """,
        (int(category["id"]), entity.id, 1 if result.is_member else 0, result.reason, utc_now_iso()),
    )
    return result


def upsert_category_element(
    db: sqlite3.Connection,
    category: sqlite3.Row,
    entity: CachedWikiEntity,
) -> int:
    """Create or update the category-specific answer row for a verified entity."""

    parent_id = None
    if category["slug"] == "animals":
        parent_id = _ensure_taxonomy_parent_element(db, int(category["id"]), entity.qid)

    is_playable = 0 if entity.qid == "Q729" else 1
    now = utc_now_iso()
    display_name = entity.page_title or entity.canonical_name
    normalized_name = normalize_text(display_name)
    existing_by_name = db.execute(
        """
        SELECT id
        FROM category_elements
        WHERE category_id = ? AND normalized_name = ? AND wiki_entity_id IS NULL
        """,
        (int(category["id"]), normalized_name),
    ).fetchone()
    if existing_by_name:
        db.execute(
            """
            UPDATE category_elements
            SET wiki_entity_id = ?,
                wiki_qid = ?,
                element_key = ?,
                canonical_name = ?,
                parent_id = ?,
                is_playable_answer = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                entity.id,
                entity.qid,
                entity.qid,
                display_name,
                parent_id,
                is_playable,
                now,
                int(existing_by_name["id"]),
            ),
        )
        _copy_wiki_aliases_to_element_aliases(db, int(existing_by_name["id"]), entity.id)
        return int(existing_by_name["id"])

    db.execute(
        """
        INSERT INTO category_elements
            (category_id, wiki_entity_id, wiki_qid, element_key, canonical_name,
             normalized_name, parent_id, is_playable_answer, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category_id, wiki_entity_id) DO UPDATE SET
            wiki_qid = excluded.wiki_qid,
            canonical_name = excluded.canonical_name,
            normalized_name = excluded.normalized_name,
            parent_id = excluded.parent_id,
            is_playable_answer = excluded.is_playable_answer,
            updated_at = excluded.updated_at
        """,
        (
            int(category["id"]),
            entity.id,
            entity.qid,
            entity.qid,
            display_name,
            normalized_name,
            parent_id,
            is_playable,
            now,
            now,
        ),
    )
    row = db.execute(
        "SELECT id FROM category_elements WHERE category_id = ? AND wiki_entity_id = ?",
        (int(category["id"]), entity.id),
    ).fetchone()
    if not row:
        current_app.logger.error(
            "Expected category element for category %s and wiki entity %s after upsert.",
            int(category["id"]),
            entity.id,
        )
        raise WikiVerificationError("Could not verify that answer right now.")
    element_id = int(row["id"])
    _copy_wiki_aliases_to_element_aliases(db, element_id, entity.id)
    return element_id


def _ensure_taxonomy_parent_element(db: sqlite3.Connection, category_id: int, qid: str) -> int | None:
    """Ensure the immediate parent taxon exists as a category element.

    Animal replacement logic depends on ``category_elements.parent_id``. Wikidata
    stores taxonomy in ``P171`` claims, so this helper lazily mirrors that parent
    chain into the existing game table whenever an animal answer is accepted.
    """

    parent_qid = _first_claim_value(db, qid, PARENT_TAXON)
    if not parent_qid:
        return None
    if parent_qid == qid:
        current_app.logger.error("Broken taxonomy chain for %s: parent points to itself.", qid)
        return None

    parent = ensure_qid_cached(db, parent_qid)
    if not parent:
        current_app.logger.error("Could not cache parent taxon %s for child %s.", parent_qid, qid)
        return None

    normalized_name = normalize_text(parent.canonical_name)
    row = db.execute(
        "SELECT id FROM category_elements WHERE category_id = ? AND wiki_entity_id = ?",
        (category_id, parent.id),
    ).fetchone()
    if row:
        return int(row["id"])

    existing_by_name = db.execute(
        """
        SELECT id, wiki_entity_id
        FROM category_elements
        WHERE category_id = ? AND normalized_name = ?
        """,
        (category_id, normalized_name),
    ).fetchone()
    if existing_by_name:
        if existing_by_name["wiki_entity_id"] is not None:
            current_app.logger.error(
                "Taxonomy parent %s for %s matched an existing row with a different wiki entity.",
                parent.qid,
                qid,
            )
            return int(existing_by_name["id"])

        branch_targets = set(CATEGORY_RULES["animals"]["targets"])
        grandparent_id = None if parent.qid in branch_targets else _ensure_taxonomy_parent_element(db, category_id, parent.qid)
        db.execute(
            """
            UPDATE category_elements
            SET wiki_entity_id = ?,
                wiki_qid = ?,
                element_key = ?,
                canonical_name = ?,
                parent_id = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                parent.id,
                parent.qid,
                parent.qid,
                parent.canonical_name,
                grandparent_id,
                utc_now_iso(),
                int(existing_by_name["id"]),
            ),
        )
        _copy_wiki_aliases_to_element_aliases(db, int(existing_by_name["id"]), parent.id)
        return int(existing_by_name["id"])

    branch_targets = set(CATEGORY_RULES["animals"]["targets"])
    grandparent_id = None if parent.qid in branch_targets else _ensure_taxonomy_parent_element(db, category_id, parent.qid)
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO category_elements
            (category_id, wiki_entity_id, wiki_qid, element_key, canonical_name,
             normalized_name, parent_id, is_playable_answer, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(category_id, wiki_entity_id) DO UPDATE SET
            parent_id = excluded.parent_id,
            updated_at = excluded.updated_at
        """,
        (
            category_id,
            parent.id,
            parent.qid,
            parent.qid,
            parent.canonical_name,
            normalized_name,
            grandparent_id,
            0 if parent.qid == "Q729" else 1,
            now,
            now,
        ),
    )
    row = db.execute(
        "SELECT id FROM category_elements WHERE category_id = ? AND wiki_entity_id = ?",
        (category_id, parent.id),
    ).fetchone()
    if not row:
        current_app.logger.error("Failed to create taxonomy parent element %s.", parent.qid)
        return None
    _copy_wiki_aliases_to_element_aliases(db, int(row["id"]), parent.id)
    return int(row["id"])


def _has_any_path(
    db: sqlite3.Connection,
    start_qid: str,
    target_qids: set[str],
    properties: set[str],
    max_depth: int = 10,
    max_nodes: int = 40,
) -> bool:
    """Walk cached Wikidata QID relationships looking for a category target."""

    if not target_qids:
        return False
    queue: list[tuple[str, int]] = [(start_qid, 0)]
    seen: set[str] = set()
    while queue:
        if len(seen) >= max_nodes:
            current_app.logger.warning(
                "Stopped Wikidata path walk from %s after %s nodes without finding %s.",
                start_qid,
                max_nodes,
                sorted(target_qids),
            )
            return False
        qid, depth = queue.pop(0)
        if qid in target_qids:
            return True
        if qid in seen or depth >= max_depth:
            continue
        seen.add(qid)
        entity = ensure_qid_cached(db, qid)
        if not entity:
            continue
        value_qids = _claim_values(db, entity.id, properties)
        for value_qid in value_qids:
            if value_qid in target_qids:
                return True
            if value_qid not in seen:
                queue.append((value_qid, depth + 1))
    return False


def _has_any_matching_category(db: sqlite3.Connection, entity_id: int, category_patterns: set[str]) -> bool:
    """Return whether cached Wikipedia categories match configured text hints.

    Wikidata paths are preferred for categories with crisp structured claims,
    but some game categories, especially fictional universes and broad award
    groupings, are more consistently exposed through Wikipedia category titles.
    The patterns are normalized with the same helper used for cached category
    titles, then checked as substrings so labels such as "American Nobel
    laureates" can still satisfy the broader "Nobel laureates" rule.
    """

    if not category_patterns:
        return False
    normalized_patterns = {normalize_text(pattern) for pattern in category_patterns if normalize_text(pattern)}
    if not normalized_patterns:
        current_app.logger.error("Category rule supplied only empty Wikipedia category patterns.")
        return False
    rows = db.execute(
        """
        SELECT normalized_category
        FROM wiki_entity_categories
        WHERE wiki_entity_id = ?
        """,
        (entity_id,),
    ).fetchall()
    for row in rows:
        normalized_category = row["normalized_category"]
        if any(pattern in normalized_category for pattern in normalized_patterns):
            return True
    return False


def _first_claim_value(db: sqlite3.Connection, qid: str, property_id: str) -> str | None:
    entity = ensure_qid_cached(db, qid)
    if not entity:
        return None
    row = db.execute(
        """
        SELECT value_qid
        FROM wiki_entity_claims
        WHERE wiki_entity_id = ? AND property_id = ?
        ORDER BY CASE rank WHEN 'preferred' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, value_qid ASC
        LIMIT 1
        """,
        (entity.id, property_id),
    ).fetchone()
    return row["value_qid"] if row else None


def _claim_values(db: sqlite3.Connection, entity_id: int, properties: set[str]) -> list[str]:
    placeholders = ",".join("?" for _ in properties)
    rows = db.execute(
        f"""
        SELECT value_qid
        FROM wiki_entity_claims
        WHERE wiki_entity_id = ? AND property_id IN ({placeholders})
        ORDER BY CASE rank WHEN 'preferred' THEN 0 WHEN 'normal' THEN 1 ELSE 2 END, value_qid ASC
        """,
        (entity_id, *sorted(properties)),
    ).fetchall()
    return [row["value_qid"] for row in rows]


def _opensearch_title(search_text: str) -> str:
    try:
        data = _wiki_json(
            current_app.config["WIKIPEDIA_API_URL"],
            {
                "action": "opensearch",
                "search": search_text,
                "namespace": "0",
                "limit": "3",
                "redirects": "resolve",
                "format": "json",
            },
        )
    except WikiVerificationError:
        current_app.logger.error(
            "Wikipedia OpenSearch failed for %r; trying direct title lookup.",
            search_text,
        )
        return search_text
    titles = data[1] if isinstance(data, list) and len(data) > 1 else []
    if not titles:
        raise WikiEntityNotFound("That is not a valid answer for this category.")
    return titles[0]


def _fetch_page_metadata(title: str) -> dict[str, Any]:
    data = _wiki_json(
        current_app.config["WIKIPEDIA_API_URL"],
        {
            "action": "query",
            "prop": "info|pageprops|categories",
            "titles": title,
            "redirects": "1",
            "cllimit": "max",
            "format": "json",
            "formatversion": "2",
        },
    )
    pages = data.get("query", {}).get("pages", [])
    if not pages or pages[0].get("missing"):
        raise WikiEntityNotFound("That is not a valid answer for this category.")
    page = pages[0]
    pageprops = page.get("pageprops", {})
    return {
        "page_id": page.get("pageid"),
        "title": page.get("title", title),
        "qid": pageprops.get("wikibase_item"),
        "disambiguation": "disambiguation" in pageprops,
        "categories": [
            category.get("title", "")
            for category in page.get("categories", [])
            if category.get("title")
        ],
    }


def _fetch_wikidata_entity(qid: str) -> dict[str, Any]:
    global _WIKIDATA_ACTION_API_THROTTLED

    data = None
    if not _WIKIDATA_ACTION_API_THROTTLED:
        try:
            data = _wiki_json(
                current_app.config["WIKIDATA_API_URL"],
                {
                    "action": "wbgetentities",
                    "ids": qid,
                    "props": "labels|aliases|descriptions|claims|sitelinks",
                    "languages": "en",
                    "format": "json",
                },
            )
        except WikiVerificationError:
            _WIKIDATA_ACTION_API_THROTTLED = True
            current_app.logger.error(
                "Wikidata wbgetentities failed for %s; trying Special:EntityData fallback.",
                qid,
            )
    if data is None:
        data = _wiki_url_json(f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json")
    entity = data.get("entities", {}).get(qid)
    if not entity or "missing" in entity:
        raise WikiEntityNotFound("That is not a valid answer for this category.")
    return entity


def _wiki_json(url: str, params: dict[str, str], retries: int = 1) -> Any:
    """Call a Wikimedia JSON endpoint with compression, timeout, and retries."""

    full_url = f"{url}?{urllib.parse.urlencode(params)}"
    return _wiki_url_json(full_url, retries=retries)


def _wiki_url_json(full_url: str, retries: int = 1) -> Any:
    """Fetch one JSON URL while respecting short Wikimedia throttle signals."""

    request = urllib.request.Request(
        full_url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": current_app.config["WIKI_USER_AGENT"],
        },
    )
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                elif "deflate" in encoding:
                    raw = zlib.decompress(raw)
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            retry_after = exc.headers.get("Retry-After")
            if attempt < retries and exc.code == 429:
                time.sleep(min(float(retry_after or "1"), 3.0))
                continue
            if attempt >= retries:
                current_app.logger.error("Wiki API request failed for %s: %s", full_url, exc)
                raise WikiVerificationError("Could not verify that answer right now.") from exc
            time.sleep(0.5 + attempt)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt >= retries:
                current_app.logger.error("Wiki API request failed for %s: %s", full_url, exc)
                raise WikiVerificationError("Could not verify that answer right now.") from exc
            time.sleep(0.5 + attempt)
    raise WikiVerificationError("Could not verify that answer right now.")


def _upsert_wiki_entity(
    db: sqlite3.Connection,
    entity: dict[str, Any],
    page: dict[str, Any] | None,
    submitted_text: str | None,
) -> int:
    qid = entity["id"]
    labels = entity.get("labels", {})
    descriptions = entity.get("descriptions", {})
    sitelinks = entity.get("sitelinks", {})
    enwiki_title = sitelinks.get("enwiki", {}).get("title")
    canonical_name = labels.get("en", {}).get("value") or page and page.get("title") or submitted_text or qid
    page_title = page and page.get("title") or enwiki_title or canonical_name
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO wiki_entities
            (qid, page_id, page_title, canonical_name, description, sitelinks, fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qid) DO UPDATE SET
            page_id = COALESCE(excluded.page_id, wiki_entities.page_id),
            page_title = excluded.page_title,
            canonical_name = excluded.canonical_name,
            description = excluded.description,
            sitelinks = excluded.sitelinks,
            fetched_at = excluded.fetched_at,
            updated_at = excluded.updated_at
        """,
        (
            qid,
            page.get("page_id") if page else None,
            page_title,
            canonical_name,
            descriptions.get("en", {}).get("value"),
            len(sitelinks),
            now,
            now,
        ),
    )
    row = db.execute("SELECT id FROM wiki_entities WHERE qid = ?", (qid,)).fetchone()
    if not row:
        current_app.logger.error("Expected wiki entity %s to exist after upsert.", qid)
        raise WikiVerificationError("Could not verify that answer right now.")
    return int(row["id"])


def _replace_wiki_entity_claims(db: sqlite3.Connection, entity_id: int, entity: dict[str, Any]) -> None:
    """Cache all discrete Wikidata-entity-valued claims for future categories."""

    now = utc_now_iso()
    db.execute("DELETE FROM wiki_entity_claims WHERE wiki_entity_id = ?", (entity_id,))
    for property_id, claims in entity.get("claims", {}).items():
        for claim in claims:
            mainsnak = claim.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            value = datavalue.get("value")
            if not isinstance(value, dict) or value.get("entity-type") != "item":
                continue
            numeric_id = value.get("numeric-id")
            if numeric_id is None:
                continue
            value_qid = f"Q{numeric_id}"
            db.execute(
                """
                INSERT OR IGNORE INTO wiki_entity_claims
                    (wiki_entity_id, property_id, value_qid, value_label, rank, created_at)
                VALUES (?, ?, ?, NULL, ?, ?)
                """,
                (entity_id, property_id, value_qid, claim.get("rank"), now),
            )


def _replace_wikipedia_categories(db: sqlite3.Connection, entity_id: int, categories: list[str]) -> None:
    """Cache page category titles as discrete labels, without page prose."""

    now = utc_now_iso()
    db.execute("DELETE FROM wiki_entity_categories WHERE wiki_entity_id = ?", (entity_id,))
    for category in categories:
        normalized = normalize_text(category.removeprefix("Category:"))
        if not normalized:
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO wiki_entity_categories
                (wiki_entity_id, category_title, normalized_category, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (entity_id, category, normalized, now),
        )


def _insert_entity_aliases(
    db: sqlite3.Connection,
    entity_id: int,
    entity: dict[str, Any],
    page: dict[str, Any] | None,
    submitted_text: str | None,
) -> None:
    """Store labels, aliases, page title, and the accepted lookup text for reuse."""

    alias_sources: list[tuple[str, str]] = []
    labels = entity.get("labels", {})
    if labels.get("en", {}).get("value"):
        alias_sources.append((labels["en"]["value"], "label"))
    for alias in entity.get("aliases", {}).get("en", []):
        if alias.get("value"):
            alias_sources.append((alias["value"], "wikidata_alias"))
    if page and page.get("title"):
        alias_sources.append((page["title"], "wikipedia_title"))
    if submitted_text:
        alias_sources.append((submitted_text, "submitted_text"))

    now = utc_now_iso()
    for alias, source in alias_sources:
        normalized = normalize_text(alias)
        if not normalized:
            continue
        db.execute(
            """
            INSERT OR IGNORE INTO wiki_entity_aliases
                (wiki_entity_id, alias, normalized_alias, source, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (entity_id, alias, normalized, source, now),
        )


def _copy_wiki_aliases_to_element_aliases(
    db: sqlite3.Connection,
    element_id: int,
    wiki_entity_id: int,
) -> None:
    """Mirror cached entity aliases into the existing answer matching table."""

    now = utc_now_iso()
    rows = db.execute(
        """
        SELECT alias, normalized_alias
        FROM wiki_entity_aliases
        WHERE wiki_entity_id = ?
        """,
        (wiki_entity_id,),
    ).fetchall()
    for row in rows:
        db.execute(
            """
            INSERT OR IGNORE INTO element_aliases
                (element_id, alias, normalized_alias, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (element_id, row["alias"], row["normalized_alias"], now),
        )


def _entity_from_row(row: sqlite3.Row) -> CachedWikiEntity:
    return CachedWikiEntity(
        id=int(row["id"]),
        qid=row["qid"],
        page_title=row["page_title"],
        canonical_name=row["canonical_name"],
        description=row["description"],
    )
