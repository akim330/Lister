from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from flask import current_app, g


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def normalize_username_for_lookup(username: str | None) -> str:
    """Return the stable lookup key used for exact-username friend requests.

    Friend requests are addressed by a player's displayed username instead of a
    separate account handle. Collapsing surrounding and repeated whitespace and
    comparing case-insensitively keeps "Ada", " ada ", and "ADA" from becoming
    different request targets while still preserving the display casing the
    player chose.
    """

    return " ".join((username or "").strip().lower().split())


def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(current_app.config["DATABASE"])
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        g.db = conn
    return g.db


def close_db(_: Exception | None = None) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_uuid TEXT NOT NULL UNIQUE,
    current_display_name TEXT NOT NULL,
    normalized_username TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    mode TEXT NOT NULL CHECK (mode IN ('flat', 'taxonomy')),
    bucket TEXT NOT NULL DEFAULT 'General',
    bucket_order INTEGER NOT NULL DEFAULT 999,
    display_order INTEGER NOT NULL DEFAULT 999,
    is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS category_elements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    wiki_entity_id INTEGER,
    wiki_qid TEXT,
    element_key TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    parent_id INTEGER,
    is_playable_answer INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(category_id, element_key),
    UNIQUE(category_id, normalized_name),
    UNIQUE(category_id, wiki_entity_id),
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE,
    FOREIGN KEY(parent_id) REFERENCES category_elements(id) ON DELETE SET NULL,
    FOREIGN KEY(wiki_entity_id) REFERENCES wiki_entities(id)
);

CREATE INDEX IF NOT EXISTS idx_category_elements_category_id ON category_elements(category_id);
CREATE INDEX IF NOT EXISTS idx_category_elements_parent_id ON category_elements(parent_id);

