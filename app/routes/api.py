from __future__ import annotations

import uuid
import sqlite3

from flask import Blueprint, current_app, jsonify, request

from ..db import get_db, normalize_username_for_lookup, utc_now_iso
from ..services.game_modes import get_game_mode, mode_for_category, require_game_mode
from ..services.gameplay import (
    can_submit_to_leaderboard,
    category_mode_for_game,
    elapsed_seconds,
    end_game,
    remaining_seconds,
    start_game,
    submit_answer,
    final_list_answers,
)
from ..services.leaderboard import top_leaderboard

bp = Blueprint("api", __name__)


class UsernameTakenError(ValueError):
    """Raised when a browser-local user tries to claim another user's name."""


def _json() -> dict:
    if request.is_json:
        return request.get_json(silent=True) or {}
    return {}


def _clean_username(username: str | None) -> str:
    username = (username or "").strip()
    if not username:
        username = "Player"
    return username[:40]


def ensure_user(client_uuid: str | None, username: str | None):
    db = get_db()
    now = utc_now_iso()
    if not client_uuid:
        client_uuid = str(uuid.uuid4())
    username = _clean_username(username)
    normalized_username = normalize_username_for_lookup(username)
    user = db.execute("SELECT * FROM users WHERE client_uuid = ?", (client_uuid,)).fetchone()
    owner = db.execute(
        "SELECT * FROM users WHERE normalized_username = ?",
        (normalized_username,),
    ).fetchone()
    if owner and not user:
        # Browser-local UUIDs can disappear when localStorage is cleared or a
        # player opens the app in a fresh browser profile. Because this project
        # does not have real accounts yet, let an otherwise unknown browser
        # recover the existing local user by typing that exact username, then
        # return the stored UUID so future requests use the same identity.
        return owner
    if owner and int(owner["id"]) != int(user["id"]):
        raise UsernameTakenError("That username is already taken.")
    if user:
        db.execute(
            """
            UPDATE users
            SET current_display_name = ?, normalized_username = ?, updated_at = ?
            WHERE id = ?
            """,
            (username, normalized_username, now, int(user["id"])),
        )
        db.commit()
        return db.execute("SELECT * FROM users WHERE id = ?", (int(user["id"]),)).fetchone()
    cur = db.execute(
        """
        INSERT INTO users
            (client_uuid, current_display_name, normalized_username, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (client_uuid, username, normalized_username, now, now),
    )
    db.commit()
    return db.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()


def _user_identity_payload(user) -> dict:
    """Return the small public user shape shared by friends endpoints."""

    return {
        "id": int(user["id"]),
        "username": user["current_display_name"],
    }


def _current_user_from_payload(data: dict):
    """Resolve the browser-local identity sent by the friends UI.

    The app does not have login accounts yet, so each API call carries the
    browser's client UUID and current display name. Calling ``ensure_user`` here
    keeps the Friends page usable before the player has started a game, while
    still enforcing the unique username rule needed for exact-name requests.
    """

    client_uuid = data.get("client_uuid")
    if not client_uuid:
        return None
    return ensure_user(client_uuid, data.get("username"))


def _friendship_pair(user_id: int, other_user_id: int) -> tuple[int, int]:
    """Return the sorted friendship pair used by the symmetric table."""

    return (user_id, other_user_id) if user_id < other_user_id else (other_user_id, user_id)


def _are_friends(db: sqlite3.Connection, user_id: int, other_user_id: int) -> bool:
    low_id, high_id = _friendship_pair(user_id, other_user_id)
    return db.execute(
        "SELECT 1 FROM friendships WHERE user_low_id = ? AND user_high_id = ?",
        (low_id, high_id),
    ).fetchone() is not None


def _best_runs(
    db: sqlite3.Connection,
    user_ids: list[int],
    category_id: int | None = None,
) -> dict[tuple[int, int], sqlite3.Row]:
    """Return each user's best ended run for each category.

    Friends compare high-score lists rather than full histories. This helper is
    the single selection rule for both comparison modes: highest score wins,
    shortest elapsed time breaks score ties, and the newest ended run wins any
    remaining tie. The result is keyed by ``(user_id, category_id)`` so callers
    can cheaply intersect one player with friends.
    """

    if not user_ids:
        return {}
    placeholders = ", ".join("?" for _ in user_ids)
    params: list[int] = list(user_ids)
    category_clause = ""
    if category_id is not None:
        category_clause = "AND gs.category_id = ?"
        params.append(category_id)
    rows = db.execute(
        f"""
        SELECT gs.*, c.name AS category_name, c.slug AS category_slug
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.user_id IN ({placeholders})
          AND gs.status = 'ended'
          {category_clause}
        ORDER BY gs.user_id ASC,
                 gs.category_id ASC,
                 gs.score DESC,
                 gs.elapsed_seconds IS NULL ASC,
                 gs.elapsed_seconds ASC,
                 gs.ended_at DESC,
                 gs.created_at DESC,
                 gs.id DESC
        """,
        params,
    ).fetchall()
    best: dict[tuple[int, int], sqlite3.Row] = {}
    for row in rows:
        key = (int(row["user_id"]), int(row["category_id"]))
        if key not in best:
            best[key] = row
    return best


def _run_payload(db: sqlite3.Connection, row: sqlite3.Row, username: str) -> dict:
    """Serialize a best run with the same accepted-answer list as Results."""

    mode = mode_for_category(require_game_mode(row["game_mode"]), row["category_slug"])
    return {
        "user_id": int(row["user_id"]),
        "username": username,
        "game_id": row["id"],
        "game_mode": row["game_mode"],
        "mode_name": mode.name,
        "category_id": int(row["category_id"]),
        "category_name": row["category_name"],
        "category_slug": row["category_slug"],
        "score": int(row["score"]),
        "elapsed_seconds": row["elapsed_seconds"],
        "ended_at": row["ended_at"],
        "results_url": f"/results/{row['id']}",
        "answers": final_list_answers(db, row["id"]),
    }


@bp.post("/users")
def upsert_user():
    data = _json()
    client_uuid = data.get("client_uuid")
    username = data.get("username")
    try:
        user = ensure_user(client_uuid, username)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    return jsonify(
        {
            "client_uuid": user["client_uuid"],
            "username": user["current_display_name"],
        }
    )


@bp.post("/games")
def create_game():
    data = _json()
    db = get_db()
    try:
        user = ensure_user(data.get("client_uuid"), data.get("username"))
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409

    mode = get_game_mode(data.get("game_mode"))
    if not mode:
        return jsonify({"error": "Choose a valid game mode."}), 400

    category = db.execute(
        "SELECT * FROM categories WHERE slug = ? AND is_active = 1",
        (data.get("category_slug"),),
    ).fetchone()

    if not category:
        return jsonify({"error": "Choose a valid category."}), 404

    game_id = str(uuid.uuid4())
    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO game_sessions
            (id, user_id, display_name, category_id, game_mode, status, score, seconds_awarded,
             submitted_to_leaderboard, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'created', 0, 0, 0, ?, ?)
        """,
        (
            game_id,
            int(user["id"]),
            user["current_display_name"],
            int(category["id"]),
            mode.slug,
            now,
            now,
        ),
    )
    db.commit()
    category_mode = mode_for_category(mode, category["slug"])
    return jsonify(
        {
            "game_id": game_id,
            "play_url": f"/play/{game_id}",
            "category_name": category["name"],
            "game_mode": mode.slug,
            "mode_name": category_mode.name,
            "starting_seconds": category_mode.starting_seconds,
        }
    )


