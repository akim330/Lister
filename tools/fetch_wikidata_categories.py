#!/usr/bin/env python3
"""Legacy offline fetcher for selected Lister category data from Wikidata.

Gameplay now verifies answers at submission time through Wikipedia and the
linked Wikidata entity, then caches structured facts in SQLite. This script is
kept only as a review aid for inspecting possible category data offline.
"""

from __future__ import annotations

import argparse
import datetime as dt
import difflib
import gzip
import importlib.util
import json
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

NORMALIZATION_PATH = ROOT / "app" / "services" / "normalization.py"
NORMALIZATION_SPEC = importlib.util.spec_from_file_location("lister_normalization", NORMALIZATION_PATH)
if NORMALIZATION_SPEC is None or NORMALIZATION_SPEC.loader is None:
    raise RuntimeError(f"Could not load normalization helpers from {NORMALIZATION_PATH}.")
NORMALIZATION = importlib.util.module_from_spec(NORMALIZATION_SPEC)
NORMALIZATION_SPEC.loader.exec_module(NORMALIZATION)
normalize_text = NORMALIZATION.normalize_text
singularize_phrase = NORMALIZATION.singularize_phrase


CONFIG_PATH = ROOT / "tools" / "wikidata_category_config.json"
FETCHED_AT = dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class Candidate:
    """A normalized Wikidata candidate before it is written as seed JSON.

    Wikidata rows can arrive duplicated because a single item may have multiple
    aliases, common names, or query paths. Keeping one mutable candidate per QID
    lets the fetch step merge those rows into one game element before validation.
    """

    qid: str
    name: str
    aliases: set[str] = field(default_factory=set)
    parent_qid: str | None = None
    parent_label: str | None = None
    sitelinks: int = 0
    key: str = ""
    parent_key: str | None = None
    is_playable_answer: bool = True


class FetchError(RuntimeError):
    """Raised when Wikidata cannot be reached or returns an unusable response."""


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh selected Lister category JSON from Wikidata.")
    parser.add_argument(
        "--categories",
        nargs="+",
        choices=("animals", "countries", "fruits"),
        default=["animals", "countries", "fruits"],
        help="Categories to fetch. Defaults to all Wikidata-backed categories.",
    )
    parser.add_argument(
        "--config",
        default=str(CONFIG_PATH),
        help="Path to the Wikidata fetch config JSON.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and validate data, print the report, but do not write category JSON or report files.",
    )
    args = parser.parse_args()

    config_path = Path(args.config)
    config = load_config(config_path)
    output_dir = resolve_repo_path(config.get("output_dir", "app/data/categories"))
    report_path = resolve_repo_path(config.get("report_path", "tools/wikidata_category_report.md"))

    reports: list[str] = []
    generated: dict[str, dict[str, Any]] = {}
    for slug in args.categories:
        category_config = config["categories"][slug]
        print(f"Fetching {slug} from Wikidata...", file=sys.stderr)
        category_data, report = build_category(slug, category_config, config, output_dir)
        generated[slug] = category_data
        reports.append(report)

    report_text = "\n\n".join(reports) + "\n"
    if args.dry_run:
        print(report_text)
        return 0

    # Writes happen only after every requested category has fetched and
    # validated successfully. That prevents a partial refresh where one category
    # is new but another failed midway through the run.
    for slug, category_data in generated.items():
        output_path = output_dir / f"{slug}.json"
        output_path.write_text(format_category_json(category_data), encoding="utf-8")
    report_path.write_text(report_text, encoding="utf-8")
    print(f"Wrote {len(generated)} category file(s) and {report_path.relative_to(ROOT)}.")
    return 0


def load_config(path: Path) -> dict[str, Any]:
    """Read and lightly validate the fetch config before any network requests."""

    data = json.loads(path.read_text(encoding="utf-8"))
    if "categories" not in data:
        raise ValueError(f"{path} must define a categories object.")
    return data


def resolve_repo_path(value: str) -> Path:
    """Resolve config paths relative to the repository root for stable CLI use."""

    path = Path(value)
    if path.is_absolute():
        return path
    return ROOT / path


