"""SQLite storage.

Design rule: `order_items` is append-only ground truth. Everything else in the
schema is either a slowly-changing attribute of a product (`items`), an explicit
human/agent override (`preferences`), or a record of what we predicted so it can
be scored later (`proposals`).

`orders.order_number` and `orders.source_ref` are UNIQUE, which is what makes
re-ingesting the same receipt a no-op instead of a double-count.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from . import config

SCHEMA_VERSION = 1

SCHEMA_V1 = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- One row per Kroger order.
CREATE TABLE IF NOT EXISTS orders (
    id            INTEGER PRIMARY KEY,
    order_number  TEXT UNIQUE,
    ordered_at    TEXT NOT NULL,                       -- ISO date, YYYY-MM-DD
    channel       TEXT NOT NULL DEFAULT 'unknown',     -- delivery|pickup|manual
    total_cents   INTEGER,
    savings_cents INTEGER,
    source        TEXT NOT NULL,                       -- gmail|scrape|manual
    source_ref    TEXT UNIQUE,                         -- gmail message id
    ingested_at   TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS ix_orders_ordered_at ON orders(ordered_at);

-- Product catalog. `upc` is the 13-digit Kroger productId the Cart API needs,
-- so it is NULL until resolve_upcs fills it in.
CREATE TABLE IF NOT EXISTS items (
    id          INTEGER PRIMARY KEY,
    upc         TEXT UNIQUE,
    name        TEXT NOT NULL,
    name_norm   TEXT NOT NULL,
    brand       TEXT,
    size        TEXT,
    category    TEXT,
    upc_status  TEXT NOT NULL DEFAULT 'unresolved',   -- unresolved|resolved|manual
    first_seen  TEXT,
    last_seen   TEXT,
    notes       TEXT
);
CREATE INDEX IF NOT EXISTS ix_items_name_norm  ON items(name_norm);
CREATE INDEX IF NOT EXISTS ix_items_category   ON items(category);
CREATE INDEX IF NOT EXISTS ix_items_upc_status ON items(upc_status);

-- Append-only purchase events.
CREATE TABLE IF NOT EXISTS order_items (
    id               INTEGER PRIMARY KEY,
    order_id         INTEGER NOT NULL REFERENCES orders(id)  ON DELETE CASCADE,
    item_id          INTEGER NOT NULL REFERENCES items(id),
    qty              INTEGER NOT NULL DEFAULT 1 CHECK (qty > 0),
    unit_price_cents INTEGER,
    paid_cents       INTEGER,
    was_substituted  INTEGER NOT NULL DEFAULT 0,
    note             TEXT
);
CREATE INDEX IF NOT EXISTS ix_oi_order ON order_items(order_id);
CREATE INDEX IF NOT EXISTS ix_oi_item  ON order_items(item_id);

-- Explicit per-item taste overrides. Survives even if the item stops recurring.
CREATE TABLE IF NOT EXISTS preferences (
    item_id    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    kind       TEXT NOT NULL,     -- like|avoid|never|always
    weight     REAL NOT NULL DEFAULT 1.0,
    reason     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (item_id, kind)
);

-- Category-level taste, e.g. 'prefer-generic' for dairy.
CREATE TABLE IF NOT EXISTS category_preferences (
    category   TEXT NOT NULL,
    kind       TEXT NOT NULL,     -- prefer-generic|prefer-brand|avoid
    weight     REAL NOT NULL DEFAULT 1.0,
    reason     TEXT,
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (category, kind)
);

-- What we proposed, and what actually happened. This table is the learning
-- signal: reconcile() scores propose-vs-actual and updates preferences.
CREATE TABLE IF NOT EXISTS proposals (
    id               INTEGER PRIMARY KEY,
    created_at       TEXT NOT NULL DEFAULT (datetime('now')),
    status           TEXT NOT NULL DEFAULT 'proposed',
                                        -- proposed|added|reconciled|rejected|expired
    channel          TEXT NOT NULL DEFAULT 'delivery',
    matched_order_id INTEGER REFERENCES orders(id),
    notes            TEXT
);
CREATE INDEX IF NOT EXISTS ix_proposals_status ON proposals(status);

CREATE TABLE IF NOT EXISTS proposal_items (
    proposal_id    INTEGER NOT NULL REFERENCES proposals(id) ON DELETE CASCADE,
    item_id        INTEGER NOT NULL REFERENCES items(id),
    qty            INTEGER NOT NULL CHECK (qty > 0),
    est_unit_cents INTEGER,
    resolved_upc   TEXT,
    added          INTEGER NOT NULL DEFAULT 0,
    outcome        TEXT,     -- bought|declined|swapped, set by reconcile
    PRIMARY KEY (proposal_id, item_id)
);

-- Alternate spellings that map to the same product.
CREATE TABLE IF NOT EXISTS aliases (
    name_norm TEXT PRIMARY KEY,
    item_id   INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE
);

-- Rolling price history, so we can flag "this is way more than you pay".
CREATE TABLE IF NOT EXISTS price_log (
    id            INTEGER PRIMARY KEY,
    item_id       INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    observed_at   TEXT NOT NULL,
    unit_cents    INTEGER NOT NULL,
    promo_cents   INTEGER,
    in_stock      INTEGER,
    source        TEXT NOT NULL DEFAULT 'kroger_api'
);
CREATE INDEX IF NOT EXISTS ix_price_item ON price_log(item_id, observed_at);

-- Cheap aggregate view. Anything needing a median is computed in cadence.py.
CREATE VIEW IF NOT EXISTS item_agg AS
SELECT i.id                                   AS item_id,
       i.upc,
       i.name,
       i.category,
       i.upc_status,
       COUNT(oi.id)                           AS times_bought,
       SUM(oi.qty)                            AS total_qty,
       MIN(o.ordered_at)                      AS first_order,
       MAX(o.ordered_at)                      AS last_order,
       CAST(julianday('now') - julianday(MAX(o.ordered_at)) AS INTEGER) AS days_since_last
FROM items i
JOIN order_items oi ON oi.item_id = i.id
JOIN orders      o  ON o.id      = oi.order_id
GROUP BY i.id;
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today() -> str:
    return datetime.now().date().isoformat()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or config.db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # WAL survives a crash mid-write and makes the db safe to read while writing.
    # Back up with `kroger export` rather than copying a live WAL database.
    conn.execute("PRAGMA journal_mode = WAL")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def migrate(conn: sqlite3.Connection) -> int:
    """Apply pending migrations. Returns the resulting schema version."""
    current = conn.execute("PRAGMA user_version").fetchone()[0]

    if current < 1:
        conn.executescript(SCHEMA_V1)
        conn.execute("PRAGMA user_version = 1")
        conn.commit()
        current = 1

    return current


def init(path: Path | None = None) -> sqlite3.Connection:
    conn = connect(path)
    migrate(conn)
    return conn


# ─── items ───────────────────────────────────────────────────────────────────

# Below this token-overlap, two names are treated as different products. Tuned so
# "a2 milk vitamin d whole milk" and "a2 milk whole" merge, but "whole milk" and
# "skim milk" (sharing only "milk") do not.
SIMILARITY_THRESHOLD = 0.6


def find_or_create_item(
    conn: sqlite3.Connection,
    name: str,
    upc: str | None = None,
    brand: str | None = None,
    size: str | None = None,
    threshold: float = SIMILARITY_THRESHOLD,
) -> tuple[int, bool]:
    """Resolve a receipt description to an items row.

    Precedence, strongest signal first:
      1. UPC -- authoritative, a 13-digit productId is unambiguous
      2. exact name_norm hit
      3. an alias recorded by a previous fuzzy match
      4. token similarity at or above `threshold`

    Returns (item_id, created).
    """
    from .text import normalize_name, similarity

    if upc:
        row = conn.execute("SELECT id FROM items WHERE upc = ?", (str(upc),)).fetchone()
        if row:
            return row["id"], False

    norm = normalize_name(name)
    if not norm:
        # Filler-word stripping can consume a whole description (e.g. "Item A").
        # With a UPC the row is still worth keeping -- fall back to a stub key
        # that a later name/UPC resolution pass can replace. Without either, the
        # description is unusable and we must not invent an item.
        if not upc:
            raise ValueError(
                f"description {name!r} normalizes to nothing and has no UPC to fall back on"
            )
        norm = f"upc:{upc}"

    row = conn.execute("SELECT id FROM items WHERE name_norm = ?", (norm,)).fetchone()
    if row:
        return row["id"], False

    row = conn.execute(
        "SELECT item_id FROM aliases WHERE name_norm = ?", (norm,)
    ).fetchone()
    if row:
        return row["item_id"], False

    best_id, best_score = None, 0.0
    for cand in conn.execute("SELECT id, name FROM items WHERE name_norm != ''"):
        score = similarity(name, cand["name"])
        if score > best_score:
            best_id, best_score = cand["id"], score

    if best_id is not None and best_score >= threshold:
        conn.execute(
            "INSERT OR IGNORE INTO aliases (name_norm, item_id) VALUES (?,?)",
            (norm, best_id),
        )
        return best_id, False

    cur = conn.execute(
        "INSERT INTO items (upc, name, name_norm, brand, size, upc_status) "
        "VALUES (?,?,?,?,?,?)",
        (
            str(upc) if upc else None,
            name,
            norm,
            brand,
            size,
            "resolved" if upc else "unresolved",
        ),
    )
    item_id = cur.lastrowid
    conn.execute(
        "INSERT OR IGNORE INTO aliases (name_norm, item_id) VALUES (?,?)", (norm, item_id)
    )
    return item_id, True


def touch_item(conn: sqlite3.Connection, item_id: int, ordered_at: str) -> None:
    """Widen first_seen/last_seen as orders are ingested."""
    conn.execute(
        "UPDATE items SET"
        "  first_seen = CASE WHEN first_seen IS NULL OR first_seen > ? THEN ? ELSE first_seen END,"
        "  last_seen  = CASE WHEN last_seen  IS NULL OR last_seen  < ? THEN ? ELSE last_seen  END"
        " WHERE id = ?",
        (ordered_at, ordered_at, ordered_at, ordered_at, item_id),
    )


def add_preference(
    conn: sqlite3.Connection,
    item_id: int,
    kind: str,
    weight: float = 1.0,
    reason: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO preferences (item_id, kind, weight, reason) VALUES (?,?,?,?) "
        "ON CONFLICT(item_id, kind) DO UPDATE SET weight=excluded.weight, "
        "reason=excluded.reason, updated_at=datetime('now')",
        (item_id, kind, weight, reason),
    )


def set_upc(conn: sqlite3.Connection, item_id: int, upc: str) -> None:
    """Attach a resolved UPC. Refuses to steal a UPC already held by another item."""
    existing = conn.execute("SELECT id FROM items WHERE upc = ?", (str(upc),)).fetchone()
    if existing and existing["id"] != item_id:
        raise ValueError(
            f"UPC {upc} already belongs to item {existing['id']}; "
            "merge the items first rather than duplicating the UPC"
        )
    conn.execute(
        "UPDATE items SET upc = ?, upc_status = 'resolved' WHERE id = ?", (str(upc), item_id)
    )


# ─── summary ────────────────────────────────────────────────────────────────


def db_summary(conn: sqlite3.Connection) -> dict:
    def scalar(sql: str) -> int:
        return conn.execute(sql).fetchone()[0] or 0

    first = conn.execute("SELECT MIN(ordered_at) FROM orders").fetchone()[0]
    last = conn.execute("SELECT MAX(ordered_at) FROM orders").fetchone()[0]
    version = conn.execute("PRAGMA user_version").fetchone()[0]

    return {
        "schema_version": version,
        "path": str(config.db_path()),
        "orders": scalar("SELECT COUNT(*) FROM orders"),
        "items": scalar("SELECT COUNT(*) FROM items"),
        "order_items": scalar("SELECT COUNT(*) FROM order_items"),
        "items_unresolved_upc": scalar(
            "SELECT COUNT(*) FROM items WHERE upc IS NULL"
        ),
        "categories": scalar("SELECT COUNT(DISTINCT category) FROM items"),
        "proposals_open": scalar(
            "SELECT COUNT(*) FROM proposals WHERE status IN ('proposed','added')"
        ),
        "first_order": first,
        "last_order": last,
    }
