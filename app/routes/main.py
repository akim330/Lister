from __future__ import annotations

from flask import Blueprint, abort, render_template

from ..db import get_db
from ..services.game_modes import all_game_modes, mode_for_category, require_game_mode
from ..services.gameplay import can_submit_to_leaderboard, final_list_answers
from ..services.leaderboard import top_leaderboard

bp = Blueprint("main", __name__)


def grouped_categories(categories):
    """Return categories grouped by their theme bucket for the home picker.

    The database query orders rows by configured bucket and display order, so
    this helper only needs to preserve the current bucket while building a
    template-friendly list. Keeping this in Python keeps the Jinja template
    focused on rendering rather than stateful grouping logic.
    """

    groups = []
    current_bucket = None
    current_categories = None
    for category in categories:
        bucket = category["bucket"] or "General"
        if bucket != current_bucket:
            current_bucket = bucket
            current_categories = []
            groups.append({"bucket": bucket, "categories": current_categories})
        current_categories.append(category)
    return groups


def active_categories(db):
    """Return the active categories in the picker order shared by entry pages.

    The normal home page and challenge links both need to render the same
    category chooser. Keeping the query in one helper prevents challenge pages
    from drifting away from the default picker ordering as category buckets or
    display order values change.
    """

    return db.execute(
        """
        SELECT slug, name, bucket
        FROM categories
        WHERE is_active = 1
        ORDER BY bucket_order, display_order, name
        """
    ).fetchall()


@bp.get("/")
def index():
    db = get_db()
    categories = active_categories(db)
    return render_template(
        "index.html",
        category_groups=grouped_categories(categories),
        game_modes=all_game_modes(),
        challenge=None,
    )


@bp.get("/challenge/<game_id>")
def challenge(game_id: str):
    """Render a same-category, same-mode entry point for a shared finished run.

    Challenge links intentionally do not reuse the original session. The shared
    game id identifies the category and mode that should be challenged, then
    the receiver starts their own new run through the existing browser-local
    player flow so scores, timers, and answer lists stay separate.
    """

    db = get_db()
    game = db.execute(
        """
        SELECT gs.*, c.name AS category_name, c.slug AS category_slug
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.id = ?
        """,
        (game_id,),
    ).fetchone()
    if not game:
        abort(404)
    if game["status"] != "ended":
        abort(404)
    mode = mode_for_category(require_game_mode(game["game_mode"]), game["category_slug"])
    categories = active_categories(db)
    return render_template(
        "index.html",
        category_groups=grouped_categories(categories),
        game_modes=all_game_modes(),
        challenge={
            "game_id": game_id,
            "category_name": game["category_name"],
            "category_slug": game["category_slug"],
            "mode_name": mode.name,
            "mode_slug": mode.slug,
            "score": int(game["score"]),
        },
    )


@bp.get("/play/<game_id>")
def play(game_id: str):
    db = get_db()
    game = db.execute(
        """
        SELECT gs.*, c.name AS category_name, c.slug AS category_slug
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.id = ?
        """,
        (game_id,),
    ).fetchone()
    if not game:
        abort(404)
    mode = mode_for_category(require_game_mode(game["game_mode"]), game["category_slug"])
    return render_template(
        "play.html",
        game=game,
        mode=mode,
        starting_seconds=mode.starting_seconds,
    )


@bp.get("/results/<game_id>")
def results(game_id: str):
    db = get_db()
    game = db.execute(
        """
        SELECT gs.*, c.name AS category_name, c.slug AS category_slug
        FROM game_sessions gs
        JOIN categories c ON c.id = gs.category_id
        WHERE gs.id = ?
        """,
        (game_id,),
    ).fetchone()
    if not game:
        abort(404)
    mode = mode_for_category(require_game_mode(game["game_mode"]), game["category_slug"])
    entries = top_leaderboard(db, int(game["category_id"]), mode.slug, 10)
    return render_template(
        "results.html",
        game=game,
        mode=mode,
        leaderboard=entries,
        can_submit=can_submit_to_leaderboard(db, game),
        final_answers=final_list_answers(db, game_id),
    )


@bp.get("/leaderboards")
def leaderboards():
    db = get_db()
    categories = db.execute(
        "SELECT * FROM categories WHERE is_active = 1 ORDER BY name"
    ).fetchall()
    cards = []
    for category in categories:
        boards = []
        for mode in all_game_modes():
            category_mode = mode_for_category(mode, category["slug"])
            boards.append(
                {
                    "mode": category_mode,
                    "entries": top_leaderboard(db, int(category["id"]), category_mode.slug, 10),
                }
            )
        cards.append({"category": category, "boards": boards})
    return render_template("leaderboards.html", cards=cards)


@bp.get("/me/scores")
def my_scores():
    return render_template("your_scores.html")


@bp.get("/friends")
def friends():
    return render_template("friends.html")