def build_category(
    slug: str,
    category_config: dict[str, Any],
    global_config: dict[str, Any],
    output_dir: Path,
) -> tuple[dict[str, Any], str]:
    """Fetch, normalize, validate, and report one configured category."""

    previous_path = output_dir / f"{slug}.json"
    previous = load_previous_category(previous_path)
    if slug == "animals":
        candidates, notes = fetch_animals(category_config, global_config)
        if previous:
            apply_existing_taxonomy_overlay(candidates, previous, category_config, notes)
        apply_manual_taxonomy_elements(candidates, category_config, notes)
        elements = build_taxonomy_elements(candidates, category_config, notes)
    elif slug == "countries":
        candidates, notes = fetch_countries(category_config, global_config)
        elements = build_flat_elements(candidates, category_config, notes)
    elif slug == "fruits":
        candidates, notes = fetch_fruits(category_config, global_config)
        elements = build_flat_elements(candidates, category_config, notes)
    else:
        raise ValueError(f"Unsupported category: {slug}")

    category_data = {
        "slug": category_config["slug"],
        "name": category_config["name"],
        "mode": category_config["mode"],
        "source": "wikidata",
        "last_fetched_at": FETCHED_AT,
        "elements": elements,
    }
    validation_notes = validate_category(category_data)
    notes.extend(validation_notes)

    report = render_report(slug, category_data, previous, notes, category_config)
    return category_data, report


def sparql_query(query: str, global_config: dict[str, Any], retries: int = 2) -> list[dict[str, Any]]:
    """Run one Wikidata Query Service request and return binding dictionaries.

    The request follows Wikimedia automation expectations: a descriptive
    User-Agent, compression support, conservative timeout, and simple retry
    behavior for transient throttling or service errors.
    """

    endpoint = global_config["endpoint"]
    body = urllib.parse.urlencode({"query": query, "format": "json"}).encode("utf-8")
    headers = {
        "Accept": "application/sparql-results+json",
        "Accept-Encoding": "gzip, deflate",
        "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        "User-Agent": global_config["user_agent"],
    }
    request = urllib.request.Request(endpoint, data=body, headers=headers, method="POST")

    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                raw = response.read()
                encoding = response.headers.get("Content-Encoding", "")
                if "gzip" in encoding:
                    raw = gzip.decompress(raw)
                elif "deflate" in encoding:
                    raw = zlib.decompress(raw)
                data = json.loads(raw.decode("utf-8"))
                return data.get("results", {}).get("bindings", [])
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, socket.timeout) as exc:
            if attempt >= retries:
                raise FetchError(f"Wikidata request failed after {retries + 1} attempts: {exc}") from exc
            time.sleep(2 + attempt * 2)

    raise FetchError("Wikidata request failed unexpectedly.")


