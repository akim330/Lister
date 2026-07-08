from __future__ import annotations

from datetime import datetime, timezone
import sqlite3

from flask import current_app

from ..db import utc_now_iso
from .answer_validation import match_answer
from .game_modes import mode_for_category, require_game_mode
from .normalization import normalize_text
from .taxonomy import get_parent_map, is_ancestor, is_descendant


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return datetime.fromisoformat(value)


def elapsed_seconds(game: sqlite3.Row) -> int:
    """Return the number of seconds a session has been running or had run.

    Target-list leaderboards rank by completion speed, and the play UI needs a
    count-up clock. Storing the final elapsed value on end keeps finished games
    stable while active games can still be calculated from ``started_at``.
    """

    if game["elapsed_seconds"] is not None:
        return int(game["elapsed_seconds"])
    started = _parse_iso(game["started_at"])
    if started is None:
        return 0
    ended = _parse_iso(game["ended_at"]) or datetime.now(timezone.utc)
    return max(0, int(round((ended - started).total_seconds())))


def remaining_seconds(db: sqlite3.Connection, game: sqlite3.Row) -> int | None:
    """Return countdown time left, or None for modes that do not count down."""

    mode = require_game_mode(game["game_mode"])
    if mode.timer_kind != "countdown":
        return None
    starting_seconds = int(mode.starting_seconds or 0)
    if game["status"] != "active" or not game["started_at"]:
        return starting_seconds
    started = _parse_iso(game["started_at"])
    if started is None:
        return starting_seconds
    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    allowed = starting_seconds + int(game["seconds_awarded"])
    return max(0, int(round(allowed - elapsed)))


def category_mode_for_game(db: sqlite3.Connection, game: sqlite3.Row):
    """Return this game's mode with any category-specific target applied.

    Game sessions store only ``category_id`` and the stable mode slug. Looking
    up the category slug here gives gameplay and routes one shared path for
    applying target overrides, so a category like US States can behave as
    ``Name 50`` without creating a separate persisted game mode.
    """

    category = db.execute("SELECT slug FROM categories WHERE id = ?", (int(game["category_id"]),)).fetchone()
    if not category:
        current_mode = require_game_mode(game["game_mode"])
        # A game without its category row would indicate a broken local
        # database relationship. Flask's logger is the backend equivalent used
        # in this app for surfacing unexpected impossible states.
        current_app.logger.error("Expected category %s for game %s.", game["category_id"], game["id"])
        return current_mode
    return mode_for_category(require_game_mode(game["game_mode"]), category["slug"])


def can_submit_to_leaderboard(db: sqlite3.Connection, game: sqlite3.Row) -> bool:
    """Return whether this ended game has a valid score for its mode board."""

    mode = category_mode_for_game(db, game)
    if game["status"] != "ended" or bool(game["submitted_to_leaderboard"]):
        return False
    if mode.target_score is not None:
        return int(game["score"]) >= mode.target_score and game["elapsed_seconds"] is not None
    return True


def active_answers(db: sqlite3.Connection, game_id: str) -> list[sqlite3.Row]:
    return db.execute(
        """
        SELECT ga.*, ce.canonical_name
        FROM game_answers ga
        JOIN category_elements ce ON ce.id = ga.element_id
        WHERE ga.game_session_id = ? AND ga.status = 'accepted'
        ORDER BY ga.created_at ASC, ga.id ASC
        """,
        (game_id,),
    ).fetchall()


def final_list_answers(db: sqlite3.Connection, game_id: str) -> list[dict]:
    """Return the saved answers that make up a finished list.

    Every submitted answer is persisted in ``game_answers`` as play happens, so
    the final list does not need a separate snapshot table. Reading only rows
    that are still ``accepted`` gives results pages and shared leaderboard links
    the exact list that counted toward the player's final score, excluding
    invalid guesses, duplicates, and broader answers later replaced by a more
    specific one.
    """

    rows = active_answers(db, game_id)
    return [
        {
            "id": int(row["id"]),
            "name": row["canonical_name"],
            "submitted_text": row["submitted_text"],
        }
        for row in rows
    ]


