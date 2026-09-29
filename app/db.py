"""SQLite store. One connection per request; schema applied at startup.

Four tables, and the monthly re-price's log (`reprice_runs`, one row per run,
and `reprice_items`, what became of each item it planned — `app/reprice.py`):
- items       — one row per steelbook in the library. `upc` is the primary
                identification key when the back photo has a readable barcode;
                the vision fields (title/edition/retailer/region) fill the rest.
                `spine_color` is the shelf spine's hex, taken from the front
                photo at save; NULL = not yet computed, '' = the photo gave none.
                `spine_box` is where the case sits in the spine photo (JSON,
                `spine.Box`); NULL = not yet looked for, '' = none found. With it,
                `spine_ratio` (the box's width over height) and `spine_reads`
                (1 when its print is legible at shelf size). `spine_choice` is
                the owner's override: NULL = automatic, 'photo' or 'edge'.
                `genre` (the main one) and `director` come from TMDB by title
                (`app/film.py`) or the edit form; `tmdb_id` is the match, NULL =
                not looked up yet, 0 = TMDB found none, -1 = typed by hand, which
                TMDB never touches (HAND_SET).
- photos      — front / spine / back / other images on disk under PHOTO_DIR.
- valuations  — one row per pricing fetch, never overwritten, so the shelf's
                worth can be charted over time. `source` names the pricing
                module: `ebay_active` (by UPC) or `ebay_keyword` (by title, looser),
                `serpapi_sold` / `soldcomps_sold` (sold listings via SerpApi or
                SoldComps), or `manual_sold` — sold
                prices typed in by hand. `via` is 'monthly' for a row the monthly
                re-price wrote, NULL for one fetched or typed in by hand.
- listings    — the eBay listings a valuation was computed from, one row each, so
                the item page can show them. `excluded` = 1 marks one the owner set
                aside as not this edition; the flag is per (item, key), written on
                every row of that key, and later fetches leave the key out.
"""

import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    format      TEXT,               -- '4K UHD', 'Blu-ray', '4K+BD', ...
    edition     TEXT,               -- 'Steelbook', 'Steelbook (Limited)', ...
    retailer    TEXT,               -- 'Best Buy', 'Zavvi', 'Walmart', ...
    region      TEXT,               -- 'US', 'UK', 'DE', ...
    upc         TEXT,
    condition   TEXT,               -- 'sealed', 'opened', 'dented', ...
    notes       TEXT,
    spine_color TEXT,
    spine_box   TEXT,
    spine_ratio REAL,
    spine_reads INTEGER,
    spine_choice TEXT,             -- NULL automatic | 'photo' | 'edge'
    paid_price  REAL,               -- what was paid, in the valuation currency
    paid_on     TEXT,               -- 'YYYY-MM-DD', as typed
    genre       TEXT,               -- the main one: 'Science Fiction', 'Horror', ...
    director    TEXT,               -- co-directors joined with ' & '
    tmdb_id     INTEGER,            -- NULL not looked up | 0 no match | -1 by hand
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_items_upc ON items(upc);

