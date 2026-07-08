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
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
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


ANIMAL_BRANCH_TARGETS = {
    # These branch QIDs are broad animal classes that let non-food categories
    # reject obvious living-creature answers early when Wikidata exposes a clear
    # instance/subclass relationship, such as peafowl -> bird.
    "Q729",
    "Q7377",
    "Q5113",
    "Q10811",
    "Q152",
    "Q10908",
    "Q1390",
    "Q1358",
    "Q25326",
    "Q25364",
}


HUMAN_EXCLUSION_TARGETS = {
    # Wikidata represents real people such as Albert Einstein as instances of
    # human. For categories like fruits, this is as decisive as seeing a bird or
    # mammal branch, so the live walk can reject early instead of exploring a
    # person's generic class relationships.
    "Q5",
}


ENTITY_BUCKET_TARGETS = {
    # These broad buckets are deliberately coarse. They are not meant to prove
    # category membership; they only catch obvious incompatibilities before the
    # more expensive category-specific target walk runs.
    "person": HUMAN_EXCLUSION_TARGETS,
    "animal": ANIMAL_BRANCH_TARGETS,
    "place": {"Q2221906", "Q17334923", "Q618123", "Q82794", "Q6256", "Q515", "Q35657"},
    "creative_work": {"Q17537576", "Q7725634", "Q11424", "Q5398426", "Q15416", "Q7889"},
    "organization": {"Q43229", "Q4830453", "Q6881511"},
}


# These exclusion groups are intentionally broad and conservative. They only
# reject direct first-hop Wikidata classifications that are clearly impossible
# for a category family, which keeps the cheap prefilter from replacing the
# deeper category-specific proof for plausible answers.
EXCLUDE_PERSON_ANSWERS = {"person", "animal", "place", "creative_work", "organization"}
EXCLUDE_PLACE_ANSWERS = {"person", "animal", "creative_work", "organization"}
EXCLUDE_CREATIVE_WORK_ANSWERS = {"person", "animal", "place", "organization"}
EXCLUDE_ORGANIZATION_ANSWERS = {"person", "animal", "place", "creative_work"}
EXCLUDE_ANIMAL_ANSWERS = {"person", "place", "creative_work", "organization"}
EXCLUDE_FOOD_ANSWERS = {"person", "place", "creative_work", "organization"}
EXCLUDE_ABSTRACT_ANSWERS = {"person", "animal", "place", "creative_work", "organization"}