def all_display_answers(db: sqlite3.Connection, game_id: str) -> list[dict]:
    rows = db.execute(
        """
        SELECT ga.*, ce.canonical_name,
               repl_ce.canonical_name AS replaced_by_name
        FROM game_answers ga
        LEFT JOIN category_elements ce ON ce.id = ga.element_id
        LEFT JOIN game_answers repl ON repl.id = ga.replaced_answer_id
        LEFT JOIN category_elements repl_ce ON repl_ce.id = repl.element_id
        WHERE ga.game_session_id = ?
          AND ga.status IN ('accepted', 'replaced')
        ORDER BY ga.created_at ASC, ga.id ASC
        """,
        (game_id,),
    ).fetchall()
    return [
        {
            "id": row["id"],
            "name": row["canonical_name"],
            "status": row["status"],
            "replaced_by": row["replaced_by_name"],
        }
        for row in rows
    ]


def start_game(db: sqlite3.Connection, game_id: str) -> sqlite3.Row | None:
    game = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
    if not game or game["status"] != "created":
        return game
    now = utc_now_iso()
    db.execute(
        "UPDATE game_sessions SET status = 'active', started_at = ?, updated_at = ? WHERE id = ?",
        (now, now, game_id),
    )
    db.commit()
    return db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()


def end_game(db: sqlite3.Connection, game_id: str, reason: str) -> sqlite3.Row | None:
    if reason not in {"timeout", "stopped"}:
        reason = "timeout"
    game = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return None
    if game["status"] == "ended":
        return game
    now = utc_now_iso()
    final_elapsed_seconds = elapsed_seconds(game)
    db.execute(
        """
        UPDATE game_sessions
        SET status = 'ended', ended_at = ?, end_reason = ?, elapsed_seconds = ?, updated_at = ?
        WHERE id = ?
        """,
        (now, reason, final_elapsed_seconds, now, game_id),
    )
    db.commit()
    return db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()