def fetch_animals(
    category_config: dict[str, Any],
    global_config: dict[str, Any],
) -> tuple[dict[str, Candidate], list[str]]:
    """Fetch taxonomy-heavy animal candidates below configured branch roots.

    A single transitive Animalia query is too expensive for WDQS. Instead, this
    performs a bounded breadth-first walk over direct parent-taxon edges. Each
    query is narrow, and the config controls how many child taxa are followed per
    parent and how deep the refresh may go.
    """

    root_qid = category_config["root_qid"]
    notes: list[str] = []
    candidates: dict[str, Candidate] = {}
    root = Candidate(
        qid=root_qid,
        name=category_config["root_name"],
        aliases=set(category_config.get("manual_aliases", {}).get(root_qid, [])),
        key=category_config["root_key"],
        is_playable_answer=False,
    )
    candidates[root_qid] = root

    queue: list[tuple[str, int]] = []
    for seed in category_config.get("seed_roots", []):
        qid = seed["qid"]
        candidate = Candidate(
            qid=qid,
            name=seed["name"],
            aliases=set(seed.get("aliases", [])),
            parent_qid=seed.get("parent_qid", root_qid),
            parent_label=category_config["root_name"],
            sitelinks=10_000,
        )
        candidates[qid] = candidate
        queue.append((qid, 1))

    max_depth = int(category_config.get("max_depth", 4))
    max_elements = int(category_config.get("max_elements", 300))
    children_per_parent = int(category_config.get("children_per_parent", 16))
    visited_parents: set[str] = set()
    while queue and len(candidates) < max_elements:
        parent_qid, depth = queue.pop(0)
        if parent_qid in visited_parents or depth > max_depth:
            continue
        visited_parents.add(parent_qid)
        child_rows = fetch_direct_taxon_children(parent_qid, children_per_parent, global_config)
        child_candidates = rows_to_candidates(child_rows, prefer_common_name=True, notes=notes)
        for child in child_candidates.values():
            if len(candidates) >= max_elements:
                break
            child.parent_qid = parent_qid
            child.parent_label = candidates.get(parent_qid, Candidate(parent_qid, parent_qid)).name
            existing = candidates.get(child.qid)
            if existing is None:
                candidates[child.qid] = child
                queue.append((child.qid, depth + 1))
            else:
                existing.aliases.update(child.aliases)
                if existing.parent_qid is None:
                    existing.parent_qid = parent_qid

    apply_qid_aliases(candidates, category_config.get("manual_aliases", {}))
    apply_denylist(candidates, category_config.get("denylist_qids", []), notes)
    return candidates, notes


def fetch_direct_taxon_children(
    parent_qid: str,
    limit: int,
    global_config: dict[str, Any],
) -> list[dict[str, Any]]:
    """Fetch direct taxon children for one parent QID.

    Direct-child requests are much cheaper than asking Wikidata for the whole
    transitive Animalia subtree. Ordering by sitelinks keeps the bounded child
    set biased toward taxa that are more likely to be recognizable to players.
    """

    query = f"""
SELECT ?item ?itemLabel ?itemAltLabel ?commonName ?sitelinks WHERE {{
  ?item wdt:P31 wd:Q16521 .
  ?item wdt:P171 wd:{parent_qid} .
  OPTIONAL {{
    ?item wdt:P1843 ?commonName .
    FILTER(LANG(?commonName) = "en")
  }}
  OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks . }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en" .
  }}
}}
ORDER BY DESC(?sitelinks) ?itemLabel
LIMIT {limit}
"""
    return sparql_query(query, global_config, retries=1)


def apply_existing_taxonomy_overlay(
    candidates: dict[str, Candidate],
    previous: dict[str, Any],
    category_config: dict[str, Any],
    notes: list[str],
) -> None:
    """Preserve existing game-friendly taxonomy entries as required answers.

    Wikidata is good at formal taxonomy, but a game category also needs common
    answers like "cat", "dog", and "owl". This overlay keeps the current seed
    file's animal entries in the generated result unless Wikidata already
    produced an element with the same normalized name.
    """

    root_qid = category_config["root_qid"]
    root_key = category_config["root_key"]
    elements = previous.get("elements", [])
    qid_by_previous_key: dict[str, str] = {root_key: root_qid}
    candidate_by_name = {normalize_text(candidate.name): candidate for candidate in candidates.values()}

    for element in elements:
        previous_key = element["key"]
        normalized_name = normalize_text(element["name"])
        if previous_key == root_key:
            qid_by_previous_key[previous_key] = root_qid
            continue
        existing = candidate_by_name.get(normalized_name)
        if existing is not None:
            existing.aliases.update(element.get("aliases", []))
            existing.is_playable_answer = bool(element.get("is_playable_answer", True))
            existing.key = previous_key
            qid_by_previous_key[previous_key] = existing.qid
            continue
        manual_qid = f"manual:{previous_key}"
        candidates[manual_qid] = Candidate(
            qid=manual_qid,
            name=element["name"],
            aliases=set(element.get("aliases", [])),
            is_playable_answer=bool(element.get("is_playable_answer", True)),
            sitelinks=9_000,
            key=previous_key,
        )
        qid_by_previous_key[previous_key] = manual_qid
        candidate_by_name[normalized_name] = candidates[manual_qid]

    for element in elements:
        previous_key = element["key"]
        candidate_qid = qid_by_previous_key.get(previous_key)
        if not candidate_qid or candidate_qid == root_qid:
            continue
        candidate = candidates[candidate_qid]
        parent_key = element.get("parent")
        if parent_key:
            candidate.parent_qid = qid_by_previous_key.get(parent_key, root_qid)
        elif candidate.parent_qid is None:
            candidate.parent_qid = root_qid

    notes.append("Preserved existing animal seed entries as required game-friendly taxonomy answers.")


