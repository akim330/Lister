# Lister

Lister is a Flask web game where you race to list as many valid items as possible in a category before time runs out.

## Features included

- Random category play
- Specific category play from leaderboards
- Username saved in browser
- Hidden browser user ID
- 60-second timer
- +6 seconds per new counted answer
- Stop button
- Server-side answer validation
- Case-insensitive matching
- Simple plural/singular handling
- Alias support
- Conservative typo matching
- Runtime Wikipedia/Wikidata answer verification
- Local cache of structured wiki entity data
- Taxonomy replacement logic, e.g. `Owl` can be replaced by `Barred Owl`
- Private scores page
- Per-category top 10 leaderboards
- Best submitted score only per user per category
- Shared ranks for ties
- Minimal clean UI

## Requirements

- Python 3.10+

## Run locally

```bash
cd lister_app
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python run.py
```

Then open:

```text
http://127.0.0.1:5000
```

The SQLite database is created automatically at:

```text
instance/lister.sqlite
```

Starter category metadata and manual alias hints are loaded from:

```text
app/data/categories/*.json
```

## Reset the database

Stop the server, delete:

```text
instance/lister.sqlite
```

Then run `python run.py` again.

## Runtime answer verification

When a player submits an answer, Lister first checks the local SQLite cache for
a normalized title or alias. If the entity is not cached yet, the server resolves
the text through the English Wikipedia API, follows the linked Wikidata item,
and stores only structured categorization data:

- canonical label, aliases, page title, short Wikidata description, and sitelink count
- discrete Wikidata item claims such as instance of, subclass of, parent taxon, and part of
- Wikipedia category titles as category labels
- per-category membership results

The cache intentionally does not store Wikipedia article text, extracts, or other
longform page content. Verified answers are mirrored into `category_elements`
only after they pass the active category rule, so existing scoring, duplicates,
leaderboards, and accepted-answer display can keep using the same game tables.

If the cache or category data gets into a bad state during development, stop the
server, delete:

```text
instance/lister.sqlite
```

Then run `python run.py` again. The database will recreate the schema, category
metadata, and manual alias hints.

## Editing categories

Edit or add JSON files in `app/data/categories/`. These files now define the
available categories and manual alias hints, not the full answer database.

Aliases are still useful for terse player inputs that Wikipedia may not resolve
the way a player expects:

```json
{
  "key": "united_states",
  "name": "United States",
  "aliases": ["USA", "US", "U.S.", "America"]
}
```

Each alias is stored as a category-specific lookup hint. The answer is still
accepted only if the resolved Wikipedia/Wikidata entity satisfies that category's
live verification rule.

## Legacy Wikidata category fetcher

`tools/fetch_wikidata_categories.py` is legacy offline tooling from the previous
preloaded-answer model. Gameplay no longer depends on it. It can still be useful
for reviewing Wikidata category ideas by running:

```bash
python tools/fetch_wikidata_categories.py --categories animals countries fruits --dry-run
```

The fetch settings live in:

```text
tools/wikidata_category_config.json
```