def submit_answer(db: sqlite3.Connection, game_id: str, submitted_text: str) -> dict:
    game = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return {"status": "invalid", "message": "Game not found."}
    if game["status"] != "active":
        return {"status": "invalid", "message": "This game is not active."}

    mode = category_mode_for_game(db, game)
    remaining = remaining_seconds(db, game)
    if remaining is not None and remaining <= 0:
        end_game(db, game_id, "timeout")
        return {"status": "too_late", "message": "Time is up.", "remaining_seconds": 0}

    category_id = int(game["category_id"])
    match = match_answer(db, category_id, submitted_text)
    normalized = match.normalized_text or normalize_text(submitted_text)
    now = utc_now_iso()

    if match.status in {"invalid", "ambiguous"} or match.element_id is None:
        status = "ambiguous" if match.status == "ambiguous" else "invalid"
        db.execute(
            """
            INSERT INTO game_answers
                (game_session_id, submitted_text, normalized_text, element_id, status,
                 score_delta, time_delta, message, created_at)
            VALUES (?, ?, ?, NULL, ?, 0, 0, ?, ?)
            """,
            (game_id, submitted_text, normalized, status, match.message, now),
        )
        db.commit()
        return {
            "status": status,
            "message": match.message,
            "current_score": int(game["score"]),
            "remaining_seconds": remaining_seconds(db, game),
            "elapsed_seconds": elapsed_seconds(game),
            "accepted_answers": all_display_answers(db, game_id),
        }

    element_id = int(match.element_id)
    canonical_name = match.canonical_name or "Answer"
    active = active_answers(db, game_id)
    parent_map = get_parent_map(db, category_id)

    for row in active:
        if int(row["element_id"]) == element_id:
            message = "You already listed that."
            db.execute(
                """
                INSERT INTO game_answers
                    (game_session_id, submitted_text, normalized_text, element_id, status,
                     score_delta, time_delta, message, created_at)
                VALUES (?, ?, ?, ?, 'duplicate', 0, 0, ?, ?)
                """,
                (game_id, submitted_text, normalized, element_id, message, now),
            )
            db.commit()
            return {
                "status": "duplicate",
                "message": message,
                "current_score": int(game["score"]),
                "remaining_seconds": remaining_seconds(db, game),
                "elapsed_seconds": elapsed_seconds(game),
                "accepted_answers": all_display_answers(db, game_id),
            }

    for row in active:
        active_element_id = int(row["element_id"])
        if is_descendant(active_element_id, element_id, parent_map):
            message = f"Already covered by your more specific answer: {row['canonical_name']}."
            db.execute(
                """
                INSERT INTO game_answers
                    (game_session_id, submitted_text, normalized_text, element_id, status,
                     score_delta, time_delta, message, created_at)
                VALUES (?, ?, ?, ?, 'covered', 0, 0, ?, ?)
                """,
                (game_id, submitted_text, normalized, element_id, message, now),
            )
            db.commit()
            return {
                "status": "covered",
                "message": message,
                "current_score": int(game["score"]),
                "remaining_seconds": remaining_seconds(db, game),
                "elapsed_seconds": elapsed_seconds(game),
                "accepted_answers": all_display_answers(db, game_id),
            }

    ancestors_to_replace = [row for row in active if is_ancestor(int(row["element_id"]), element_id, parent_map)]

    if ancestors_to_replace:
        replaced_names = ", ".join(row["canonical_name"] for row in ancestors_to_replace)
        cur = db.execute(
            """
            INSERT INTO game_answers
                (game_session_id, submitted_text, normalized_text, element_id, status,
                 score_delta, time_delta, message, created_at)
            VALUES (?, ?, ?, ?, 'accepted', 0, 0, ?, ?)
            """,
            (
                game_id,
                submitted_text,
                normalized,
                element_id,
                f"Replaced {replaced_names} with more specific answer: {canonical_name}.",
                now,
            ),
        )
        new_answer_id = cur.lastrowid
        for row in ancestors_to_replace:
            db.execute(
                "UPDATE game_answers SET status = 'replaced', replaced_answer_id = ? WHERE id = ?",
                (new_answer_id, int(row["id"])),
            )
        db.commit()
        refreshed = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
        return {
            "status": "replaced",
            "canonical_name": canonical_name,
            "replaced": replaced_names,
            "score_delta": 0,
            "time_delta": 0,
            "message": f"Replaced {replaced_names} with more specific answer: {canonical_name}.",
            "current_score": int(refreshed["score"]),
            "remaining_seconds": remaining_seconds(db, refreshed),
            "elapsed_seconds": elapsed_seconds(refreshed),
            "accepted_answers": all_display_answers(db, game_id),
        }

    # Only Survival extends the clock on accepted answers. Other modes still
    # record a zero time_delta so the answer history accurately reflects that
    # the score changed without changing the timer.
    time_delta = int(mode.correct_answer_seconds)
    message = match.message or "Accepted."
    if match.matched_from and message == "Accepted.":
        message = f"Accepted as {canonical_name}."

    db.execute(
        """
        INSERT INTO game_answers
            (game_session_id, submitted_text, normalized_text, element_id, status,
             score_delta, time_delta, message, created_at)
        VALUES (?, ?, ?, ?, 'accepted', 1, ?, ?, ?)
        """,
        (game_id, submitted_text, normalized, element_id, time_delta, message, now),
    )
    db.execute(
        """
        UPDATE game_sessions
        SET score = score + 1,
            seconds_awarded = seconds_awarded + ?,
            updated_at = ?
        WHERE id = ?
        """,
        (time_delta, now, game_id),
    )
    db.commit()
    refreshed = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
    result = {
        "status": "accepted",
        "canonical_name": canonical_name,
        "score_delta": 1,
        "time_delta": time_delta,
        "message": message,
        "current_score": int(refreshed["score"]),
        "remaining_seconds": remaining_seconds(db, refreshed),
        "elapsed_seconds": elapsed_seconds(refreshed),
        "accepted_answers": all_display_answers(db, game_id),
    }
    if mode.target_score is not None and int(refreshed["score"]) >= mode.target_score:
        completed = end_game(db, game_id, "stopped")
        if completed:
            result["status"] = "completed"
            result["message"] = f"Completed {mode.name} in {elapsed_seconds(completed)} seconds."
            result["game_ended"] = True
            result["elapsed_seconds"] = elapsed_seconds(completed)
            result["results_url"] = f"/results/{game_id}"
    return result