CATEGORY_RULES: dict[str, dict[str, Any]] = {
    "animals": {
        "targets": ANIMAL_BRANCH_TARGETS,
        "excluded_buckets": EXCLUDE_ANIMAL_ANSWERS,
        "properties": {PARENT_TAXON},
        "max_depth": 24,
        "mode": "taxonomy",
        "reason": "taxon ancestry reaches Animalia or a configured animal branch",
    },
    "countries": {
        "targets": {"Q3624078", "Q6256"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches country or sovereign state",
    },
    "fruits": {
        "targets": {"Q3314483", "Q1364", "Q1470762"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF, PART_OF, USE},
        "mode": "flat",
        "reason": "structured claims connect to culinary or botanical fruit",
    },
    "us-states": {
        "targets": {"Q35657"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches U.S. state",
    },
    "women": {
        "targets": {"Q6581072"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {SEX_OR_GENDER},
        "max_depth": 2,
        "mode": "flat",
        "reason": "sex or gender claim reaches female",
    },
    "men": {
        "targets": {"Q6581097"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {SEX_OR_GENDER},
        "max_depth": 2,
        "mode": "flat",
        "reason": "sex or gender claim reaches male",
    },
    "foods": {
        "targets": {"Q2095", "Q746549", "Q25403900"},
        "excluded_buckets": EXCLUDE_FOOD_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF, PART_OF, USE},
        "mode": "flat",
        "reason": "structured claims connect to food, dish, or food ingredient",
    },
    "cities": {
        "targets": {"Q515", "Q1093829", "Q1549591", "Q62049", "Q2154459", "Q15127012"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches city or compatible U.S. municipality type",
    },
    "movies": {
        "targets": {"Q11424"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches film",
    },
    "tv-shows": {
        "targets": {"Q5398426", "Q15416"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches television series or television program",
    },
    "video-games": {
        "targets": {"Q7889"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches video game",
    },
    "musicians": {
        "targets": {"Q639669"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches musician",
    },
    "dog-breeds": {
        "targets": {"Q39367"},
        "excluded_buckets": EXCLUDE_ANIMAL_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches dog breed",
    },
    "pokemon": {
        "targets": {"Q3966183"},
        "excluded_buckets": EXCLUDE_ANIMAL_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"pokemon species"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a Pokemon species",
    },
    "harry-potter-characters": {
        "category_patterns": {"harry potter characters"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "mode": "flat",
        "reason": "page categories identify a Harry Potter character",
    },
    "lord-of-the-rings-characters": {
        "category_patterns": {"lord of the rings characters", "the lord of the rings characters", "middle earth characters"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "mode": "flat",
        "reason": "page categories identify a Lord of the Rings character",
    },
    "gods-deities": {
        "targets": {"Q178885", "Q22989102"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"deities", "gods"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a god or deity",
    },
    "biblical-figures": {
        "category_patterns": {"biblical people", "biblical figures", "people in the hebrew bible", "people in the new testament"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "mode": "flat",
        "reason": "page categories identify a Biblical figure",
    },
    "currencies": {
        "targets": {"Q8142"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches currency",
    },
    "languages": {
        "targets": {"Q34770", "Q20162172", "Q33742"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches language or human language",
    },
    "rivers": {
        "targets": {"Q4022"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches river",
    },
    "mountains": {
        "targets": {"Q8502"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches mountain",
    },
    "islands": {
        "targets": {"Q23442"},
        "excluded_buckets": EXCLUDE_PLACE_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches island",
    },
    "us-presidents": {
        "targets": {"Q11696"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {POSITION_HELD},
        "max_depth": 2,
        "mode": "flat",
        "reason": "position-held claim reaches president of the United States",
    },
    "world-leaders": {
        "targets": {"Q48352", "Q2285706"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
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
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches philosopher",
    },
    "painters": {
        "targets": {"Q1028181"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches painter",
    },
    "authors": {
        "targets": {"Q482980"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches author",
    },
    "poets": {
        "targets": {"Q49757"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches poet",
    },
    "anime": {
        "targets": {"Q1107"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"anime"},
        "mode": "flat",
        "reason": "structured claims or page categories identify anime",
    },
    "actors": {
        "targets": {"Q33999"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "mode": "flat",
        "reason": "occupation path reaches actor",
    },
    "albums": {
        "targets": {"Q482994"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"albums"},
        "mode": "flat",
        "reason": "structured claims or page categories identify an album",
    },
    "athletes": {
        "targets": {"Q2066131"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"sportspeople", "athletes", "players"},
        "mode": "flat",
        "reason": "occupation path or page categories identify an athlete",
    },
    "chemical-elements": {
        "targets": {"Q11344"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches chemical element",
    },
    "dinosaurs": {
        "targets": {"Q430"},
        "excluded_buckets": EXCLUDE_ANIMAL_ANSWERS,
        "properties": {PARENT_TAXON},
        "max_depth": 24,
        "mode": "taxonomy",
        "reason": "taxon ancestry reaches Dinosauria",
    },
    "classical-composers": {
        "targets": {"Q36834"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"classical composers"},
        "mode": "flat",
        "reason": "occupation path or page categories identify a classical composer",
    },
    "novels": {
        "targets": {"Q8261"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"novels"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a novel",
    },
    "nobel-prize-winners": {
        "targets": {"Q7191"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {AWARD_RECEIVED},
        "max_depth": 2,
        "category_patterns": {"nobel laureates", "nobel prize winners"},
        "mode": "flat",
        "reason": "award-received claim or page categories identify a Nobel Prize winner",
    },
    "olympic-sports": {
        "targets": {"Q212434"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"olympic sports", "sports at the olympics"},
        "mode": "flat",
        "reason": "structured claims or page categories identify an Olympic sport",
    },
    "paintings": {
        "targets": {"Q3305213"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"paintings"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a painting",
    },
    "scientists": {
        "targets": {"Q901"},
        "excluded_buckets": EXCLUDE_PERSON_ANSWERS - {"person"},
        "properties": {OCCUPATION, SUBCLASS_OF},
        "category_patterns": {"scientists"},
        "mode": "flat",
        "reason": "occupation path or page categories identify a scientist",
    },
    "sports-franchises": {
        "targets": {"Q847017"},
        "excluded_buckets": EXCLUDE_ORGANIZATION_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"sports clubs", "sports teams", "sports franchises"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a sports franchise",
    },
    "fonts": {
        "targets": {"Q17451"},
        "excluded_buckets": EXCLUDE_CREATIVE_WORK_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"typefaces", "fonts"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a typeface or font",
    },
    "religions": {
        "targets": {"Q9174"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "mode": "flat",
        "reason": "instance/subclass path reaches religion",
    },
    "medical-specialties": {
        "targets": {"Q930752"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"medical specialties", "medical specialities"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a medical specialty",
    },
    "units-of-measurement": {
        "targets": {"Q47574"},
        "excluded_buckets": EXCLUDE_ABSTRACT_ANSWERS,
        "properties": {INSTANCE_OF, SUBCLASS_OF},
        "category_patterns": {"units of measurement"},
        "mode": "flat",
        "reason": "structured claims or page categories identify a unit of measurement",
    },
}


WIKIDATA_TRAVERSAL_PRUNE_QIDS = {
    # These QIDs are ontology scaffolding that frequently appear after the
    # useful gameplay signal has already been missed. Walking through them tends
    # to spend request budget on abstract Wikidata modeling concepts instead of
    # evidence that an answer belongs to a player-facing category.
    "Q55983715",  # organisms known by a particular common name
    "Q19478619",  # metaclass
    "Q21871294",  # group or class of living things
    "Q115949945",  # scientific concept
}


_WIKIDATA_ACTION_API_THROTTLED = False
_WIKI_THROTTLED_UNTIL = 0.0
_NEGATIVE_VERIFICATION_CACHE: dict[tuple[int, str], float] = {}


class WikiVerificationError(RuntimeError):
    """Raised when the remote wiki APIs fail before an answer can be verified."""


class WikiRateLimitedError(WikiVerificationError):
    """Raised when Wikimedia asks this process to stop making requests briefly."""


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
    claims_complete: bool


@dataclass(frozen=True)
class MembershipResult:
    is_member: bool
    reason: str


@dataclass
class WikiRequestBudget:
    """Track the remote work allowed for one live answer verification.

    Cached local rows do not consume budget because they do not touch Wikimedia.
    Only cold-cache network work is counted, which lets common/cached answers
    keep behaving normally while preventing one obscure answer from walking a
    large Wikidata graph during gameplay.
    """

    wikipedia_lookup_remaining: int
    lightweight_wikidata_fetches_remaining: int
    primary_wikidata_fetches_remaining: int
    related_qid_fetches_remaining: int
    live_path_max_nodes: int

    def consume_wikipedia_lookup(self, submitted_text: str) -> None:
        """Reserve the single Wikipedia lookup flow for this answer.

        The lookup flow uses one combined page search/pageprops request in the
        normal path. If a category-pattern fallback later needs page categories,
        that extra request happens only after Wikidata checks fail; this budget
        still catches accidental repeated full resolution of the same answer.
        """

        if self.wikipedia_lookup_remaining <= 0:
            current_app.logger.error(
                "Wiki request budget exhausted before resolving submitted text %r.",
                submitted_text,
            )
            raise WikiVerificationError("Could not verify that answer right now.")
        self.wikipedia_lookup_remaining -= 1

    def consume_lightweight_wikidata_fetch(self, qid: str) -> None:
        """Reserve the tiny direct-claim fetch used for obvious exclusions.

        This fetch is intentionally separate from the full primary entity fetch:
        it lets answers such as Barack Obama for Fruit reject after a small P31
        lookup, while still allowing plausible answers to fetch the full entity
        later in the same request.
        """

        if self.lightweight_wikidata_fetches_remaining <= 0:
            current_app.logger.error(
                "Wiki request budget exhausted before fetching lightweight claims for %s.",
                qid,
            )
            raise WikiVerificationError("Could not verify that answer right now.")
        self.lightweight_wikidata_fetches_remaining -= 1

    def consume_primary_wikidata_fetch(self, qid: str) -> None:
        """Reserve the one primary Wikidata entity fetch for the submitted answer."""

        if self.primary_wikidata_fetches_remaining <= 0:
            current_app.logger.error(
                "Wiki request budget exhausted before fetching primary entity %s.",
                qid,
            )
            raise WikiVerificationError("Could not verify that answer right now.")
        self.primary_wikidata_fetches_remaining -= 1

    def consume_related_qid_fetch(self, qid: str) -> None:
        """Reserve one cold-cache related QID fetch during membership walking.

        Related QIDs are where request chains used to balloon. Stopping here
        means the answer is temporarily unverifiable rather than letting a player
        request trigger dozens of upstream calls.
        """

        if self.related_qid_fetches_remaining <= 0:
            current_app.logger.error(
                "Wiki request budget exhausted before fetching related entity %s.",
                qid,
            )
            raise WikiVerificationError("Could not verify that answer right now.")
        self.related_qid_fetches_remaining -= 1


def new_wiki_request_budget() -> WikiRequestBudget:
    """Create the conservative live-verification budget for one submitted answer."""

    return WikiRequestBudget(
        wikipedia_lookup_remaining=1,
        lightweight_wikidata_fetches_remaining=1,
        primary_wikidata_fetches_remaining=1,
        related_qid_fetches_remaining=_config_int("WIKI_LIVE_RELATED_QID_FETCH_BUDGET", 5),
        live_path_max_nodes=_config_int("WIKI_LIVE_PATH_MAX_NODES", 8),
    )


def recently_failed_live_verification(db: sqlite3.Connection, category_id: int, normalized: str) -> bool:
    """Return whether a recent cold-cache failure should short-circuit retries.

    The in-memory cache catches repeats within one worker, and the SQLite row
    lets separate workers share the same short-lived answer failure. Expired
    entries are deleted lazily so temporary throttles never become permanent
    answer data.
    """

    key = (category_id, normalized)
    memory_expires_at = _NEGATIVE_VERIFICATION_CACHE.get(key)
    if memory_expires_at is not None and memory_expires_at > time.monotonic():
        return True
    if memory_expires_at is not None:
        _NEGATIVE_VERIFICATION_CACHE.pop(key, None)

    row = db.execute(
        """
        SELECT expires_at
        FROM wiki_verification_failures
        WHERE category_id = ? AND normalized_text = ?
        """,
        (category_id, normalized),
    ).fetchone()
    if not row:
        return False
    try:
        expires_at = datetime.fromisoformat(row["expires_at"])
    except ValueError:
        current_app.logger.error(
            "Invalid wiki verification failure expiry %r for category %s text %r.",
            row["expires_at"],
            category_id,
            normalized,
        )
        db.execute(
            "DELETE FROM wiki_verification_failures WHERE category_id = ? AND normalized_text = ?",
            (category_id, normalized),
        )
        return False
    if expires_at <= datetime.now(timezone.utc):
        db.execute(
            "DELETE FROM wiki_verification_failures WHERE category_id = ? AND normalized_text = ?",
            (category_id, normalized),
        )
        return False
    _NEGATIVE_VERIFICATION_CACHE[key] = time.monotonic() + max(
        (expires_at - datetime.now(timezone.utc)).total_seconds(),
        0.0,
    )
    return True


def remember_failed_live_verification(db: sqlite3.Connection, category_id: int, normalized: str) -> None:
    """Temporarily remember a failed uncached verification attempt.

    The row is intentionally overwritten on repeat failures so another worker
    can stop retrying the same unverifiable answer until the configured TTL
    passes. This is not a permanent invalid-answer cache.
    """

    ttl = _config_int("WIKI_NEGATIVE_CACHE_TTL_SECONDS", 300)
    if ttl <= 0:
        return
    _NEGATIVE_VERIFICATION_CACHE[(category_id, normalized)] = time.monotonic() + ttl
    now = datetime.now(timezone.utc)
    db.execute(
        """
        INSERT INTO wiki_verification_failures
            (category_id, normalized_text, message, expires_at, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(category_id, normalized_text) DO UPDATE SET
            message = excluded.message,
            expires_at = excluded.expires_at,
            created_at = excluded.created_at
        """,
        (
            category_id,
            normalized,
            "Could not verify that answer right now.",
            (now + timedelta(seconds=ttl)).isoformat(),
            now.isoformat(),
        ),
    )


def _config_int(key: str, default: int) -> int:
    """Read an integer config value and log unexpected values before falling back."""

    value = current_app.config.get(key, default)
    try:
        return int(value)
    except (TypeError, ValueError):
        current_app.logger.error("Config %s must be an integer; using %s.", key, default)
        return default


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


def resolve_and_cache_entity(
    db: sqlite3.Connection,
    submitted_text: str,
    budget: WikiRequestBudget | None = None,
) -> CachedWikiEntity:
    """Resolve submitted text through Wikipedia, then cache the linked Wikidata item."""

    return _resolve_and_cache_entity_from_page(db, submitted_text, budget=budget)


def resolve_and_cache_entity_for_category(
    db: sqlite3.Connection,
    submitted_text: str,
    category: sqlite3.Row,
    budget: WikiRequestBudget | None = None,
) -> CachedWikiEntity:
    """Resolve an uncached answer with a cheap category-specific reject screen.

    The normal full Wikidata entity can be very large for people and other
    heavily-linked subjects. First fetch only the direct ``instance of`` claims.
    If those claims already prove the answer is incompatible, or directly prove
    the category target such as ``New Hampshire -> U.S. state``, cache that
    minimal fact and skip the full entity download.
    """

    started_at = time.perf_counter()
    if budget:
        budget.consume_wikipedia_lookup(submitted_text)
    page = _timed_step(
        "wiki resolve page search",
        lambda: _resolve_page_metadata(submitted_text),
        submitted_text=submitted_text,
    )
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

    rule = CATEGORY_RULES.get(category["slug"])
    excluded_buckets = set(rule.get("excluded_buckets", set())) if rule else set()
    properties = set(rule.get("properties", set())) if rule else set()
    direct_claim_entity = None
    if excluded_buckets or properties:
        direct_claim_entity = _timed_step(
            "wiki resolve lightweight direct claims",
            lambda: _fetch_wikidata_direct_claims(qid, budget=budget),
            submitted_text=submitted_text,
            qid=qid,
        )
        direct_claim_qids = _claim_value_qids_from_entity(direct_claim_entity)
        if _direct_claim_bucket(direct_claim_qids, excluded_buckets):
            entity_id = _timed_step(
                "wiki resolve upsert lightweight entity",
                lambda: _upsert_wiki_entity(db, direct_claim_entity, page, submitted_text, claims_complete=False),
                submitted_text=submitted_text,
                qid=qid,
            )
            _timed_step(
                "wiki resolve replace lightweight claims",
                lambda: _replace_wiki_entity_claims(db, entity_id, direct_claim_entity),
                submitted_text=submitted_text,
                qid=qid,
            )
            _timed_step(
                "wiki resolve insert lightweight aliases",
                lambda: _insert_entity_aliases(db, entity_id, direct_claim_entity, page, submitted_text),
                submitted_text=submitted_text,
                qid=qid,
            )
            resolved = get_entity_by_id(db, entity_id)
            _log_timing("wiki resolve lightweight exclusion total", started_at, submitted_text=submitted_text, qid=qid)
            return resolved
        if direct_claim_qids & set(rule.get("targets", set())):
            entity_id = _timed_step(
                "wiki resolve upsert lightweight entity",
                lambda: _upsert_wiki_entity(db, direct_claim_entity, page, submitted_text, claims_complete=False),
                submitted_text=submitted_text,
                qid=qid,
            )
            _timed_step(
                "wiki resolve replace lightweight claims",
                lambda: _replace_wiki_entity_claims(db, entity_id, direct_claim_entity),
                submitted_text=submitted_text,
                qid=qid,
            )
            _timed_step(
                "wiki resolve insert lightweight aliases",
                lambda: _insert_entity_aliases(db, entity_id, direct_claim_entity, page, submitted_text),
                submitted_text=submitted_text,
                qid=qid,
            )
            resolved = get_entity_by_id(db, entity_id)
            _log_timing("wiki resolve lightweight direct target total", started_at, submitted_text=submitted_text, qid=qid)
            return resolved

    return _resolve_and_cache_entity_from_page(db, submitted_text, page=page, budget=budget, started_at=started_at)


def _resolve_and_cache_entity_from_page(
    db: sqlite3.Connection,
    submitted_text: str,
    page: dict[str, Any] | None = None,
    budget: WikiRequestBudget | None = None,
    started_at: float | None = None,
) -> CachedWikiEntity:
    """Fetch and cache the full Wikidata entity for a resolved page."""

    if started_at is None:
        started_at = time.perf_counter()
    if page is None:
        if budget:
            budget.consume_wikipedia_lookup(submitted_text)
        page = _timed_step(
            "wiki resolve page search",
            lambda: _resolve_page_metadata(submitted_text),
            submitted_text=submitted_text,
        )
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

    entity = _timed_step(
        "wiki resolve primary wikidata entity",
        lambda: _fetch_wikidata_entity(qid, budget=budget, fetch_kind="primary"),
        submitted_text=submitted_text,
        qid=qid,
    )
    entity_id = _timed_step(
        "wiki resolve upsert entity",
        lambda: _upsert_wiki_entity(db, entity, page, submitted_text, claims_complete=True),
        submitted_text=submitted_text,
        qid=qid,
    )
    _timed_step(
        "wiki resolve replace claims",
        lambda: _replace_wiki_entity_claims(db, entity_id, entity),
        submitted_text=submitted_text,
        qid=qid,
    )
    _timed_step(
        "wiki resolve insert aliases",
        lambda: _insert_entity_aliases(db, entity_id, entity, page, submitted_text),
        submitted_text=submitted_text,
        qid=qid,
    )
    resolved = get_entity_by_id(db, entity_id)
    _log_timing("wiki resolve total", started_at, submitted_text=submitted_text, qid=qid)
    return resolved


def ensure_qid_cached(
    db: sqlite3.Connection,
    qid: str,
    budget: WikiRequestBudget | None = None,
    fetch_kind: str = "related",
    require_complete: bool = True,
) -> CachedWikiEntity | None:
    """Make sure an ancestor/class QID exists in the local structured cache.

    Category checks walk from the submitted entity through its class, subclass,
    and taxonomy claims. When a claim points at an uncached QID, this function
    fetches just enough Wikidata data for that node to continue the walk.
    """

    row = db.execute("SELECT * FROM wiki_entities WHERE qid = ?", (qid,)).fetchone()
    if row and (not require_complete or bool(row["claims_complete"])):
        return _entity_from_row(row)

    try:
        entity = _timed_step(
            "wiki related wikidata entity",
            lambda: _fetch_wikidata_entity(qid, budget=budget, fetch_kind=fetch_kind),
            qid=qid,
            fetch_kind=fetch_kind,
        )
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
    budget: WikiRequestBudget | None = None,
) -> MembershipResult:
    """Check and cache whether one wiki entity belongs to one game category."""

    started_at = time.perf_counter()
    cached = db.execute(
        """
        SELECT is_member, reason
        FROM category_entity_memberships
        WHERE category_id = ? AND wiki_entity_id = ?
        """,
        (int(category["id"]), entity.id),
    ).fetchone()
    if cached:
        _log_timing(
            "wiki membership cached",
            started_at,
            category=category["slug"],
            qid=entity.qid,
            is_member=bool(cached["is_member"]),
        )
        return MembershipResult(bool(cached["is_member"]), cached["reason"])

    slug = category["slug"]
    rule = CATEGORY_RULES.get(slug)
    properties = set(rule.get("properties", set())) if rule else set()
    if not rule:
        current_app.logger.error("Category %r has no live wiki membership rule.", slug)
        result = MembershipResult(False, "No live verification rule is configured for this category.")
    elif _timed_step(
        "wiki membership excluded bucket check",
        lambda: _has_any_excluded_bucket(db, entity.qid, set(rule.get("excluded_buckets", set())), budget=budget),
        category=slug,
        qid=entity.qid,
    ):
        result = MembershipResult(False, "Excluded by category rule.")
    elif properties and _timed_step(
        "wiki membership target path check",
        lambda: _has_any_path(
            db,
            entity.qid,
            set(rule["targets"]),
            properties,
            max_depth=int(rule.get("max_depth", 10)),
            max_nodes=budget.live_path_max_nodes if budget else 40,
            budget=budget,
            walk_name="target",
        ),
        category=slug,
        qid=entity.qid,
    ):
        result = MembershipResult(True, rule["reason"])
    elif _timed_step(
        "wiki membership category pattern check",
        lambda: _has_any_matching_category(db, entity, set(rule.get("category_patterns", set()))),
        category=slug,
        qid=entity.qid,
    ):
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
    _log_timing(
        "wiki membership total",
        started_at,
        category=slug,
        qid=entity.qid,
        is_member=result.is_member,
        reason=result.reason,
    )
    return result


def upsert_category_element(
    db: sqlite3.Connection,
    category: sqlite3.Row,
    entity: CachedWikiEntity,
    budget: WikiRequestBudget | None = None,
) -> int:
    """Create or update the category-specific answer row for a verified entity."""

    parent_id = None
    if category["slug"] == "animals":
        parent_id = _ensure_taxonomy_parent_element(db, int(category["id"]), entity.qid, budget=budget)

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


def _ensure_taxonomy_parent_element(
    db: sqlite3.Connection,
    category_id: int,
    qid: str,
    budget: WikiRequestBudget | None = None,
) -> int | None:
    """Ensure the immediate parent taxon exists as a category element.

    Animal replacement logic depends on ``category_elements.parent_id``. Wikidata
    stores taxonomy in ``P171`` claims, so this helper lazily mirrors that parent
    chain into the existing game table whenever an animal answer is accepted.
    """

    try:
        parent_qid = _first_claim_value(db, qid, PARENT_TAXON, budget=budget)
    except WikiVerificationError as exc:
        current_app.logger.error("Could not inspect parent taxon for %s: %s", qid, exc)
        return None
    if not parent_qid:
        return None
    if parent_qid == qid:
        current_app.logger.error("Broken taxonomy chain for %s: parent points to itself.", qid)
        return None

    try:
        parent = ensure_qid_cached(db, parent_qid, budget=budget)
    except WikiVerificationError as exc:
        current_app.logger.error("Could not cache parent taxon %s for child %s: %s", parent_qid, qid, exc)
        return None
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
        grandparent_id = None if parent.qid in branch_targets else _ensure_taxonomy_parent_element(db, category_id, parent.qid, budget=budget)
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
    grandparent_id = None if parent.qid in branch_targets else _ensure_taxonomy_parent_element(db, category_id, parent.qid, budget=budget)
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
    budget: WikiRequestBudget | None = None,
    walk_name: str = "target",
) -> bool:
    """Walk Wikidata QID relationships looking for a category target.

    The walk is allowed to use cached rows freely, but cold-cache related QIDs
    spend from the live answer budget. If that budget runs out, the caller gets
    a temporary verification failure instead of a false category-membership
    result, which avoids poisoning the permanent membership cache.
    """

    if not target_qids:
        return False
    queue: list[tuple[str, int]] = [(start_qid, 0)]
    seen: set[str] = set()
    # Keep a plain record of the nodes this gameplay request actually examined
    # so production logs can explain surprising category decisions. This log is
    # intentionally built only from entities already fetched for verification;
    # it must never trigger additional Wikimedia requests just to make the log
    # prettier.
    traversed_nodes: list[tuple[str, int, str | None]] = []
    while queue:
        if len(seen) >= max_nodes:
            current_app.logger.warning(
                "Stopped Wikidata path walk from %s after %s nodes without finding %s. Traversed nodes: %s",
                start_qid,
                max_nodes,
                sorted(target_qids),
                _format_traversed_nodes(traversed_nodes),
            )
            if budget:
                raise WikiVerificationError("Could not verify that answer right now.")
            return False
        qid, depth = queue.pop(0)
        if qid in target_qids:
            traversed_nodes.append((qid, depth, _cached_qid_name(db, qid)))
            _log_path_walk_success(
                walk_name,
                "Wikidata path walk from %s found target %s. Traversed nodes: %s",
                start_qid,
                qid,
                _format_traversed_nodes(traversed_nodes),
            )
            return True
        if qid in seen or depth >= max_depth:
            continue
        seen.add(qid)
        entity = ensure_qid_cached(db, qid, budget=budget)
        if not entity:
            traversed_nodes.append((qid, depth, None))
            continue
        traversed_nodes.append((qid, depth, entity.canonical_name))
        value_qids = _claim_values(db, entity.id, properties)
        for value_qid in value_qids:
            if value_qid in target_qids:
                traversed_nodes.append((value_qid, depth + 1, _cached_qid_name(db, value_qid)))
                _log_path_walk_success(
                    walk_name,
                    "Wikidata path walk from %s reached target %s through %s. Traversed nodes: %s",
                    start_qid,
                    value_qid,
                    qid,
                    _format_traversed_nodes(traversed_nodes),
                )
                return True
            if value_qid in WIKIDATA_TRAVERSAL_PRUNE_QIDS:
                traversed_nodes.append((value_qid, depth + 1, _cached_qid_name(db, value_qid)))
                current_app.logger.info(
                    "Pruned generic Wikidata node %s while walking from %s. Traversed nodes so far: %s",
                    value_qid,
                    start_qid,
                    _format_traversed_nodes(traversed_nodes),
                )
                continue
            if value_qid not in seen:
                queue.append((value_qid, depth + 1))
    current_app.logger.info(
        "Wikidata path walk from %s did not find targets %s. Traversed nodes: %s",
        start_qid,
        sorted(target_qids),
        _format_traversed_nodes(traversed_nodes),
    )
    return False


def _has_any_excluded_bucket(
    db: sqlite3.Connection,
    start_qid: str,
    excluded_buckets: set[str],
    budget: WikiRequestBudget | None = None,
) -> bool:
    """Return whether an entity clearly falls into a disallowed broad bucket.

    Buckets are a cheap prefilter for gameplay categories. For example, Fruit
    does not need a deep fruit proof once the answer classifies as ``person`` or
    ``animal`` through the same early Wikidata claims that category walking
    already uses.
    """

    if not excluded_buckets:
        return False
    unknown_buckets = excluded_buckets - set(ENTITY_BUCKET_TARGETS)
    if unknown_buckets:
        current_app.logger.error(
            "Category rule referenced unknown excluded entity buckets: %s.",
            sorted(unknown_buckets),
        )
    direct_bucket = _direct_entity_bucket(db, start_qid, excluded_buckets, budget=budget)
    if direct_bucket:
        current_app.logger.warning(
            "Wikidata classified %s into excluded bucket %r from direct claims.",
            start_qid,
            direct_bucket,
        )
        return True
    return False


def _direct_entity_bucket(
    db: sqlite3.Connection,
    start_qid: str,
    candidate_buckets: set[str],
    budget: WikiRequestBudget | None = None,
) -> str | None:
    """Classify an entity from its first-hop claims before doing path walks.

    Many obvious rejects are direct ``instance of`` or ``subclass of`` values:
    Einstein -> human and peafowl -> bird. Checking those cached claims first
    avoids spending budget proving the answer is not in every other bucket.
    """

    entity = ensure_qid_cached(db, start_qid, budget=budget, require_complete=False)
    if not entity:
        return None
    direct_values = set(_claim_values(db, entity.id, {INSTANCE_OF, SUBCLASS_OF, PART_OF, USE}))
    return _direct_claim_bucket(direct_values, candidate_buckets)


def _direct_claim_bucket(direct_values: set[str], candidate_buckets: set[str]) -> str | None:
    """Return the excluded bucket hit by already-fetched direct claim QIDs."""

    for bucket in sorted(candidate_buckets):
        targets = ENTITY_BUCKET_TARGETS.get(bucket)
        if targets and direct_values & targets:
            return bucket
    return None


def _log_path_walk_success(walk_name: str, message: str, *args: Any) -> None:
    """Log path-walk hits at a level that matches their gameplay meaning.

    Target hits are normal successful validation details, so they remain info.
    Exclusion hits explain an early rejection such as peafowl -> bird for a
    fruit answer, so they are warnings to remain visible in the same logs that
    previously showed node-cap warnings.
    """

    if walk_name == "exclusion":
        current_app.logger.warning(message, *args)
    else:
        current_app.logger.info(message, *args)


def _cached_qid_name(db: sqlite3.Connection, qid: str) -> str | None:
    """Return a cached display name for log output without making network calls."""

    row = db.execute("SELECT canonical_name FROM wiki_entities WHERE qid = ?", (qid,)).fetchone()
    return row["canonical_name"] if row else None


def _format_traversed_nodes(nodes: list[tuple[str, int, str | None]]) -> str:
    """Format a compact path-walk trace for logs.

    The depth annotation makes it easier to see why an answer was rejected: an
    unrelated answer usually fans out through claims such as species/taxon or
    instance/subclass nodes that never approach the configured category targets.
    """

    if not nodes:
        return "[]"
    formatted = []
    for qid, depth, name in nodes:
        label = f"{qid}:{name}" if name else qid
        formatted.append(f"{label}@depth{depth}")
    return "[" + " -> ".join(formatted) + "]"


def _has_any_matching_category(
    db: sqlite3.Connection,
    entity: CachedWikiEntity,
    category_patterns: set[str],
) -> bool:
    """Return whether cached Wikipedia categories match configured text hints.

    Wikidata paths are preferred for categories with crisp structured claims,
    but some game categories, especially fictional universes and broad award
    groupings, are more consistently exposed through Wikipedia category titles.
    The patterns are normalized with the same helper used for cached category
    titles, then checked as substrings so labels such as "American Nobel
    laureates" can still satisfy the broader "Nobel laureates" rule.

    Categories are fetched lazily here instead of during the initial answer
    resolution. Most answers are accepted or rejected through Wikidata claims,
    so fetching page categories up front added a full Wikipedia round trip to
    every cold-cache answer even when no category-pattern fallback was needed.
    """

    if not category_patterns:
        return False
    _ensure_wikipedia_categories_cached(db, entity)
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
        (entity.id,),
    ).fetchall()
    for row in rows:
        normalized_category = row["normalized_category"]
        if any(pattern in normalized_category for pattern in normalized_patterns):
            return True
    return False


def _ensure_wikipedia_categories_cached(db: sqlite3.Connection, entity: CachedWikiEntity) -> None:
    """Populate cached page categories only when category-pattern rules need them.

    The normal cold path avoids Wikipedia categories entirely because Wikidata
    claims usually decide the answer. When a category rule has text patterns,
    this helper performs the extra Wikipedia request at the last possible moment
    and then stores the result in the existing category cache table.
    """

    existing = db.execute(
        """
        SELECT 1
        FROM wiki_entity_categories
        WHERE wiki_entity_id = ?
        LIMIT 1
        """,
        (entity.id,),
    ).fetchone()
    if existing:
        return
    if not entity.page_title:
        current_app.logger.error("Cannot fetch Wikipedia categories for %s without a page title.", entity.qid)
        return
    categories = _timed_step(
        "wiki category fallback fetch categories",
        lambda: _fetch_wikipedia_categories(entity.page_title),
        qid=entity.qid,
        title=entity.page_title,
    )
    _timed_step(
        "wiki category fallback replace categories",
        lambda: _replace_wikipedia_categories(db, entity.id, categories),
        qid=entity.qid,
        title=entity.page_title,
    )


def _first_claim_value(
    db: sqlite3.Connection,
    qid: str,
    property_id: str,
    budget: WikiRequestBudget | None = None,
) -> str | None:
    entity = ensure_qid_cached(db, qid, budget=budget)
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


def _resolve_page_metadata(search_text: str) -> dict[str, Any]:
    """Resolve submitted text to a Wikipedia page and linked Wikidata QID.

    This uses MediaWiki's search generator so the initial cold-cache path gets
    the best page candidate, pageprops, and Wikidata item in one request. The old
    OpenSearch-then-page-metadata flow cost two Wikipedia round trips before the
    app could even fetch the Wikidata entity.
    """

    data = _wiki_json(
        current_app.config["WIKIPEDIA_API_URL"],
        {
            "action": "query",
            "generator": "search",
            "gsrsearch": search_text,
            "gsrnamespace": "0",
            "gsrlimit": "1",
            "prop": "info|pageprops",
            "redirects": "1",
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
        "title": page.get("title", search_text),
        "qid": pageprops.get("wikibase_item"),
        "disambiguation": "disambiguation" in pageprops,
    }


def _fetch_wikipedia_categories(title: str) -> list[str]:
    """Fetch Wikipedia category titles only for rules that need text fallback."""

    data = _wiki_json(
        current_app.config["WIKIPEDIA_API_URL"],
        {
            "action": "query",
            "prop": "categories",
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
    return [
        category.get("title", "")
        for category in pages[0].get("categories", [])
        if category.get("title")
    ]


def _fetch_wikidata_direct_claims(qid: str, budget: WikiRequestBudget | None = None) -> dict[str, Any]:
    """Fetch only direct instance-of claims for cheap obvious rejection.

    ``wbgetentities`` with full claims/aliases/sitelinks can be hundreds of KB
    for famous people. A direct ``P31`` claim fetch is tiny and enough to reject
    common incompatible answers such as people, birds, movies, and companies for
    categories like Fruit.
    """

    if budget:
        budget.consume_lightweight_wikidata_fetch(qid)
    data = _wiki_json(
        current_app.config["WIKIDATA_API_URL"],
        {
            "action": "wbgetclaims",
            "entity": qid,
            "property": INSTANCE_OF,
            "format": "json",
        },
    )
    return {
        "id": qid,
        "claims": data.get("claims", {}),
    }


def _claim_value_qids_from_entity(entity: dict[str, Any]) -> set[str]:
    """Extract item-valued claim QIDs from a partial or full Wikidata entity."""

    value_qids: set[str] = set()
    for claims in entity.get("claims", {}).values():
        for claim in claims:
            mainsnak = claim.get("mainsnak", {})
            datavalue = mainsnak.get("datavalue", {})
            value = datavalue.get("value")
            if not isinstance(value, dict) or value.get("entity-type") != "item":
                continue
            numeric_id = value.get("numeric-id")
            if numeric_id is not None:
                value_qids.add(f"Q{numeric_id}")
    return value_qids


def _fetch_wikidata_entity(
    qid: str,
    budget: WikiRequestBudget | None = None,
    fetch_kind: str = "related",
) -> dict[str, Any]:
    global _WIKIDATA_ACTION_API_THROTTLED

    if budget:
        if fetch_kind == "primary":
            budget.consume_primary_wikidata_fetch(qid)
        elif fetch_kind == "related":
            budget.consume_related_qid_fetch(qid)
        else:
            current_app.logger.error("Unknown Wikidata fetch budget kind %r for %s.", fetch_kind, qid)
            raise WikiVerificationError("Could not verify that answer right now.")

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
        except WikiRateLimitedError:
            _WIKIDATA_ACTION_API_THROTTLED = True
            current_app.logger.error(
                "Wikidata wbgetentities rate-limited for %s; skipping fallback during gameplay.",
                qid,
            )
            raise
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

    if _wiki_throttle_active():
        raise WikiRateLimitedError("Could not verify that answer right now.")

    request_started_at = time.perf_counter()
    request = urllib.request.Request(
        full_url,
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": current_app.config["WIKI_USER_AGENT"],
        },
    )
    for attempt in range(retries + 1):
        attempt_started_at = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                raw = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                elif "deflate" in encoding:
                    raw = zlib.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
                _log_timing(
                    "wiki http attempt",
                    attempt_started_at,
                    url=full_url,
                    attempt=attempt + 1,
                    status="ok",
                    bytes=len(raw),
                )
                _log_timing("wiki http total", request_started_at, url=full_url, attempts=attempt + 1, status="ok")
                return data
        except urllib.error.HTTPError as exc:
            _log_timing(
                "wiki http attempt",
                attempt_started_at,
                url=full_url,
                attempt=attempt + 1,
                status=f"http-{exc.code}",
            )
            retry_after = exc.headers.get("Retry-After")
            if exc.code == 429:
                retry_after_seconds = _retry_after_seconds(retry_after)
                if attempt < retries and (retry_after_seconds is None or retry_after_seconds <= 3.0):
                    time.sleep(min(retry_after_seconds or 1.0, 3.0))
                    continue
                _activate_wiki_throttle(retry_after_seconds)
                current_app.logger.error("Wiki API request rate-limited for %s: %s", full_url, exc)
                raise WikiRateLimitedError("Could not verify that answer right now.") from exc
            if attempt >= retries:
                current_app.logger.error("Wiki API request failed for %s: %s", full_url, exc)
                raise WikiVerificationError("Could not verify that answer right now.") from exc
            time.sleep(0.5 + attempt)
        except (urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            _log_timing(
                "wiki http attempt",
                attempt_started_at,
                url=full_url,
                attempt=attempt + 1,
                status=type(exc).__name__,
            )
            if attempt >= retries:
                current_app.logger.error("Wiki API request failed for %s: %s", full_url, exc)
                raise WikiVerificationError("Could not verify that answer right now.") from exc
            time.sleep(0.5 + attempt)
    raise WikiVerificationError("Could not verify that answer right now.")


def _timed_step(label: str, callback: Any, **context: Any) -> Any:
    """Run a small verification step and log its elapsed time.

    These logs are intentionally fine-grained because live verification latency
    can come from several places: remote HTTP calls, local SQLite writes, or the
    membership walk. Keeping each step visible makes slow answers diagnosable
    without changing the player-facing API.
    """

    started_at = time.perf_counter()
    try:
        result = callback()
    except Exception:
        _log_timing(label, started_at, status="error", **context)
        raise
    _log_timing(label, started_at, status="ok", **context)
    return result


def _log_timing(label: str, started_at: float, **context: Any) -> None:
    """Log one timing sample with compact key/value context."""

    elapsed_ms = (time.perf_counter() - started_at) * 1000
    context_text = " ".join(f"{key}={value!r}" for key, value in sorted(context.items()))
    if context_text:
        current_app.logger.info("%s took %.1fms %s", label, elapsed_ms, context_text)
    else:
        current_app.logger.info("%s took %.1fms", label, elapsed_ms)


def _wiki_throttle_active() -> bool:
    """Return whether a recent 429 should prevent more Wikimedia requests."""

    global _WIKI_THROTTLED_UNTIL

    if _WIKI_THROTTLED_UNTIL <= 0:
        return False
    if _WIKI_THROTTLED_UNTIL <= time.monotonic():
        _WIKI_THROTTLED_UNTIL = 0.0
        return False
    return True


def _activate_wiki_throttle(retry_after_seconds: float | None) -> None:
    """Remember Wikimedia's throttle signal without sleeping through gameplay.

    ``Retry-After`` can ask for a longer pause than a player request should wait.
    This stores the pause globally for the process so the current submission can
    fail quickly and later submissions avoid making the same doomed request.
    """

    global _WIKI_THROTTLED_UNTIL

    ttl = _config_int("WIKI_THROTTLE_TTL_SECONDS", 60)
    throttle_seconds = max(float(ttl), retry_after_seconds or 0.0)
    if throttle_seconds <= 0:
        return
    _WIKI_THROTTLED_UNTIL = max(_WIKI_THROTTLED_UNTIL, time.monotonic() + throttle_seconds)


def _retry_after_seconds(value: str | None) -> float | None:
    """Parse a Retry-After header as seconds, supporting both legal formats."""

    if not value:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError, OverflowError):
        current_app.logger.error("Could not parse Wikimedia Retry-After header %r.", value)
        return None
    if retry_at.tzinfo is None:
        retry_at = retry_at.replace(tzinfo=timezone.utc)
    return max((retry_at - datetime.now(timezone.utc)).total_seconds(), 0.0)


def _upsert_wiki_entity(
    db: sqlite3.Connection,
    entity: dict[str, Any],
    page: dict[str, Any] | None,
    submitted_text: str | None,
    claims_complete: bool = True,
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
            (qid, page_id, page_title, canonical_name, description, sitelinks, claims_complete, fetched_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(qid) DO UPDATE SET
            page_id = COALESCE(excluded.page_id, wiki_entities.page_id),
            page_title = excluded.page_title,
            canonical_name = excluded.canonical_name,
            description = CASE
                WHEN excluded.claims_complete = 1 THEN excluded.description
                ELSE wiki_entities.description
            END,
            sitelinks = CASE
                WHEN excluded.claims_complete = 1 THEN excluded.sitelinks
                ELSE wiki_entities.sitelinks
            END,
            claims_complete = CASE
                WHEN wiki_entities.claims_complete = 1 THEN 1
                ELSE excluded.claims_complete
            END,
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
            1 if claims_complete else 0,
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
        claims_complete=bool(row["claims_complete"]),
    )
