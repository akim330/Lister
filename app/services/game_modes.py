from __future__ import annotations

from dataclasses import dataclass, replace


@dataclass(frozen=True)
class GameMode:
    """Describe the rule knobs that change from one mode to another.

    Keeping these values in one small object lets the routes, templates, and
    gameplay service share the same mode names, descriptions, and scoring rules
    without each layer needing to duplicate string literals or timer settings.
    """

    slug: str
    name: str
    description: str
    rules: tuple[str, ...]
    timer_kind: str
    starting_seconds: int | None
    target_score: int | None
    correct_answer_seconds: int
    leaderboard_metric: str


GAME_MODES: dict[str, GameMode] = {
    "survival": GameMode(
        slug="survival",
        name="Survival",
        description="Keep the clock alive by naming correct answers.",
        rules=(
            "Start with 60 seconds.",
            "Each new correct answer adds 6 seconds.",
            "The run ends when the timer hits zero or you stop.",
        ),
        timer_kind="countdown",
        starting_seconds=60,
        target_score=None,
        correct_answer_seconds=6,
        leaderboard_metric="score",
    ),
    "name_100": GameMode(
        slug="name_100",
        name="Name 100",
        description="Name 100 valid answers as quickly as possible.",
        rules=(
            "There is no countdown timer.",
            "The clock counts up from zero.",
            "The run is complete when you reach 100 accepted answers.",
        ),
        timer_kind="countup",
        starting_seconds=None,
        target_score=100,
        correct_answer_seconds=0,
        leaderboard_metric="elapsed",
    ),
}


DEFAULT_GAME_MODE = "survival"

# These category-specific targets keep the underlying mode slug stable while
# letting categories with fewer realistic answers present a fairer goal. The
# leaderboard still groups these runs under the same ``name_100`` ruleset slug,
# but the player-facing mode copy and completion check use the overridden count.
NAME_100_TARGET_OVERRIDES: dict[str, int] = {
    "us-states": 50,
    # There have been 47 presidencies but 45 distinct people, because Grover
    # Cleveland and Donald Trump each served non-consecutive terms. The answer
    # category is people, so Name 100 becomes Name 45 for U.S. presidents.
    "us-presidents": 45,
}


def all_game_modes() -> list[GameMode]:
    """Return modes in the order the home page should present them."""

    return [GAME_MODES["survival"], GAME_MODES["name_100"]]


def get_game_mode(slug: str | None) -> GameMode | None:
    """Resolve a submitted mode slug, returning None for unknown modes."""

    if not slug:
        return None
    return GAME_MODES.get(slug)


def require_game_mode(slug: str | None) -> GameMode:
    """Return a valid mode, falling back to Survival for older stored sessions.

    Existing local rows created before game modes will not have an intentional
    mode choice. Treating those rows as Survival keeps development data readable
    while new API calls still validate submitted mode slugs strictly.
    """

    return GAME_MODES.get(slug or "", GAME_MODES[DEFAULT_GAME_MODE])


def mode_for_category(mode: GameMode, category_slug: str | None) -> GameMode:
    """Return a player-facing copy of ``mode`` adjusted for one category.

    The stored game mode should remain stable because it is used in API payloads
    and leaderboard queries. When a category has a smaller target, this function
    creates a temporary display/config copy with the same slug and timer rules
    but with the target-sensitive name, description, and rules rewritten.
    """

    if mode.slug != "name_100":
        return mode
    target_score = NAME_100_TARGET_OVERRIDES.get(category_slug or "", mode.target_score)
    if target_score == mode.target_score:
        return mode
    return replace(
        mode,
        name=f"Name {target_score}",
        description=f"Name {target_score} valid answers as quickly as possible.",
        rules=(
            "There is no countdown timer.",
            "The clock counts up from zero.",
            f"The run is complete when you reach {target_score} accepted answers.",
        ),
        target_score=target_score,
    )