@bp.post("/games/<game_id>/start")
def api_start_game(game_id: str):
    db = get_db()
    game = start_game(db, game_id)
    if not game:
        return jsonify({"error": "Game not found."}), 404
    return jsonify(
        {
            "status": game["status"],
            "started_at": game["started_at"],
            "remaining_seconds": remaining_seconds(db, game),
            "elapsed_seconds": elapsed_seconds(game),
            "score": int(game["score"]),
        }
    )


@bp.post("/games/<game_id>/answers")
def api_submit_answer(game_id: str):
    data = _json()
    try:
        result = submit_answer(get_db(), game_id, data.get("answer", ""))
    except sqlite3.OperationalError as exc:
        current_app.logger.error("Could not submit answer for game %s: %s", game_id, exc)
        return jsonify({"status": "invalid", "message": "Still checking the previous answer. Try again in a moment."}), 503
    status_code = 200
    if result.get("message") == "Game not found.":
        status_code = 404
    return jsonify(result), status_code


@bp.post("/games/<game_id>/end")
def api_end_game(game_id: str):
    data = _json()
    db = get_db()
    game = end_game(db, game_id, data.get("reason", "timeout"))
    if not game:
        return jsonify({"error": "Game not found."}), 404
    category = db.execute("SELECT name FROM categories WHERE id = ?", (int(game["category_id"]),)).fetchone()
    mode = category_mode_for_game(db, game)
    return jsonify(
        {
            "game_id": game_id,
            "category_name": category["name"] if category else "Category",
            "game_mode": mode.slug,
            "mode_name": mode.name,
            "final_score": int(game["score"]),
            "elapsed_seconds": elapsed_seconds(game),
            "can_submit": can_submit_to_leaderboard(db, game),
            "results_url": f"/results/{game_id}",
        }
    )


