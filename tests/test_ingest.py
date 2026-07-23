import sqlite3
from pathlib import Path

import pytest

from jumia_feed_sync import db, ingest

FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.migrate(connection)
    return connection


def test_parse_feed_extracts_all_items():
    items = ingest.parse_feed(FIXTURE.read_bytes())
    assert len(items) == 5
    assert {i.sku for i in items} == {
        "UG-55551B", "POWER CAB 3 PIN", "981-000870", "SDSDXEP-064G-GN4IN", "12XD005LUM",
    }


def test_parse_feed_strips_currency_suffix():
    items = {i.sku: i for i in ingest.parse_feed(FIXTURE.read_bytes())}
    assert items["UG-55551B"].price_kes == 32000.00


def test_parse_feed_handles_sku_with_spaces():
    items = {i.sku: i for i in ingest.parse_feed(FIXTURE.read_bytes())}
    assert items["POWER CAB 3 PIN"].title == "3 Pin Power Cable"


def test_parse_feed_decodes_html_entities():
    items = {i.sku: i for i in ingest.parse_feed(FIXTURE.read_bytes())}
    assert items["UG-55551B"].product_type_raw == "Components & Accessories"


def test_upsert_products_first_run_is_all_new(conn):
    items = ingest.parse_feed(FIXTURE.read_bytes())
    summary = ingest.upsert_products(conn, items)
    assert summary == ingest.IngestSummary(total=5, new=5, updated=0, unchanged=0)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 5


def test_upsert_products_second_run_with_same_data_is_unchanged(conn):
    items = ingest.parse_feed(FIXTURE.read_bytes())
    ingest.upsert_products(conn, items)
    summary = ingest.upsert_products(conn, items)
    assert summary == ingest.IngestSummary(total=5, new=0, updated=0, unchanged=5)


def test_upsert_products_detects_changed_price(conn):
    items = ingest.parse_feed(FIXTURE.read_bytes())
    ingest.upsert_products(conn, items)

    items[0].price_kes = 99999.00
    items[0].feed_hash = "different-hash-for-test"
    summary = ingest.upsert_products(conn, items)

    assert summary.updated == 1
    assert summary.unchanged == 4
    stored_price = conn.execute(
        "SELECT price_kes FROM products WHERE sku = ?", (items[0].sku,)
    ).fetchone()[0]
    assert stored_price == 99999.00