def apply_manual_taxonomy_elements(
    candidates: dict[str, Candidate],
    category_config: dict[str, Any],
    notes: list[str],
) -> None:
    """Add configured common-name animals that Wikidata taxonomy may miss.

    The WDQS animal walk intentionally follows formal parent-taxon edges, which
    can skip everyday names that players reasonably expect to type. These manual
    elements keep those gameplay-friendly answers in the generated file while
    still letting them participate in taxonomy replacement logic.
    """

    manual_qids_by_key: dict[str, str] = {category_config["root_key"]: category_config["root_qid"]}
    for candidate in candidates.values():
        if candidate.key:
            manual_qids_by_key[candidate.key] = candidate.qid

    pending_parent_keys: list[tuple[str, str | None]] = []
    for element in category_config.get("manual_elements", []):
        manual_qid = f"manual:{element['key']}"
        candidate = candidates.get(manual_qid)
        if candidate is None:
            candidate = Candidate(
                qid=manual_qid,
                name=element["name"],
                aliases=set(element.get("aliases", [])),
                is_playable_answer=bool(element.get("is_playable_answer", True)),
                sitelinks=9_500,
                key=element["key"],
            )
            candidates[manual_qid] = candidate
        else:
            candidate.aliases.update(element.get("aliases", []))
            candidate.is_playable_answer = bool(element.get("is_playable_answer", True))
            candidate.key = element["key"]
        manual_qids_by_key[element["key"]] = manual_qid
        pending_parent_keys.append((manual_qid, element.get("parent")))

    for manual_qid, parent_key in pending_parent_keys:
        if not parent_key:
            candidates[manual_qid].parent_qid = category_config["root_qid"]
            continue
        parent_qid = manual_qids_by_key.get(parent_key)
        if parent_qid is None:
            notes.append(f"Manual animal {candidates[manual_qid].name} could not find parent key {parent_key!r}; attached to Animal.")
            parent_qid = category_config["root_qid"]
        candidates[manual_qid].parent_qid = parent_qid

    if category_config.get("manual_elements"):
        notes.append("Added configured manual animal entries for common-name gameplay coverage.")


def fetch_countries(
    category_config: dict[str, Any],
    global_config: dict[str, Any],
) -> tuple[dict[str, Candidate], list[str]]:
    """Fetch current sovereign states as a flat game category."""

    sovereign_state_qid = category_config["sovereign_state_qid"]
    query = f"""
SELECT ?item ?itemLabel ?itemAltLabel WHERE {{
  ?item wdt:P31 wd:{sovereign_state_qid} .
  FILTER NOT EXISTS {{ ?item wdt:P576 ?dissolvedDate . }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en" .
  }}
}}
ORDER BY ?itemLabel
"""
    rows = sparql_query(query, global_config)
    notes: list[str] = []
    candidates = rows_to_candidates(rows, prefer_common_name=False, notes=notes)
    apply_qid_aliases(candidates, category_config.get("manual_aliases", {}))
    apply_denylist(candidates, category_config.get("denylist_qids", []), notes)
    return candidates, notes