CREATE TABLE IF NOT EXISTS element_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    element_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(element_id, normalized_alias),
    FOREIGN KEY(element_id) REFERENCES category_elements(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_element_aliases_normalized ON element_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS category_manual_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    lookup_text TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(category_id, normalized_alias, lookup_text),
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_category_manual_aliases_normalized ON category_manual_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS wiki_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    qid TEXT NOT NULL UNIQUE,
    page_id INTEGER,
    page_title TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    description TEXT,
    sitelinks INTEGER NOT NULL DEFAULT 0,
    claims_complete INTEGER NOT NULL DEFAULT 1,
    fetched_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_wiki_entities_page_title ON wiki_entities(page_title);

CREATE TABLE IF NOT EXISTS wiki_entity_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_entity_id INTEGER NOT NULL,
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(wiki_entity_id, normalized_alias),
    FOREIGN KEY(wiki_entity_id) REFERENCES wiki_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wiki_entity_aliases_normalized ON wiki_entity_aliases(normalized_alias);

CREATE TABLE IF NOT EXISTS wiki_entity_claims (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_entity_id INTEGER NOT NULL,
    property_id TEXT NOT NULL,
    value_qid TEXT NOT NULL,
    value_label TEXT,
    rank TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(wiki_entity_id, property_id, value_qid),
    FOREIGN KEY(wiki_entity_id) REFERENCES wiki_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wiki_entity_claims_entity_property ON wiki_entity_claims(wiki_entity_id, property_id);
CREATE INDEX IF NOT EXISTS idx_wiki_entity_claims_value ON wiki_entity_claims(value_qid);

CREATE TABLE IF NOT EXISTS wiki_entity_categories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wiki_entity_id INTEGER NOT NULL,
    category_title TEXT NOT NULL,
    normalized_category TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(wiki_entity_id, normalized_category),
    FOREIGN KEY(wiki_entity_id) REFERENCES wiki_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wiki_entity_categories_entity ON wiki_entity_categories(wiki_entity_id);

CREATE TABLE IF NOT EXISTS category_entity_memberships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    wiki_entity_id INTEGER NOT NULL,
    is_member INTEGER NOT NULL,
    reason TEXT NOT NULL,
    checked_at TEXT NOT NULL,
    UNIQUE(category_id, wiki_entity_id),
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE,
    FOREIGN KEY(wiki_entity_id) REFERENCES wiki_entities(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_category_entity_memberships_category ON category_entity_memberships(category_id);

CREATE TABLE IF NOT EXISTS wiki_verification_failures (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    category_id INTEGER NOT NULL,
    normalized_text TEXT NOT NULL,
    message TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(category_id, normalized_text),
    FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_wiki_verification_failures_expires
ON wiki_verification_failures(expires_at);

CREATE TABLE IF NOT EXISTS game_sessions (
    id TEXT PRIMARY KEY,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    game_mode TEXT NOT NULL DEFAULT 'survival',
    status TEXT NOT NULL CHECK (status IN ('created', 'active', 'ended')),
    started_at TEXT,
    ended_at TEXT,
    end_reason TEXT CHECK (end_reason IS NULL OR end_reason IN ('timeout', 'stopped')),
    score INTEGER NOT NULL DEFAULT 0,
    seconds_awarded INTEGER NOT NULL DEFAULT 0,
    elapsed_seconds INTEGER,
    submitted_to_leaderboard INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(category_id) REFERENCES categories(id)
);

CREATE INDEX IF NOT EXISTS idx_game_sessions_user_id ON game_sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_game_sessions_category_id ON game_sessions(category_id);

CREATE TABLE IF NOT EXISTS game_answers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    game_session_id TEXT NOT NULL,
    submitted_text TEXT NOT NULL,
    normalized_text TEXT NOT NULL,
    element_id INTEGER,
    status TEXT NOT NULL CHECK (status IN ('accepted', 'replaced', 'duplicate', 'covered', 'invalid', 'ambiguous', 'too_late')),
    replaced_answer_id INTEGER,
    score_delta INTEGER NOT NULL DEFAULT 0,
    time_delta INTEGER NOT NULL DEFAULT 0,
    message TEXT NOT NULL,
    created_at TEXT NOT NULL,
    FOREIGN KEY(game_session_id) REFERENCES game_sessions(id) ON DELETE CASCADE,
    FOREIGN KEY(element_id) REFERENCES category_elements(id),
    FOREIGN KEY(replaced_answer_id) REFERENCES game_answers(id)
);

CREATE INDEX IF NOT EXISTS idx_game_answers_session_id ON game_answers(game_session_id);
CREATE INDEX IF NOT EXISTS idx_game_answers_element_id ON game_answers(element_id);

CREATE TABLE IF NOT EXISTS leaderboard_entries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    category_id INTEGER NOT NULL,
    game_mode TEXT NOT NULL DEFAULT 'survival',
    game_session_id TEXT NOT NULL UNIQUE,
    score INTEGER NOT NULL,
    elapsed_seconds INTEGER,
    created_at TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id),
    FOREIGN KEY(category_id) REFERENCES categories(id),
    FOREIGN KEY(game_session_id) REFERENCES game_sessions(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_leaderboard_category_score ON leaderboard_entries(category_id, score DESC);

CREATE TABLE IF NOT EXISTS friend_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requester_id INTEGER NOT NULL,
    recipient_id INTEGER NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'canceled')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(requester_id, recipient_id),
    CHECK(requester_id != recipient_id),
    FOREIGN KEY(requester_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_friend_requests_recipient_status
ON friend_requests(recipient_id, status);

CREATE INDEX IF NOT EXISTS idx_friend_requests_requester_status
ON friend_requests(requester_id, status);

CREATE TABLE IF NOT EXISTS friendships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_low_id INTEGER NOT NULL,
    user_high_id INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(user_low_id, user_high_id),
    CHECK(user_low_id < user_high_id),
    FOREIGN KEY(user_low_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY(user_high_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_friendships_high_user ON friendships(user_high_id);
"""


def init_db() -> None:
    db = get_db()
    db.executescript(SCHEMA_SQL)
    _ensure_user_lookup_column(db)
    _ensure_category_bucket_column(db)
    _ensure_live_wiki_columns(db)
    _ensure_wiki_verification_failure_table(db)
    _ensure_game_mode_columns(db)
    _ensure_friend_tables(db)
    db.commit()
    current_app.teardown_appcontext(close_db)


def _ensure_user_lookup_column(db: sqlite3.Connection) -> None:
    """Backfill unique username lookup keys for existing development users.

    New databases get ``normalized_username`` from ``SCHEMA_SQL``. Older local
    databases may already contain duplicate display names because usernames were
    previously only cosmetic. Since friend requests now resolve by exact
    username, this migration keeps the earliest row's display name and suffixes
    later duplicates before creating the unique lookup index.
    """

    existing = {
        row["name"]
        for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "normalized_username" not in existing:
        db.execute("ALTER TABLE users ADD COLUMN normalized_username TEXT")

    seen: set[str] = set()
    users = db.execute(
        "SELECT id, current_display_name FROM users ORDER BY id ASC"
    ).fetchall()
    for user in users:
        display_name = _unique_development_display_name(user["current_display_name"], seen)
        normalized = normalize_username_for_lookup(display_name)
        seen.add(normalized)
        db.execute(
            """
            UPDATE users
            SET current_display_name = ?, normalized_username = ?, updated_at = ?
            WHERE id = ?
            """,
            (display_name, normalized, utc_now_iso(), int(user["id"])),
        )

    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_normalized_username "
        "ON users(normalized_username)"
    )


def _unique_development_display_name(display_name: str | None, seen: set[str]) -> str:
    """Return a unique display name while preserving existing data where possible.

    This is intentionally a development-data repair helper rather than a public
    username policy. It only runs during schema initialization for older local
    databases, and it suffixes duplicate names deterministically so friend
    lookup can safely enforce one row per normalized username.
    """

    base = (display_name or "").strip() or "Player"
    base = base[:40]
    candidate = base
    suffix = 2
    while normalize_username_for_lookup(candidate) in seen:
        suffix_text = f" {suffix}"
        candidate = f"{base[:40 - len(suffix_text)]}{suffix_text}"
        suffix += 1
    return candidate


def _ensure_friend_tables(db: sqlite3.Connection) -> None:
    """Create friend request and accepted-friendship tables for older databases.

    ``friend_requests`` keeps the directional request history so the UI can show
    incoming and outgoing pending requests. ``friendships`` stores accepted
    relationships as a sorted user-id pair, which makes the relationship
    symmetric without requiring duplicate rows.
    """

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS friend_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            requester_id INTEGER NOT NULL,
            recipient_id INTEGER NOT NULL,
            status TEXT NOT NULL CHECK (status IN ('pending', 'accepted', 'declined', 'canceled')),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(requester_id, recipient_id),
            CHECK(requester_id != recipient_id),
            FOREIGN KEY(requester_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(recipient_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_friend_requests_recipient_status "
        "ON friend_requests(recipient_id, status)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_friend_requests_requester_status "
        "ON friend_requests(requester_id, status)"
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS friendships (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_low_id INTEGER NOT NULL,
            user_high_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(user_low_id, user_high_id),
            CHECK(user_low_id < user_high_id),
            FOREIGN KEY(user_low_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(user_high_id) REFERENCES users(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_friendships_high_user ON friendships(user_high_id)"
    )


def _ensure_category_bucket_column(db: sqlite3.Connection) -> None:
    """Add theme-bucket metadata to older local category tables.

    Fresh databases receive these columns from ``SCHEMA_SQL``. Existing
    development databases need the additive columns before category JSON
    synchronization can write the requested bucket names and stable display
    ordering for the grouped home-page category picker.
    """

    existing = {
        row["name"]
        for row in db.execute("PRAGMA table_info(categories)").fetchall()
    }
    if "bucket" not in existing:
        db.execute("ALTER TABLE categories ADD COLUMN bucket TEXT NOT NULL DEFAULT 'General'")
    if "bucket_order" not in existing:
        db.execute("ALTER TABLE categories ADD COLUMN bucket_order INTEGER NOT NULL DEFAULT 999")
    if "display_order" not in existing:
        db.execute("ALTER TABLE categories ADD COLUMN display_order INTEGER NOT NULL DEFAULT 999")


def _ensure_live_wiki_columns(db: sqlite3.Connection) -> None:
    """Add live-wiki columns to existing development databases.

    The project does not need save-file compatibility, but this lightweight
    migration keeps a previously-created local SQLite file usable after the new
    schema is introduced. Fresh databases receive these columns from
    ``SCHEMA_SQL`` above, while older databases receive them here.
    """

    existing = {
        row["name"]
        for row in db.execute("PRAGMA table_info(category_elements)").fetchall()
    }
    if "wiki_entity_id" not in existing:
        db.execute("ALTER TABLE category_elements ADD COLUMN wiki_entity_id INTEGER")
    if "wiki_qid" not in existing:
        db.execute("ALTER TABLE category_elements ADD COLUMN wiki_qid TEXT")
    entity_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(wiki_entities)").fetchall()
    }
    if "claims_complete" not in entity_columns:
        db.execute("ALTER TABLE wiki_entities ADD COLUMN claims_complete INTEGER NOT NULL DEFAULT 1")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_category_elements_category_wiki_entity "
        "ON category_elements(category_id, wiki_entity_id)"
    )


def _ensure_wiki_verification_failure_table(db: sqlite3.Connection) -> None:
    """Create the short-lived wiki failure cache for existing databases.

    The table is deliberately tiny and stores only normalized answer text plus
    an expiry. It lets separate app workers agree that a recent answer was
    temporarily unverifiable without preserving that failure forever.
    """

    db.execute(
        """
        CREATE TABLE IF NOT EXISTS wiki_verification_failures (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            normalized_text TEXT NOT NULL,
            message TEXT NOT NULL,
            expires_at TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(category_id, normalized_text),
            FOREIGN KEY(category_id) REFERENCES categories(id) ON DELETE CASCADE
        )
        """
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_wiki_verification_failures_expires "
        "ON wiki_verification_failures(expires_at)"
    )


def _ensure_game_mode_columns(db: sqlite3.Connection) -> None:
    """Add game-mode tracking columns to older local SQLite databases.

    The current project does not need production-style save compatibility, but
    these additive migrations keep the existing development database usable
    after the game-mode schema lands. Older sessions and leaderboard rows are
    treated as Survival because that was the only ruleset before this change.
    """

    session_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(game_sessions)").fetchall()
    }
    if "game_mode" not in session_columns:
        db.execute("ALTER TABLE game_sessions ADD COLUMN game_mode TEXT NOT NULL DEFAULT 'survival'")
    if "elapsed_seconds" not in session_columns:
        db.execute("ALTER TABLE game_sessions ADD COLUMN elapsed_seconds INTEGER")

    leaderboard_columns = {
        row["name"]
        for row in db.execute("PRAGMA table_info(leaderboard_entries)").fetchall()
    }
    if "game_mode" not in leaderboard_columns:
        db.execute("ALTER TABLE leaderboard_entries ADD COLUMN game_mode TEXT NOT NULL DEFAULT 'survival'")
    if "elapsed_seconds" not in leaderboard_columns:
        db.execute("ALTER TABLE leaderboard_entries ADD COLUMN elapsed_seconds INTEGER")

    db.execute(
        "CREATE INDEX IF NOT EXISTS idx_leaderboard_mode_category_score "
        "ON leaderboard_entries(game_mode, category_id, score DESC)"
    )


def seed_categories_if_needed() -> None:
    db = get_db()
    data_dir = Path(current_app.root_path) / "data" / "categories"
    seen_slugs: set[str] = set()
    for path in sorted(data_dir.glob("*.json")):
        data = seed_category(path)
        seen_slugs.add(data["slug"])

    # Category JSON files are the development source of truth for which
    # categories should be available from the home page and random play. We
    # deactivate categories whose files were removed instead of deleting their
    # rows so any local in-progress sessions or leaderboard rows keep valid
    # foreign keys while disappearing from active category selection.
    if seen_slugs:
        placeholders = ", ".join("?" for _ in seen_slugs)
        db.execute(
            f"UPDATE categories SET is_active = 0, updated_at = ? WHERE slug NOT IN ({placeholders})",
            (utc_now_iso(), *sorted(seen_slugs)),
        )
    db.commit()


def seed_category(path: Path) -> dict[str, Any]:
    db = get_db()
    now = utc_now_iso()
    data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))

    db.execute(
        """
        INSERT INTO categories
            (slug, name, mode, bucket, bucket_order, display_order, is_active, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        ON CONFLICT(slug) DO UPDATE SET
            name = excluded.name,
            mode = excluded.mode,
            bucket = excluded.bucket,
            bucket_order = excluded.bucket_order,
            display_order = excluded.display_order,
            is_active = 1,
            updated_at = excluded.updated_at
        """,
        (
            data["slug"],
            data["name"],
            data.get("mode", "flat"),
            data.get("bucket", "General"),
            int(data.get("bucket_order", 999)),
            int(data.get("display_order", 999)),
            now,
            now,
        ),
    )
    # SQLite reports the most recent inserted row for the connection, which can
    # be stale when an UPSERT updates an existing category. Reading the row back
    # by slug guarantees that category-specific alias hints attach to the
    # category that was just synchronized from this JSON file.
    row = db.execute("SELECT id FROM categories WHERE slug = ?", (data["slug"],)).fetchone()
    if not row:
        current_app.logger.error("Expected category %r to exist after seeding.", data["slug"])
        return data
    category_id = int(row["id"])

    seed_manual_aliases_for_category(category_id, data)
    return data


def seed_manual_aliases_if_needed() -> None:
    """Backfill manual alias hints when an existing database already has categories.

    Category JSON files used to preload answers before live verification. They
    now provide only category metadata plus human-authored aliases such as
    "USA", "UK", and two-letter state abbreviations. This function imports those
    alias hints exactly once for databases created before the new cache tables.
    """

    db = get_db()
    alias_count = db.execute("SELECT COUNT(*) FROM category_manual_aliases").fetchone()[0]
    if alias_count > 0:
        return
    data_dir = Path(current_app.root_path) / "data" / "categories"
    for path in sorted(data_dir.glob("*.json")):
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        category = db.execute("SELECT id FROM categories WHERE slug = ?", (data["slug"],)).fetchone()
        if category:
            seed_manual_aliases_for_category(int(category["id"]), data)
    db.commit()


def seed_manual_aliases_for_category(category_id: int, data: dict[str, Any]) -> None:
    """Store aliases that should resolve to a specific Wikipedia lookup.

    These rows are not accepted answers by themselves. They only rewrite terse or
    culturally common inputs into a better lookup string before the Wikipedia
    and Wikidata verification path runs.
    """

    from .services.normalization import normalize_text

    db = get_db()
    now = utc_now_iso()
    # Treat category JSON as the source of truth for manual lookup hints. This
    # prevents removed typo aliases from lingering forever in an existing SQLite
    # database and silently bypassing spelling guards.
    db.execute("DELETE FROM category_manual_aliases WHERE category_id = ?", (category_id,))
    for element in data.get("elements", []):
        lookup_text = element["name"]
        normalized_name = normalize_text(lookup_text)
        for alias in element.get("aliases", []):
            normalized_alias = normalize_text(alias)
            if not normalized_alias or normalized_alias == normalized_name:
                continue
            db.execute(
                """
                INSERT OR IGNORE INTO category_manual_aliases
                    (category_id, alias, normalized_alias, lookup_text, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (category_id, alias, normalized_alias, lookup_text, now),
            )
