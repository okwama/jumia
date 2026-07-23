import sqlite3
from pathlib import Path

import openpyxl
import pytest

from jumia_feed_sync import bootstrap, db

_HEADER = ["Name", "SellerSKU", "ParentSKU", "Brand", "PrimaryCategory", "AdditionalCategory", "Price_KES"]


def _make_template(path: Path, rows: list[dict]) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(_HEADER)
    for row in rows:
        ws.append([row.get(col) for col in _HEADER])
    wb.save(path)


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.migrate(connection)
    return connection


def test_harvest_extracts_brand_and_category_pairs(tmp_path):
    path = tmp_path / "template.xlsx"
    _make_template(
        path,
        [
            {"Name": "Widget A", "SellerSKU": "A1", "Brand": "1045133 - Generic",
             "PrimaryCategory": "1002708 - Computing / Printer Ink"},
            {"Name": "Widget B", "SellerSKU": "A2", "Brand": "1045133 - Generic",
             "PrimaryCategory": "1002708 - Computing / Printer Ink",
             "AdditionalCategory": "1002999 - Computing / Accessories"},
        ],
    )
    row_count, pairs = bootstrap.harvest_from_workbook(str(path))
    assert row_count == 2
    assert ("brand", "1045133", "Generic") in pairs
    assert ("category", "1002708", "Computing / Printer Ink") in pairs
    assert ("category", "1002999", "Computing / Accessories") in pairs


def test_harvest_skips_malformed_values(tmp_path):
    path = tmp_path / "template.xlsx"
    _make_template(path, [{"Name": "Widget C", "SellerSKU": "A3", "Brand": "Generic", "PrimaryCategory": "no id here"}])
    _, pairs = bootstrap.harvest_from_workbook(str(path))
    assert pairs == []


def test_harvest_skips_fully_blank_rows(tmp_path):
    path = tmp_path / "template.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(_HEADER)
    ws.append(["Widget A", "A1", None, "1045133 - Generic", "1002708 - Computing / Printer Ink", None, 1500])
    ws.append([None] * len(_HEADER))
    wb.save(path)

    row_count, pairs = bootstrap.harvest_from_workbook(str(path))
    assert row_count == 1
    assert len(pairs) == 2


def test_upsert_catalog_dedupes_and_tracks_new(conn):
    pairs = [
        ("brand", "1045133", "Generic"),
        ("brand", "1045133", "Generic"),
        ("category", "1002708", "Computing / Printer Ink"),
    ]
    found, new = bootstrap.upsert_catalog(conn, pairs)
    assert found == 2
    assert new == 2
    assert conn.execute("SELECT COUNT(*) FROM id_label_catalog").fetchone()[0] == 2


def test_upsert_catalog_second_run_reports_zero_new(conn):
    pairs = [("brand", "1045133", "Generic")]
    bootstrap.upsert_catalog(conn, pairs)
    found, new = bootstrap.upsert_catalog(conn, pairs)
    assert found == 1
    assert new == 0


def test_harvest_end_to_end(tmp_path, conn):
    path = tmp_path / "template.xlsx"
    _make_template(
        path,
        [{"Name": "Widget A", "SellerSKU": "A1", "Brand": "1045133 - Generic",
          "PrimaryCategory": "1002708 - Computing / Printer Ink"}],
    )
    summary = bootstrap.harvest(conn, str(path))
    assert summary.rows_scanned == 1
    assert summary.pairs_found == 2
    assert summary.pairs_new == 2
