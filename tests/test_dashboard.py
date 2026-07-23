"""Dashboard route tests via TestClient. See Readme.md #10.

Uses a real SQLite file (not :memory:) since the app opens its own
connection per request (app.py's documented pattern) -- tests seed data
through the same file path, then hit routes as a client would.
"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jumia_feed_sync import config, db, image, pipeline
from jumia_feed_sync.dashboard.app import app

REAL_RULES_PATH = Path(__file__).parent.parent / "config" / "rules.yaml"
FEED_FIXTURE = Path(__file__).parent / "fixtures" / "sample_feed.xml"


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(config, "RULES_PATH", str(REAL_RULES_PATH))
    monkeypatch.setattr(image, "probe_images", lambda conn, urls, **kw: {})
    monkeypatch.setattr("jumia_feed_sync.ingest.fetch_feed", lambda url, **kw: FEED_FIXTURE.read_bytes())
    conn = db.get_connection(str(db_path))
    conn.close()
    return db_path


@pytest.fixture
def client():
    return TestClient(app)


def _insert_product(conn, sku, **overrides):
    base = {
        "sku": sku, "title": "A perfectly valid product title", "description": "A different description",
        "image_link": None, "price_kes": 1500.0, "sale_price_kes": None, "brand_raw": "UGREEN",
        "product_type_raw": "Cables", "availability": "in stock", "condition": "new",
        "fetched_at": "2026-01-01T00:00:00Z", "feed_hash": f"hash-{sku}",
    }
    base.update(overrides)
    conn.execute(
        """INSERT INTO products (sku, title, description, image_link, price_kes, sale_price_kes,
                                  brand_raw, product_type_raw, availability, condition, fetched_at, feed_hash)
           VALUES (:sku, :title, :description, :image_link, :price_kes, :sale_price_kes,
                   :brand_raw, :product_type_raw, :availability, :condition, :fetched_at, :feed_hash)""",
        base,
    )
    conn.commit()


def _insert_resolution(conn, kind, raw_value, jumia_id, jumia_label):
    conn.execute(
        """INSERT INTO resolutions (kind, raw_value, jumia_id, jumia_label, confirmed_by_human, updated_at)
           VALUES (?, ?, ?, ?, 1, '2026-01-01T00:00:00Z')""",
        (kind, raw_value, jumia_id, jumia_label),
    )
    conn.commit()


def _seed_completed_run(db_path) -> int:
    conn = db.get_connection(str(db_path))
    _insert_resolution(conn, "brand", "UGREEN", "1118344", "Ugreen")
    _insert_resolution(conn, "category", "Cables", "1000473", "Computing / Cables")
    _insert_product(conn, "A1")
    _insert_product(conn, "A2", title="Too short")
    result = pipeline.run_validation(conn, rules_path=str(REAL_RULES_PATH))
    conn.close()
    return result.run_id


def test_index_redirects_to_run(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code in (302, 307)
    assert response.headers["location"] == "/run"


def test_run_page_with_no_runs(client):
    response = client.get("/run")
    assert response.status_code == 200
    assert "No runs yet" in response.text


def test_run_start_creates_a_completed_run(client, isolated_db):
    response = client.post("/run/start")
    assert response.status_code == 200

    conn = db.get_connection(str(isolated_db))
    status = conn.execute("SELECT status FROM validation_runs ORDER BY id DESC LIMIT 1").fetchone()[0]
    conn.close()
    assert status == "completed"


def test_run_start_disallows_concurrent_run(client, isolated_db):
    conn = db.get_connection(str(isolated_db))
    conn.execute("INSERT INTO validation_runs (started_at, status) VALUES ('2026-01-01T00:00:00Z', 'running')")
    conn.commit()
    conn.close()

    client.post("/run/start")

    conn = db.get_connection(str(isolated_db))
    count = conn.execute("SELECT COUNT(*) FROM validation_runs").fetchone()[0]
    conn.close()
    assert count == 1  # no second run created while one was in progress


def test_review_page_with_no_completed_run(client):
    response = client.get("/review/latest")
    assert response.status_code == 200
    assert "No completed run yet" in response.text


def test_review_page_shows_products(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)
    response = client.get(f"/review/{run_id}")
    assert response.status_code == 200
    assert "A1" in response.text
    assert "A2" in response.text


def test_review_page_latest_resolves_to_most_recent_completed(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)
    response = client.get("/review/latest")
    assert response.status_code == 200
    assert f"Run #{run_id}" in response.text


def test_review_grid_filter_blocked_only(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)
    response = client.get(f"/review/{run_id}/grid?filter=blocked")
    assert "A2" in response.text
    assert "A1" not in response.text


def test_review_detail_shows_issue_messages(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)
    response = client.get(f"/review/{run_id}/detail/A2")
    assert response.status_code == 200
    assert "name_length" in response.text


def test_review_override_excludes_selected_sku(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)

    response = client.post(f"/review/{run_id}/override", data={"action": "excluded", "sku": ["A1"]})
    assert response.status_code == 200

    conn = db.get_connection(str(isolated_db))
    override = conn.execute(
        "SELECT human_override FROM run_products WHERE run_id = ? AND sku = 'A1'", (run_id,)
    ).fetchone()[0]
    conn.close()
    assert override == "excluded"


def test_review_override_approves_selected_sku(client, isolated_db):
    run_id = _seed_completed_run(isolated_db)

    client.post(f"/review/{run_id}/override", data={"action": "approved", "sku": ["A2"]})

    conn = db.get_connection(str(isolated_db))
    override = conn.execute(
        "SELECT human_override FROM run_products WHERE run_id = ? AND sku = 'A2'", (run_id,)
    ).fetchone()[0]
    conn.close()
    assert override == "approved"