def fetch_fruits(
    category_config: dict[str, Any],
    global_config: dict[str, Any],
) -> tuple[dict[str, Candidate], list[str]]:
    """Fetch fruit candidates and apply a culinary-fruit curation overlay.

    Wikidata's fruit modeling mixes botanical fruit, culinary fruit, food groups,
    and list/category pages. The query gives us a Wikidata-backed candidate pool,
    while required_names and manual_aliases_by_name keep the final game list
    focused on recognizable foods.
    """

    root_qid = category_config["root_qid"]
    max_candidates = int(category_config["max_candidates"])
    query = f"""
SELECT ?item ?itemLabel ?itemAltLabel ?sitelinks WHERE {{
  {{
    ?item wdt:P31/wdt:P279* wd:{root_qid} .
  }} UNION {{
    ?item wdt:P279* wd:{root_qid} .
  }}
  OPTIONAL {{ ?item wikibase:sitelinks ?sitelinks . }}
  SERVICE wikibase:label {{
    bd:serviceParam wikibase:language "en" .
  }}
}}
ORDER BY DESC(?sitelinks) ?itemLabel
LIMIT {max_candidates}
"""
    rows = sparql_query(query, global_config)
    notes: list[str] = []
    candidates = rows_to_candidates(rows, prefer_common_name=False, notes=notes)
    apply_denylist(candidates, category_config.get("denylist_qids", []), notes)

    # Fruits are deliberately curated by name. This keeps noisy Wikidata hits out
    # of the game while still letting fetched aliases/QIDs enrich matching when
    # the required fruit appears in the candidate pool.
    required_names = category_config.get("required_names", [])
    by_normalized_name = {normalize_text(candidate.name): candidate for candidate in candidates.values()}
    curated: dict[str, Candidate] = {}
    for name in required_names:
        normalized_name = normalize_text(name)
        candidate = by_normalized_name.get(normalized_name)
        if candidate is None:
            candidate = Candidate(qid="", name=name)
            notes.append(f"Manual fruit fallback used because Wikidata query did not return {name!r}.")
        curated[candidate.qid or f"manual:{normalized_name}"] = candidate

    apply_name_aliases(curated, category_config.get("manual_aliases_by_name", {}))
    return curated, notes


def rows_to_candidates(
    rows: list[dict[str, Any]],
    prefer_common_name: bool,
    notes: list[str],
) -> dict[str, Candidate]:
    """Collapse WDQS result rows into one candidate per QID."""

    candidates: dict[str, Candidate] = {}
    for row in rows:
        qid = qid_from_binding(row.get("item"))
        label = value_from_binding(row.get("itemLabel"))
        common_name = value_from_binding(row.get("commonName"))
        parent_qid = qid_from_binding(row.get("parent"))
        parent_label = value_from_binding(row.get("parentLabel"))
        alt_label = value_from_binding(row.get("itemAltLabel"))
        sitelinks = int(value_from_binding(row.get("sitelinks")) or 0)

        if not qid:
            notes.append("Skipped a Wikidata row without an item QID.")
            continue
        name = clean_name(common_name if prefer_common_name and common_name else label)
        if not name:
            notes.append(f"Skipped {qid} because it had no usable English label or common name.")
            continue

        candidate = candidates.get(qid)
        if candidate is None:
            candidate = Candidate(
                qid=qid,
                name=name,
                parent_qid=parent_qid,
                parent_label=parent_label,
                sitelinks=sitelinks,
            )
            candidates[qid] = candidate
        else:
            # Prefer a common name over a scientific-looking label when present,
            # but keep the old name as an alias so previous player inputs still
            # have a chance to validate after review.
            if prefer_common_name and common_name:
                previous_name = candidate.name
                candidate.name = name
                if previous_name != name:
                    candidate.aliases.add(previous_name)
            if candidate.parent_qid is None and parent_qid:
                candidate.parent_qid = parent_qid
                candidate.parent_label = parent_label
            candidate.sitelinks = max(candidate.sitelinks, sitelinks)

        if alt_label:
            candidate.aliases.add(clean_name(alt_label))
        if common_name and clean_name(common_name) != candidate.name:
            candidate.aliases.add(clean_name(common_name))
    return candidates


def qid_from_binding(binding: dict[str, Any] | None) -> str:
    """Extract a compact QID from a WDQS URI binding."""

    value = value_from_binding(binding)
    if not value:
        return ""
    return value.rsplit("/", 1)[-1]


