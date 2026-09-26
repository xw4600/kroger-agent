import sqlite3

import pytest

from kroger import db
from kroger.text import normalize_name


@pytest.fixture
def conn(tmp_path):
    c = db.init(tmp_path / "test.db")
    yield c
    c.close()


def _item(conn, name, upc=None):
    """Upsert, mirroring what ingest does rather than blindly inserting."""
    item_id, _ = db.find_or_create_item(conn, name, upc=upc)
    return item_id


def test_migrate_is_idempotent(conn):
    v1 = db.migrate(conn)
    v2 = db.migrate(conn)
    assert v1 == v2 == db.SCHEMA_VERSION


def test_reingesting_same_order_is_a_noop(conn):
    """Re-running `ingest gmail` must not double-count an order."""
    _item(conn, "a2 Milk Whole", upc="00085239200100")
    for _ in range(3):
        try:
            conn.execute(
                "INSERT INTO orders (order_number, ordered_at, source, source_ref) "
                "VALUES ('12345','2026-09-01','gmail','msg-abc')"
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass

    assert conn.execute("SELECT COUNT(*) FROM orders").fetchone()[0] == 1


def test_order_items_fk_cascade(conn):
    order_id = conn.execute(
        "INSERT INTO orders (order_number, ordered_at, source) VALUES ('1','2026-09-01','gmail')"
    ).lastrowid
    item_id = _item(conn, "Eggs Large")
    conn.execute(
        "INSERT INTO order_items (order_id, item_id, qty) VALUES (?,?,2)", (order_id, item_id)
    )
    conn.commit()
    conn.execute("DELETE FROM orders WHERE id=?", (order_id,))
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM order_items").fetchone()[0] == 0


def test_item_agg_view(conn):
    for date, qty in (("2026-08-01", 1), ("2026-08-15", 2)):
        order_id = conn.execute(
            "INSERT INTO orders (order_number, ordered_at, source) VALUES (?,?,'gmail')",
            (f"o{date}", date),
        ).lastrowid
        item_id = _item(conn, "a2 Milk Whole", upc="00085239200100")
        conn.execute(
            "INSERT INTO order_items (order_id, item_id, qty) VALUES (?,?,?)",
            (order_id, item_id, qty),
        )
    conn.commit()

    row = conn.execute("SELECT * FROM item_agg").fetchone()
    assert row["times_bought"] == 2
    assert row["total_qty"] == 3
    assert row["first_order"] == "2026-08-01"
    assert row["last_order"] == "2026-08-15"
    assert row["upc"] == "00085239200100"


def test_proposal_outcomes_default_null(conn):
    item_id = _item(conn, "Coffee Dark Roast")
    pid = conn.execute("INSERT INTO proposals DEFAULT VALUES").lastrowid
    conn.execute(
        "INSERT INTO proposal_items (proposal_id, item_id, qty) VALUES (?,?,1)", (pid, item_id)
    )
    conn.commit()
    row = conn.execute("SELECT * FROM proposal_items").fetchone()
    assert row["outcome"] is None
    assert row["added"] == 0


def test_preferences_primary_key_prevents_duplicates(conn):
    item_id = _item(conn, "Cilantro")
    conn.execute("INSERT INTO preferences (item_id, kind) VALUES (?, 'never')", (item_id,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO preferences (item_id, kind) VALUES (?, 'never')", (item_id,))


def test_find_or_create_prefers_upc(conn):
    a, created_a = db.find_or_create_item(conn, "Whole Milk 1 gal", upc="0001111041700")
    b, created_b = db.find_or_create_item(conn, "Kroger Whole Milk", upc="0001111041700")
    assert created_a is True
    assert created_b is False
    assert a == b


def test_find_or_create_fuzzy_merges_receipt_variants(conn):
    a, _ = db.find_or_create_item(conn, "a2 Milk Vitamin D Whole Milk, 59 fl oz")
    b, created = db.find_or_create_item(conn, "A2 MILK WHOLE 59 OZ")
    assert created is False
    assert a == b


def test_find_or_create_does_not_merge_different_milks(conn):
    a, _ = db.find_or_create_item(conn, "Kroger Whole Milk 1 gal")
    b, created = db.find_or_create_item(conn, "Kroger Skim Milk 1 gal")
    assert created is True
    assert a != b


def test_find_or_create_rejects_empty_name(conn):
    with pytest.raises(ValueError):
        db.find_or_create_item(conn, "")


def test_set_upc_refuses_to_steal(conn):
    a, _ = db.find_or_create_item(conn, "Item A", upc="0000000000001")
    b, _ = db.find_or_create_item(conn, "Item B")
    with pytest.raises(ValueError, match="already belongs"):
        db.set_upc(conn, b, "0000000000001")
    assert conn.execute("SELECT upc FROM items WHERE id=?", (a,)).fetchone()[0] == "0000000000001"


def test_touch_item_widens_range(conn):
    item_id, _ = db.find_or_create_item(conn, "Eggs")
    db.touch_item(conn, item_id, "2026-08-15")
    db.touch_item(conn, item_id, "2026-07-01")
    db.touch_item(conn, item_id, "2026-09-01")
    conn.commit()
    row = conn.execute("SELECT first_seen, last_seen FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["first_seen"] == "2026-07-01"
    assert row["last_seen"] == "2026-09-01"


def test_find_or_create_falls_back_when_name_normalizes_away(conn):
    """Receipt labels like 'Item A' must not crash ingest when a UPC exists."""
    item_id, created = db.find_or_create_item(conn, "Item A", upc="0000000000009")
    assert created is True
    row = conn.execute("SELECT name_norm FROM items WHERE id=?", (item_id,)).fetchone()
    assert row["name_norm"] == "upc:0000000000009"