CREATE TABLE IF NOT EXISTS photos (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL CHECK (kind IN ('front', 'spine', 'back', 'other')),
    path        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS valuations (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    source      TEXT NOT NULL,      -- ebay_active|ebay_keyword|serpapi_active[_used]|
                                    -- serpapi_sold|soldcomps_sold|manual_sold
    low         REAL,
    median      REAL,
    high        REAL,
    n_listings  INTEGER,
    currency    TEXT NOT NULL DEFAULT 'USD',
    fetched_at  TEXT NOT NULL DEFAULT (datetime('now')),
    via         TEXT,               -- 'monthly' | NULL (by hand)
    matched     TEXT                -- how its listings were matched to the edition:
                                    -- upc|judged|keywords|retailer+region|retailer|
                                    -- region|all|typed; NULL = from before, when
                                    -- low and high were the extremes, not quartiles
);
CREATE INDEX IF NOT EXISTS idx_valuations_item ON valuations(item_id, fetched_at);

CREATE TABLE IF NOT EXISTS listings (
    id           INTEGER PRIMARY KEY,
    valuation_id INTEGER NOT NULL REFERENCES valuations(id) ON DELETE CASCADE,
    item_id      INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    key          TEXT NOT NULL,     -- eBay item id; stable across fetches
    title        TEXT NOT NULL,
    price        REAL NOT NULL,
    condition    TEXT,
    url          TEXT,
    excluded     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_listings_valuation ON listings(valuation_id);
CREATE INDEX IF NOT EXISTS idx_listings_item_key ON listings(item_id, key);

CREATE TABLE IF NOT EXISTS reprice_runs (
    id            INTEGER PRIMARY KEY,
    trigger       TEXT NOT NULL,    -- 'schedule' | 'resume' | 'hand'
    started_at    TEXT NOT NULL DEFAULT (datetime('now')),
    finished_at   TEXT,
    planned       INTEGER NOT NULL, -- items it set out to re-price
    priced        INTEGER NOT NULL DEFAULT 0,
    no_listings   INTEGER NOT NULL DEFAULT 0,
    failed        INTEGER NOT NULL DEFAULT 0,
    searches_left INTEGER,          -- SerpApi's count at the start; NULL for another source
    stopped       TEXT              -- why it stopped short; NULL = reached every item
);

CREATE TABLE IF NOT EXISTS reprice_items (
    run_id      INTEGER NOT NULL REFERENCES reprice_runs(id) ON DELETE CASCADE,
    item_id     INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    outcome     TEXT NOT NULL,      -- priced | no_listings | failed | not_reached
    detail      TEXT
);
CREATE INDEX IF NOT EXISTS idx_reprice_items_item ON reprice_items(item_id, run_id);
"""


def connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    # A writer (the film lookup, a re-price) commits often; wait for it, don't fail.
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Columns added after a table first shipped: CREATE TABLE IF NOT EXISTS leaves
# an existing table as it was, so init_db adds any of these that are missing.
ADDED_COLUMNS = {"items": [("spine_color", "TEXT"), ("paid_price", "REAL"), ("paid_on", "TEXT"),
                           ("spine_box", "TEXT"), ("spine_ratio", "REAL"),
                           ("spine_reads", "INTEGER"), ("spine_choice", "TEXT"),
                           ("genre", "TEXT"), ("director", "TEXT"), ("tmdb_id", "INTEGER")],
                 "valuations": [("via", "TEXT"), ("matched", "TEXT")]}


def init_db(path: str) -> None:
    with connect(path) as conn:
        # WAL: a page's reads never wait on a background writer. It keeps -wal and
        # -shm files beside the database, so a backup must use sqlite3's .backup:
        # a file copy of a live WAL database can miss committed pages.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        for table, cols in ADDED_COLUMNS.items():
            have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
            for name, decl in cols:
                if name not in have:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def count_items(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM items").fetchone()[0]


ITEM_FIELDS = ("title", "format", "edition", "retailer", "region", "upc", "condition", "notes",
               "paid_price", "paid_on", "genre", "director")


def insert_item(conn: sqlite3.Connection, fields: dict[str, str | None]) -> int:
    cols = [f for f in ITEM_FIELDS if f in fields]
    cur = conn.execute(
        f"INSERT INTO items ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
        [fields[c] for c in cols],
    )
    return cur.lastrowid


def update_item(conn: sqlite3.Connection, item_id: int, fields: dict[str, str | None]) -> None:
    cols = [f for f in ITEM_FIELDS if f in fields]
    conn.execute(
        f"UPDATE items SET {', '.join(f'{c} = ?' for c in cols)} WHERE id = ?",
        [fields[c] for c in cols] + [item_id],
    )


def insert_photo(conn: sqlite3.Connection, item_id: int, kind: str, path: str) -> int:
    cur = conn.execute(
        "INSERT INTO photos (item_id, kind, path) VALUES (?, ?, ?)", (item_id, kind, path)
    )
    return cur.lastrowid


def set_spine_color(conn: sqlite3.Connection, item_id: int, color: str | None) -> None:
    conn.execute("UPDATE items SET spine_color = ? WHERE id = ?", (color or "", item_id))


def items_missing_spine_color(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(item id, front photo path) for each item whose spine colour was never computed."""
    return conn.execute(
        """
        SELECT i.id AS item_id, p.path
        FROM items i JOIN photos p ON p.id = (
            SELECT min(id) FROM photos WHERE item_id = i.id AND kind = 'front')
        WHERE i.spine_color IS NULL
        """
    ).fetchall()


def set_spine_box(conn: sqlite3.Connection, item_id: int, box: str, ratio: float | None,
                  reads: bool | None) -> None:
    """Record where the case is in the spine photo; '' box = none found."""
    conn.execute(
        "UPDATE items SET spine_box = ?, spine_ratio = ?, spine_reads = ? WHERE id = ?",
        (box, ratio, None if reads is None else int(reads), item_id),
    )


def set_spine_choice(conn: sqlite3.Connection, item_id: int, choice: str | None) -> None:
    conn.execute("UPDATE items SET spine_choice = ? WHERE id = ?", (choice, item_id))


def spine_photo(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM photos WHERE item_id = ? AND kind = 'spine' ORDER BY id LIMIT 1",
        (item_id,),
    ).fetchone()


def items_missing_spine_box(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(item id, spine photo path) for each item whose spine photo was never looked at."""
    return conn.execute(
        """
        SELECT i.id AS item_id, p.path
        FROM items i JOIN photos p ON p.id = (
            SELECT min(id) FROM photos WHERE item_id = i.id AND kind = 'spine')
        WHERE i.spine_box IS NULL
        """
    ).fetchall()


def items_missing_film(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(id, title) of each item never looked up on TMDB, oldest first."""
    return conn.execute(
        "SELECT id, title FROM items WHERE tmdb_id IS NULL ORDER BY created_at, id"
    ).fetchall()


def set_film(conn: sqlite3.Connection, item_id: int, film) -> None:
    """Record a TMDB lookup (an `app.film.Film`, or None for no match); genre and
    director are written only where blank, so a typed one stands."""
    conn.execute(
        """
        UPDATE items SET tmdb_id = ?,
               genre = coalesce(nullif(genre, ''), ?),
               director = coalesce(nullif(director, ''), ?)
        WHERE id = ?
        """,
        (film.tmdb_id, film.genre, film.director, item_id) if film else (0, None, None, item_id),
    )


HAND_SET = -1  # tmdb_id of an item whose genre and director were typed


def reset_all_films(conn: sqlite3.Connection) -> int:
    """Forget every lookup and what it filled, for a fresh one; how many items. An item
    set by hand keeps its genre and director."""
    return conn.execute(
        "UPDATE items SET tmdb_id = NULL, genre = NULL, director = NULL"
        " WHERE tmdb_id IS NULL OR tmdb_id <> ?", (HAND_SET,)).rowcount


def set_film_by_hand(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark the item's genre and director as typed: no lookup fills or refetches them."""
    conn.execute("UPDATE items SET tmdb_id = ? WHERE id = ?", (HAND_SET, item_id))


def reset_film(conn: sqlite3.Connection, item_id: int) -> None:
    """Mark the item for a fresh lookup (its title changed)."""
    conn.execute("UPDATE items SET tmdb_id = NULL WHERE id = ?", (item_id,))


def get_item(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()


def all_photos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute("SELECT id, item_id, kind, path FROM photos ORDER BY id").fetchall()


def front_photo(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM photos WHERE item_id = ? AND kind = 'front' ORDER BY id LIMIT 1",
        (item_id,),
    ).fetchone()


def front_photos(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """(item_id, path) of every item's first front photo."""
    return conn.execute(
        """
        SELECT i.id AS item_id, p.path
        FROM items i JOIN photos p ON p.id = (
            SELECT min(id) FROM photos WHERE item_id = i.id AND kind = 'front')
        """
    ).fetchall()


def get_photo(conn: sqlite3.Connection, photo_id: int) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()


def item_photos(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    # front, spine, back, then anything else — the order they sit on the shelf page.
    return conn.execute(
        "SELECT * FROM photos WHERE item_id = ? ORDER BY"
        " CASE kind WHEN 'front' THEN 0 WHEN 'spine' THEN 1 WHEN 'back' THEN 2 ELSE 3 END, id",
        (item_id,),
    ).fetchall()


def item_valuations(conn: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM valuations WHERE item_id = ? ORDER BY fetched_at DESC, id DESC",
        (item_id,),
    ).fetchall()


def valuation_listings(conn: sqlite3.Connection, valuation_id: int) -> list[sqlite3.Row]:
    """The listings behind one valuation, priciest first, excluded ones last."""
    return conn.execute(
        "SELECT * FROM listings WHERE valuation_id = ? ORDER BY excluded, price DESC, id",
        (valuation_id,),
    ).fetchall()


# An item missing either of these was identified thinly: the model could not place
# the release. A missing UPC is not on the list — most steelbook cases carry none,
# and the title search prices without one.
REVIEW_FIELDS = ("format", "region")


def items_to_review(conn: sqlite3.Connection) -> list[tuple[sqlite3.Row, list[str]]]:
    """(item, the REVIEW_FIELDS it is missing) for each thin item, oldest first."""
    rows = conn.execute(
        "SELECT * FROM items WHERE "
        + " OR ".join(f"coalesce({f}, '') = ''" for f in REVIEW_FIELDS)
        + " ORDER BY created_at, id"
    ).fetchall()
    return [(r, [f for f in REVIEW_FIELDS if not r[f]]) for r in rows]


def valuations_by_item(conn: sqlite3.Connection) -> dict[int, list[sqlite3.Row]]:
    """Every valuation, grouped by item, newest first within each."""
    by_item: dict[int, list[sqlite3.Row]] = {}
    for v in conn.execute("SELECT * FROM valuations ORDER BY fetched_at DESC, id DESC"):
        by_item.setdefault(v["item_id"], []).append(v)
    return by_item


# The worth's columns, as list_items names them.
LATEST = {"median": "latest_median", "currency": "latest_currency", "source": "latest_source",
          "low": "latest_low", "high": "latest_high", "n_listings": "latest_n",
          "fetched_at": "latest_fetched_at", "matched": "latest_matched"}


def list_items(conn: sqlite3.Connection) -> list[dict]:
    """Every item with its front photo id and what it is worth, newest first.

    `latest_*` are the valuation `pricing.current_worth` picks — median, currency,
    source, low, high, listing count, moment and how the listings were matched —
    all None for an item with no price.
    """
    from app.pricing import current_worth

    vals = valuations_by_item(conn)
    rows = conn.execute(
        """
        SELECT i.*,
               (SELECT p.id FROM photos p WHERE p.item_id = i.id AND p.kind = 'front'
                ORDER BY p.id LIMIT 1) AS front_photo_id
        FROM items i
        ORDER BY i.created_at DESC, i.id DESC
        """
    ).fetchall()
    items = []
    for r in rows:
        worth = current_worth(vals.get(r["id"], []))
        items.append({**dict(r), **{name: worth[col] if worth else None
                                    for col, name in LATEST.items()}})
    return items


def valuation_history(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """Every valuation's item, source, median and moment, oldest first — the shelf's
    history."""
    return conn.execute(
        "SELECT id, item_id, source, median, currency, fetched_at FROM valuations"
        " ORDER BY fetched_at, id"
    ).fetchall()