def value_from_binding(binding: dict[str, Any] | None) -> str:
    """Return the plain string value from one WDQS JSON binding."""

    if not binding:
        return ""
    return str(binding.get("value", "")).strip()


def clean_name(value: str) -> str:
    """Clean display labels without applying game-input normalization."""

    return " ".join((value or "").replace("_", " ").split()).strip()


def apply_qid_aliases(candidates: dict[str, Candidate], aliases_by_qid: dict[str, list[str]]) -> None:
    """Merge configured aliases onto matching fetched QIDs."""

    for qid, aliases in aliases_by_qid.items():
        candidate = candidates.get(qid)
        if candidate is None:
            continue
        candidate.aliases.update(clean_name(alias) for alias in aliases if clean_name(alias))


def apply_name_aliases(candidates: dict[str, Candidate], aliases_by_name: dict[str, list[str]]) -> None:
    """Merge configured aliases by display name for curated flat categories."""

    by_name = {normalize_text(candidate.name): candidate for candidate in candidates.values()}
    for name, aliases in aliases_by_name.items():
        candidate = by_name.get(normalize_text(name))
        if candidate is None:
            continue
        candidate.aliases.update(clean_name(alias) for alias in aliases if clean_name(alias))


def apply_denylist(candidates: dict[str, Candidate], denylist_qids: list[str], notes: list[str]) -> None:
    """Remove configured QIDs from the candidate set and report the removal."""

    for qid in denylist_qids:
        removed = candidates.pop(qid, None)
        if removed is not None:
            notes.append(f"Denylisted {removed.name} ({qid}).")


def build_taxonomy_elements(
    candidates: dict[str, Candidate],
    category_config: dict[str, Any],
    notes: list[str],
) -> list[dict[str, Any]]:
    """Turn animal candidates into parent-linked seed elements."""

    root_qid = category_config["root_qid"]
    root_key = category_config["root_key"]
    root = candidates[root_qid]
    root.key = root_key

    included = choose_taxonomy_candidates(candidates, category_config)
    if root_qid not in included:
        included[root_qid] = root

    used_keys: set[str] = set()
    reserved_keys = {candidate.key for candidate in included.values() if candidate.key}
    for candidate in sorted(included.values(), key=lambda item: (item.qid != root_qid, item.name, item.qid)):
        if candidate.key:
            used_keys.add(candidate.key)
            continue
        candidate.key = unique_key(slug_key(candidate.name), used_keys | reserved_keys)
        used_keys.add(candidate.key)

    root.key = root_key
    used_keys.add(root_key)

    for candidate in included.values():
        if candidate.qid == root_qid:
            candidate.parent_key = None
            continue
        parent = nearest_included_parent(candidate, included, candidates)
        if parent is None:
            candidate.parent_key = root_key
            notes.append(f"Attached {candidate.name} ({candidate.qid}) to Animal because no included parent was found.")
        else:
            candidate.parent_key = parent.key

    ordered = sorted(
        included.values(),
        key=lambda candidate: (taxonomy_depth(candidate, included, candidates), candidate.name.lower(), candidate.qid),
    )
    return [candidate_to_element(candidate, include_parent=True) for candidate in ordered]


def choose_taxonomy_candidates(
    candidates: dict[str, Candidate],
    category_config: dict[str, Any],
) -> dict[str, Candidate]:
    """Select a capped animal taxonomy while preserving ancestor paths.

    The public Wikidata animal tree is huge. We rank candidates by sitelinks as a
    practical proxy for recognizability, then include ancestors for each selected
    item so replacement logic still has meaningful parent relationships.
    """

    root_qid = category_config["root_qid"]
    max_elements = int(category_config["max_elements"])
    ranked = sorted(
        (candidate for candidate in candidates.values() if candidate.qid != root_qid),
        key=lambda candidate: (-candidate.sitelinks, candidate.name.lower(), candidate.qid),
    )
    selected_qids: set[str] = {root_qid}
    for candidate in ranked:
        if len(selected_qids) >= max_elements:
            break
        selected_qids.add(candidate.qid)
        current = candidate
        while current.parent_qid and current.parent_qid in candidates:
            selected_qids.add(current.parent_qid)
            current = candidates[current.parent_qid]
            if current.qid == root_qid:
                break
            if len(selected_qids) >= max_elements:
                break
    selected = {qid: candidates[qid] for qid in selected_qids if qid in candidates}
    return dedupe_candidates_by_name(selected)


