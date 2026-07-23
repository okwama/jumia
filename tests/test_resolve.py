import sqlite3

import pytest

from jumia_feed_sync import db, resolve


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.migrate(connection)
    return connection


def _insert_product(conn, sku, brand_raw, product_type_raw):
    conn.execute(
        """INSERT INTO products (sku, title, brand_raw, product_type_raw, fetched_at, feed_hash)
           VALUES (?, 'Title', ?, ?, '2026-01-01T00:00:00Z', 'hash')""",
        (sku, brand_raw, product_type_raw),
    )
    conn.commit()


def _insert_catalog(conn, kind, jumia_id, jumia_label):
    conn.execute(
        """INSERT INTO id_label_catalog (kind, jumia_id, jumia_label, source, first_seen_at)
           VALUES (?, ?, ?, 'jumia_reference', '2026-01-01T00:00:00Z')""",
        (kind, jumia_id, jumia_label),
    )
    conn.commit()


def test_list_unresolved_groups_by_raw_value_with_counts(conn):
    _insert_product(conn, "A1", "UGREEN", "Cables")
    _insert_product(conn, "A2", "UGREEN", "Cables")
    _insert_product(conn, "A3", "Epson", "Printers")

    groups = {g.raw_value: g.product_count for g in resolve.list_unresolved(conn, "brand")}
    assert groups == {"UGREEN": 2, "Epson": 1}


def test_list_unresolved_excludes_already_resolved(conn):
    _insert_product(conn, "A1", "UGREEN", "Cables")
    _insert_product(conn, "A2", "Epson", "Printers")
    resolve.confirm(conn, "brand", "UGREEN", "1118344", "Ugreen")

    groups = [g.raw_value for g in resolve.list_unresolved(conn, "brand")]
    assert groups == ["Epson"]


def test_list_unresolved_ignores_blank_raw_values(conn):
    _insert_product(conn, "A1", "", "Cables")
    _insert_product(conn, "A2", None, "Printers")

    assert resolve.list_unresolved(conn, "brand") == []


def test_suggest_ranks_case_insensitively(conn):
    _insert_catalog(conn, "brand", "1118344", "Ugreen")
    _insert_catalog(conn, "brand", "1036890", "Epson")

    suggestions = resolve.suggest(conn, "brand", "UGREEN")
    assert suggestions[0].jumia_label == "Ugreen"
    assert suggestions[0].score > 90


def test_suggest_empty_catalog_returns_empty(conn):
    assert resolve.suggest(conn, "brand", "UGREEN") == []


def test_confirm_writes_resolution(conn):
    resolve.confirm(conn, "brand", "UGREEN", "1118344", "Ugreen")
    row = conn.execute(
        "SELECT jumia_id, jumia_label, confirmed_by_human FROM resolutions WHERE kind='brand' AND raw_value='UGREEN'"
    ).fetchone()
    assert row == ("1118344", "Ugreen", 1)


def test_confirm_appends_to_history_on_every_call(conn):
    resolve.confirm(conn, "brand", "UGREEN", "1118344", "Ugreen")
    resolve.confirm(conn, "brand", "UGREEN", "9999999", "Wrong Pick -- Corrected")  # human changes their mind

    history = conn.execute(
        "SELECT jumia_id FROM resolutions_history WHERE kind='brand' AND raw_value='UGREEN' ORDER BY id"
    ).fetchall()
    assert history == [("1118344",), ("9999999",)]

    current = conn.execute(
        "SELECT jumia_id FROM resolutions WHERE kind='brand' AND raw_value='UGREEN'"
    ).fetchone()
    assert current == ("9999999",)
