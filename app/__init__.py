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
        WIKI_USER_AGENT="ListerRuntimeVerifier/0.1 (local development)",
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