def dedupe_candidates_by_name(candidates: dict[str, Candidate]) -> dict[str, Candidate]:
    """Keep one candidate for each normalized display name.

    Wikidata can contain multiple taxa with the same English common name. The
    game matcher requires names to be unique within a category, so the refresh
    keeps the candidate with the strongest recognizability signal.
    """

    best_by_name: dict[str, Candidate] = {}
    for candidate in candidates.values():
        normalized_name = normalize_text(candidate.name)
        existing = best_by_name.get(normalized_name)
        if existing is None or candidate.sitelinks > existing.sitelinks:
            best_by_name[normalized_name] = candidate
    return {candidate.qid: candidate for candidate in best_by_name.values()}


def nearest_included_parent(
    candidate: Candidate,
    included: dict[str, Candidate],
    all_candidates: dict[str, Candidate],
) -> Candidate | None:
    """Find the closest selected ancestor for a taxonomy candidate."""

    seen: set[str] = set()
    parent_qid = candidate.parent_qid
    while parent_qid and parent_qid not in seen:
        seen.add(parent_qid)
        if parent_qid in included:
            return included[parent_qid]
        parent = all_candidates.get(parent_qid)
        if parent is None:
            return None
        parent_qid = parent.parent_qid
    return None


def taxonomy_depth(
    candidate: Candidate,
    included: dict[str, Candidate],
    all_candidates: dict[str, Candidate],
) -> int:
    """Return an approximate depth so parents are written before children."""

    depth = 0
    seen: set[str] = set()
    current = candidate
    while current.parent_qid and current.parent_qid not in seen:
        seen.add(current.parent_qid)
        depth += 1
        parent = included.get(current.parent_qid) or all_candidates.get(current.parent_qid)
        if parent is None:
            break
        current = parent
    return depth


def build_flat_elements(
    candidates: dict[str, Candidate],
    category_config: dict[str, Any],
    notes: list[str],
) -> list[dict[str, Any]]:
    """Turn candidates into a sorted flat category."""

    max_elements = category_config.get("max_elements")
    ranked = sorted(candidates.values(), key=lambda candidate: (-candidate.sitelinks, candidate.name.lower(), candidate.qid))
    if max_elements:
        ranked = ranked[: int(max_elements)]
    used_keys: set[str] = set()
    elements: list[dict[str, Any]] = []
    for candidate in sorted(ranked, key=lambda item: item.name.lower()):
        candidate.key = unique_key(slug_key(candidate.name), used_keys)
        elements.append(candidate_to_element(candidate, include_parent=False))
    if not elements:
        notes.append(f"No elements were generated for {category_config['slug']}.")
    return elements


def candidate_to_element(candidate: Candidate, include_parent: bool) -> dict[str, Any]:
    """Convert an internal candidate into the existing category JSON shape."""

    aliases = sorted(
        {
            alias
            for alias in candidate.aliases
            if alias and normalize_text(alias) != normalize_text(candidate.name)
        },
        key=lambda value: value.lower(),
    )
    element: dict[str, Any] = {
        "key": candidate.key,
        "name": candidate.name,
        "aliases": aliases,
        "is_playable_answer": candidate.is_playable_answer,
    }
    if include_parent:
        element["parent"] = candidate.parent_key
    if candidate.qid and not candidate.qid.startswith("manual:"):
        element["wikidata_qid"] = candidate.qid
    return element


def slug_key(value: str) -> str:
    """Create stable-ish element keys from display names using app normalization."""

    normalized = normalize_text(value)
    return normalized.replace(" ", "_") or "item"


def unique_key(base: str, used_keys: set[str]) -> str:
    """Avoid key collisions while keeping the first key human-readable."""

    candidate = base
    index = 2
    while candidate in used_keys:
        candidate = f"{base}_{index}"
        index += 1
    used_keys.add(candidate)
    return candidate