@bp.post("/games/<game_id>/submit-score")
def submit_score(game_id: str):
    db = get_db()
    game = db.execute("SELECT * FROM game_sessions WHERE id = ?", (game_id,)).fetchone()
    if not game:
        return jsonify({"error": "Game not found."}), 404
    if game["status"] != "ended":
        game = end_game(db, game_id, "stopped")
    if not game:
        return jsonify({"error": "Game not found."}), 404
    mode = category_mode_for_game(db, game)
    if game["submitted_to_leaderboard"]:
        entries = top_leaderboard(db, int(game["category_id"]), mode.slug, 10)
        return jsonify({"submitted": True, "leaderboard": entries})
    if not can_submit_to_leaderboard(db, game):
        return jsonify({"error": f"{mode.name} scores can only be submitted after a completed run."}), 400

    now = utc_now_iso()
    db.execute(
        """
        INSERT INTO leaderboard_entries
            (user_id, display_name, category_id, game_mode, game_session_id,
             score, elapsed_seconds, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            int(game["user_id"]),
            game["display_name"],
            int(game["category_id"]),
            mode.slug,
            game_id,
            int(game["score"]),
            game["elapsed_seconds"],
            now,
        ),
    )
    db.execute(
        "UPDATE game_sessions SET submitted_to_leaderboard = 1, updated_at = ? WHERE id = ?",
        (now, game_id),
    )
    db.commit()
    entries = top_leaderboard(db, int(game["category_id"]), mode.slug, 10)
    return jsonify({"submitted": True, "leaderboard": entries})


@bp.get("/leaderboards/<category_slug>")
def api_leaderboard(category_slug: str):
    db = get_db()
    category = db.execute("SELECT * FROM categories WHERE slug = ?", (category_slug,)).fetchone()
    if not category:
        return jsonify({"error": "Category not found."}), 404
    mode = get_game_mode(request.args.get("game_mode") or "survival")
    if not mode:
        return jsonify({"error": "Choose a valid game mode."}), 400
    category_mode = mode_for_category(mode, category["slug"])
    return jsonify(
        {
            "category_name": category["name"],
            "game_mode": category_mode.slug,
            "mode_name": category_mode.name,
            "entries": top_leaderboard(db, int(category["id"]), category_mode.slug, 10),
        }
    )


@bp.get("/me/scores")
def api_my_scores():
    db = get_db()
    client_uuid = request.args.get("client_uuid")
    if not client_uuid:
        return jsonify({"scores": []})
    user = db.execute("SELECT * FROM users WHERE client_uuid = ?", (client_uuid,)).fetchone()
    if not user:
        return jsonify({"scores": []})
    rows = db.execute(
        """
        SELECT gs.*, c.name AS category_name, c.slug AS category_slug
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.user_id = ? AND gs.status = 'ended'
        ORDER BY gs.ended_at DESC, gs.created_at DESC
        LIMIT 100
        """,
        (int(user["id"]),),
    ).fetchall()
    return jsonify(
        {
            "scores": [
                {
                    "game_mode": row["game_mode"],
                    "mode_name": mode_for_category(require_game_mode(row["game_mode"]), row["category_slug"]).name,
                    "category_name": row["category_name"],
                    "score": int(row["score"]),
                    "elapsed_seconds": row["elapsed_seconds"],
                    "submitted": bool(row["submitted_to_leaderboard"]),
                    "end_reason": row["end_reason"],
                    "ended_at": row["ended_at"],
                    "results_url": f"/results/{row['id']}",
                }
                for row in rows
            ]
        }
    )


@bp.get("/friends")
def api_friends_overview():
    db = get_db()
    try:
        user = _current_user_from_payload(request.args)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before using Friends."}), 400
    user_id = int(user["id"])

    incoming = db.execute(
        """
        SELECT fr.id, fr.created_at, u.current_display_name AS username
        FROM friend_requests fr
        JOIN users u ON u.id = fr.requester_id
        WHERE fr.recipient_id = ? AND fr.status = 'pending'
        ORDER BY fr.created_at ASC, fr.id ASC
        """,
        (user_id,),
    ).fetchall()
    outgoing = db.execute(
        """
        SELECT fr.id, fr.created_at, u.current_display_name AS username
        FROM friend_requests fr
        JOIN users u ON u.id = fr.recipient_id
        WHERE fr.requester_id = ? AND fr.status = 'pending'
        ORDER BY fr.created_at ASC, fr.id ASC
        """,
        (user_id,),
    ).fetchall()
    friends = db.execute(
        """
        SELECT u.id, u.current_display_name
        FROM friendships f
        JOIN users u
          ON u.id = CASE
              WHEN f.user_low_id = ? THEN f.user_high_id
              ELSE f.user_low_id
          END
        WHERE f.user_low_id = ? OR f.user_high_id = ?
        ORDER BY u.current_display_name COLLATE NOCASE ASC
        """,
        (user_id, user_id, user_id),
    ).fetchall()
    category_rows = db.execute(
        """
        SELECT DISTINCT c.id, c.slug, c.name
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.user_id = ? AND gs.status = 'ended'
        ORDER BY c.name COLLATE NOCASE ASC
        """,
        (user_id,),
    ).fetchall()
    return jsonify(
        {
            "user": _user_identity_payload(user),
            "incoming_requests": [
                {"id": int(row["id"]), "username": row["username"], "created_at": row["created_at"]}
                for row in incoming
            ],
            "outgoing_requests": [
                {"id": int(row["id"]), "username": row["username"], "created_at": row["created_at"]}
                for row in outgoing
            ],
            "friends": [_user_identity_payload(row) for row in friends],
            "categories": [
                {"id": int(row["id"]), "slug": row["slug"], "name": row["name"]}
                for row in category_rows
            ],
        }
    )


@bp.post("/friends/requests")
def api_send_friend_request():
    data = _json()
    db = get_db()
    try:
        user = _current_user_from_payload(data)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before sending friend requests."}), 400

    target_username = _clean_username(data.get("target_username"))
    target = db.execute(
        "SELECT * FROM users WHERE normalized_username = ?",
        (normalize_username_for_lookup(target_username),),
    ).fetchone()
    if not target:
        return jsonify({"error": "No player found with that exact username."}), 404

    user_id = int(user["id"])
    target_id = int(target["id"])
    if user_id == target_id:
        return jsonify({"error": "You cannot send a friend request to yourself."}), 400
    if _are_friends(db, user_id, target_id):
        return jsonify({"error": "You are already friends with that player."}), 400

    now = utc_now_iso()
    reverse_request = db.execute(
        """
        SELECT * FROM friend_requests
        WHERE requester_id = ? AND recipient_id = ? AND status = 'pending'
        """,
        (target_id, user_id),
    ).fetchone()
    if reverse_request:
        low_id, high_id = _friendship_pair(user_id, target_id)
        db.execute(
            "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE id = ?",
            (now, int(reverse_request["id"])),
        )
        db.execute(
            """
            INSERT OR IGNORE INTO friendships (user_low_id, user_high_id, created_at)
            VALUES (?, ?, ?)
            """,
            (low_id, high_id, now),
        )
        db.commit()
        return jsonify({"accepted": True, "message": f"You and {target['current_display_name']} are now friends."})

    existing_request = db.execute(
        """
        SELECT * FROM friend_requests
        WHERE requester_id = ? AND recipient_id = ?
        """,
        (user_id, target_id),
    ).fetchone()
    if existing_request and existing_request["status"] == "pending":
        return jsonify({"error": "You already sent that player a friend request."}), 400
    if existing_request:
        db.execute(
            "UPDATE friend_requests SET status = 'pending', updated_at = ? WHERE id = ?",
            (now, int(existing_request["id"])),
        )
    else:
        db.execute(
            """
            INSERT INTO friend_requests
                (requester_id, recipient_id, status, created_at, updated_at)
            VALUES (?, ?, 'pending', ?, ?)
            """,
            (user_id, target_id, now, now),
        )
    db.commit()
    return jsonify({"sent": True, "message": f"Friend request sent to {target['current_display_name']}."})


@bp.post("/friends/requests/<int:request_id>/accept")
def api_accept_friend_request(request_id: int):
    data = _json()
    db = get_db()
    try:
        user = _current_user_from_payload(data)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before accepting friend requests."}), 400

    request_row = db.execute(
        "SELECT * FROM friend_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not request_row:
        return jsonify({"error": "Friend request not found."}), 404
    if int(request_row["recipient_id"]) != int(user["id"]):
        current_app.logger.error(
            "User %s tried to accept request %s for recipient %s.",
            user["id"],
            request_id,
            request_row["recipient_id"],
        )
        return jsonify({"error": "Friend request not found."}), 404
    if request_row["status"] != "pending":
        return jsonify({"error": "That friend request is no longer pending."}), 400

    now = utc_now_iso()
    low_id, high_id = _friendship_pair(int(request_row["requester_id"]), int(request_row["recipient_id"]))
    db.execute(
        "UPDATE friend_requests SET status = 'accepted', updated_at = ? WHERE id = ?",
        (now, request_id),
    )
    db.execute(
        """
        INSERT OR IGNORE INTO friendships (user_low_id, user_high_id, created_at)
        VALUES (?, ?, ?)
        """,
        (low_id, high_id, now),
    )
    db.commit()
    return jsonify({"accepted": True})


@bp.post("/friends/requests/<int:request_id>/decline")
def api_decline_friend_request(request_id: int):
    data = _json()
    db = get_db()
    try:
        user = _current_user_from_payload(data)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before declining friend requests."}), 400

    request_row = db.execute(
        "SELECT * FROM friend_requests WHERE id = ?",
        (request_id,),
    ).fetchone()
    if not request_row:
        return jsonify({"error": "Friend request not found."}), 404
    if int(request_row["recipient_id"]) != int(user["id"]):
        current_app.logger.error(
            "User %s tried to decline request %s for recipient %s.",
            user["id"],
            request_id,
            request_row["recipient_id"],
        )
        return jsonify({"error": "Friend request not found."}), 404
    if request_row["status"] != "pending":
        return jsonify({"error": "That friend request is no longer pending."}), 400

    db.execute(
        "UPDATE friend_requests SET status = 'declined', updated_at = ? WHERE id = ?",
        (utc_now_iso(), request_id),
    )
    db.commit()
    return jsonify({"declined": True})


@bp.get("/friends/<int:friend_id>/comparison")
def api_friend_comparison(friend_id: int):
    db = get_db()
    try:
        user = _current_user_from_payload(request.args)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before comparing friends."}), 400
    user_id = int(user["id"])
    if not _are_friends(db, user_id, friend_id):
        return jsonify({"error": "That player is not your friend."}), 404

    friend = db.execute("SELECT * FROM users WHERE id = ?", (friend_id,)).fetchone()
    if not friend:
        current_app.logger.error("Friendship pointed at missing user id %s.", friend_id)
        return jsonify({"error": "That player is not your friend."}), 404

    best = _best_runs(db, [user_id, friend_id])
    user_categories = {category_id for run_user_id, category_id in best if run_user_id == user_id}
    friend_categories = {category_id for run_user_id, category_id in best if run_user_id == friend_id}
    shared_category_ids = sorted(
        user_categories & friend_categories,
        key=lambda category_id: best[(user_id, category_id)]["category_name"].lower(),
    )
    return jsonify(
        {
            "friend": _user_identity_payload(friend),
            "categories": [
                {
                    "category_id": category_id,
                    "category_name": best[(user_id, category_id)]["category_name"],
                    "you": _run_payload(db, best[(user_id, category_id)], user["current_display_name"]),
                    "friend": _run_payload(db, best[(friend_id, category_id)], friend["current_display_name"]),
                }
                for category_id in shared_category_ids
            ],
        }
    )


@bp.get("/friends/categories/<int:category_id>/comparison")
def api_category_friend_comparison(category_id: int):
    db = get_db()
    try:
        user = _current_user_from_payload(request.args)
    except UsernameTakenError as exc:
        return jsonify({"error": str(exc)}), 409
    if not user:
        return jsonify({"error": "Choose a username before comparing categories."}), 400
    user_id = int(user["id"])

    category = db.execute("SELECT * FROM categories WHERE id = ?", (category_id,)).fetchone()
    if not category:
        return jsonify({"error": "Category not found."}), 404
    friends = db.execute(
        """
        SELECT u.id, u.current_display_name
        FROM friendships f
        JOIN users u
          ON u.id = CASE
              WHEN f.user_low_id = ? THEN f.user_high_id
              ELSE f.user_low_id
          END
        WHERE f.user_low_id = ? OR f.user_high_id = ?
        ORDER BY u.current_display_name COLLATE NOCASE ASC
        """,
        (user_id, user_id, user_id),
    ).fetchall()
    users = [user, *friends]
    best = _best_runs(db, [int(row["id"]) for row in users], category_id)
    run_payloads = []
    for comparison_user in users:
        run = best.get((int(comparison_user["id"]), category_id))
        if run:
            run_payloads.append(_run_payload(db, run, comparison_user["current_display_name"]))
    return jsonify(
        {
            "category": {"id": int(category["id"]), "slug": category["slug"], "name": category["name"]},
            "runs": run_payloads,
        }
    )
