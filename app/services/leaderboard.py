from __future__ import annotations

import sqlite3

from .game_modes import require_game_mode


def top_leaderboard(
    db: sqlite3.Connection,
    category_id: int,
    game_mode: str = "survival",
    limit: int = 10,
) -> list[dict]:
    """Return the top submitted scores for one category and one ruleset.

    Each mode has its own competitive metric: Survival ranks by highest score,
    while Name target runs rank completed lists by lowest elapsed time. The
    query still keeps one row per player so a single user cannot fill the whole
    board with repeated attempts. Entries also include a results URL so submitted
    scores can link back to the persisted final list behind them.
    """
    mode = require_game_mode(game_mode)
    if mode.leaderboard_metric == "elapsed":
        return _top_elapsed_leaderboard(db, category_id, mode.slug, limit)
    return _top_score_leaderboard(db, category_id, mode.slug, limit)


def _top_score_leaderboard(
    db: sqlite3.Connection,
    category_id: int,
    game_mode: str,
    limit: int,
) -> list[dict]:
    """Rank modes where the largest accepted-answer count wins."""

    rows = db.execute(
        """
        WITH best_scores AS (
            SELECT user_id, MAX(score) AS best_score
            FROM leaderboard_entries
            WHERE category_id = ?
              AND game_mode = ?
            GROUP BY user_id
        ), chosen_entries AS (
            SELECT le.*
            FROM leaderboard_entries le
            JOIN best_scores bs
              ON bs.user_id = le.user_id
             AND bs.best_score = le.score
            WHERE le.category_id = ?
              AND le.game_mode = ?
              AND le.id = (
                  SELECT le2.id
                  FROM leaderboard_entries le2
                  WHERE le2.category_id = le.category_id
                    AND le2.game_mode = le.game_mode
                    AND le2.user_id = le.user_id
                    AND le2.score = le.score
                  ORDER BY le2.created_at ASC, le2.id ASC
                  LIMIT 1
              )
        )
        SELECT display_name, game_session_id, score, elapsed_seconds, created_at
        FROM chosen_entries
        ORDER BY score DESC, created_at ASC
        LIMIT ?
        """,
        (category_id, game_mode, category_id, game_mode, limit),
    ).fetchall()

    entries: list[dict] = []
    previous_score: int | None = None
    current_rank = 0
    for index, row in enumerate(rows, start=1):
        score = int(row["score"])
        if previous_score is None or score != previous_score:
            current_rank = index
        entries.append(
            {
                "rank": current_rank,
                "username": row["display_name"],
                "game_session_id": row["game_session_id"],
                "results_url": f"/results/{row['game_session_id']}",
                "score": score,
                "elapsed_seconds": row["elapsed_seconds"],
                "created_at": row["created_at"],
            }
        )
        previous_score = score
    return entries


def _top_elapsed_leaderboard(
    db: sqlite3.Connection,
    category_id: int,
    game_mode: str,
    limit: int,
) -> list[dict]:
    """Rank completed target-score modes where the shortest run wins."""

    rows = db.execute(
        """
        WITH best_times AS (
            SELECT user_id, MIN(elapsed_seconds) AS best_elapsed
            FROM leaderboard_entries
            WHERE category_id = ?
              AND game_mode = ?
              AND elapsed_seconds IS NOT NULL
            GROUP BY user_id
        ), chosen_entries AS (
            SELECT le.*
            FROM leaderboard_entries le
            JOIN best_times bt
              ON bt.user_id = le.user_id
             AND bt.best_elapsed = le.elapsed_seconds
            WHERE le.category_id = ?
              AND le.game_mode = ?
              AND le.id = (
                  SELECT le2.id
                  FROM leaderboard_entries le2
                  WHERE le2.category_id = le.category_id
                    AND le2.game_mode = le.game_mode
                    AND le2.user_id = le.user_id
                    AND le2.elapsed_seconds = le.elapsed_seconds
                  ORDER BY le2.created_at ASC, le2.id ASC
                  LIMIT 1
              )
        )
        SELECT display_name, game_session_id, score, elapsed_seconds, created_at
        FROM chosen_entries
        ORDER BY elapsed_seconds ASC, created_at ASC
        LIMIT ?
        """,
        (category_id, game_mode, category_id, game_mode, limit),
    ).fetchall()

    entries: list[dict] = []
    previous_elapsed: int | None = None
    current_rank = 0
    for index, row in enumerate(rows, start=1):
        elapsed = int(row["elapsed_seconds"])
        if previous_elapsed is None or elapsed != previous_elapsed:
            current_rank = index
        entries.append(
            {
                "rank": current_rank,
                "username": row["display_name"],
                "game_session_id": row["game_session_id"],
                "results_url": f"/results/{row['game_session_id']}",
                "score": int(row["score"]),
                "elapsed_seconds": elapsed,
                "created_at": row["created_at"],
            }
        )
        previous_elapsed = elapsed
    return entries