def validate_category(category_data: dict[str, Any]) -> list[str]:
    """Report collisions and parent issues before JSON is written."""

    notes: list[str] = []
    elements = category_data.get("elements", [])
    keys = {element["key"] for element in elements}
    seen_names: dict[str, str] = {}
    seen_aliases: dict[str, str] = {}

    for element in elements:
        normalized_name = normalize_text(element["name"])
        if normalized_name in seen_names:
            notes.append(f"Duplicate normalized name {normalized_name!r}: {seen_names[normalized_name]} and {element['key']}.")
        seen_names[normalized_name] = element["key"]

        singular_name = singularize_phrase(normalized_name)
        for alias in element.get("aliases", []):
            normalized_alias = normalize_text(alias)
            if not normalized_alias:
                notes.append(f"Blank alias found on {element['key']}.")
                continue
            if normalized_alias == normalized_name or normalized_alias == singular_name:
                notes.append(f"Redundant alias {alias!r} on {element['key']}.")
            owner = seen_aliases.get(normalized_alias)
            if owner and owner != element["key"]:
                notes.append(f"Alias collision {alias!r}: {owner} and {element['key']}.")
            seen_aliases[normalized_alias] = element["key"]

        parent = element.get("parent")
        if parent is not None and parent not in keys:
            notes.append(f"Missing parent {parent!r} for {element['key']}.")
    return notes


def load_previous_category(path: Path) -> dict[str, Any] | None:
    """Load the current seed file so the report can show review-friendly diffs."""

    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def render_report(
    slug: str,
    new_data: dict[str, Any],
    previous: dict[str, Any] | None,
    notes: list[str],
    category_config: dict[str, Any],
) -> str:
    """Create a concise Markdown report for human review."""

    new_elements = {element["key"]: element for element in new_data.get("elements", [])}
    old_elements = {element["key"]: element for element in (previous or {}).get("elements", [])}
    added = sorted(set(new_elements) - set(old_elements))
    removed = sorted(set(old_elements) - set(new_elements))
    changed = sorted(
        key
        for key in set(new_elements) & set(old_elements)
        if comparable_element(new_elements[key]) != comparable_element(old_elements[key])
    )
    large_diff_threshold = int(category_config.get("large_diff_threshold", 50))
    diff_size = len(added) + len(removed) + len(changed)
    review_notes = list(notes)
    if diff_size > large_diff_threshold:
        review_notes.append(
            f"Large diff: {diff_size} changed element(s), threshold is {large_diff_threshold}."
        )

    lines = [
        f"## {new_data['name']} ({slug})",
        "",
        f"- Fetched at: {new_data['last_fetched_at']}",
        f"- Elements: {len(new_elements)}",
        f"- Added: {len(added)}",
        f"- Removed: {len(removed)}",
        f"- Changed: {len(changed)}",
    ]
    if added:
        lines.append(f"- Added keys: {', '.join(added[:30])}{' ...' if len(added) > 30 else ''}")
    if removed:
        lines.append(f"- Removed keys: {', '.join(removed[:30])}{' ...' if len(removed) > 30 else ''}")
    if changed:
        lines.append(f"- Changed keys: {', '.join(changed[:30])}{' ...' if len(changed) > 30 else ''}")
    lines.append("")
    lines.append("### Review Notes")
    if review_notes:
        lines.extend(f"- {note}" for note in review_notes)
    else:
        lines.append("- No validation warnings.")
    return "\n".join(lines)


def comparable_element(element: dict[str, Any]) -> dict[str, Any]:
    """Strip volatile metadata before comparing old and new elements."""

    return {
        key: value
        for key, value in element.items()
        if key not in {"last_fetched_at"}
    }


def format_category_json(category_data: dict[str, Any]) -> str:
    """Format generated category JSON consistently for review diffs."""

    return json.dumps(category_data, indent=2, ensure_ascii=False) + "\n"


if __name__ == "__main__":
    raise SystemExit(main())
