from __future__ import annotations

import os
from flask import Flask

from .db import init_db, seed_categories_if_needed


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(
        SECRET_KEY="dev-change-me",
        DATABASE=os.path.join(app.instance_path, "lister.sqlite"),
        WIKIPEDIA_API_URL="https://en.wikipedia.org/w/api.php",
        WIKIDATA_API_URL="https://www.wikidata.org/w/api.php",
        # Wikimedia asks production callers to provide an identifying
        # User-Agent with contact information. Set WIKI_USER_AGENT in the
        # hosting environment before launch so throttling/debugging issues can
        # be traced back to this app instead of a generic shared host.
        WIKI_USER_AGENT=os.environ.get(
            "WIKI_USER_AGENT",
            "ListerRuntimeVerifier/0.1 (contact: set-WIKI_USER_AGENT@example.com)",
        ),
        # Live answer verification runs in the player's request path, so these
        # caps keep one cold-cache answer from expanding into a long Wikidata
        # graph crawl. Deeper discovery can still be handled later by offline
        # prewarming/admin tooling instead of blocking gameplay.
        WIKI_LIVE_RELATED_QID_FETCH_BUDGET=5,
        WIKI_LIVE_PATH_MAX_NODES=8,
        WIKI_THROTTLE_TTL_SECONDS=60,
        WIKI_NEGATIVE_CACHE_TTL_SECONDS=300,
    )

    if test_config:
        app.config.update(test_config)

    os.makedirs(app.instance_path, exist_ok=True)

    from .routes.main import bp as main_bp
    from .routes.api import bp as api_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(api_bp, url_prefix="/api")

    with app.app_context():
        init_db()
        seed_categories_if_needed()

    return app
