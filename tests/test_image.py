import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
import pytest
from PIL import Image as PILImage

from jumia_feed_sync import db, image


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    db.migrate(connection)
    return connection


def _png_bytes(size=(800, 800), color=(255, 255, 255)):
    buf = BytesIO()
    PILImage.new("RGB", size, color=color).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, status_code, content=b""):
        self.status_code = status_code
        self.content = content

    def raise_for_status(self):
        pass


class _FakeAsyncClient:
    def __init__(self, head_response=None, get_response=None, head_exc=None, get_exc=None):
        self.head_response = head_response
        self.get_response = get_response
        self.head_exc = head_exc
        self.get_exc = get_exc
        self.get_called = False

    async def head(self, url, timeout=None, follow_redirects=None):
        if self.head_exc:
            raise self.head_exc
        return self.head_response

    async def get(self, url, timeout=None, follow_redirects=None):
        self.get_called = True
        if self.get_exc:
            raise self.get_exc
        return self.get_response


def test_corner_luminance_white_patch_is_near_255():
    img = PILImage.new("RGB", (20, 20), color=(255, 255, 255))
    assert image._corner_luminance(img) > 250


def test_corner_luminance_black_patch_is_near_0():
    img = PILImage.new("RGB", (20, 20), color=(0, 0, 0))
    assert image._corner_luminance(img) < 5


def test_probe_one_success_extracts_dims_and_luminance():
    client = _FakeAsyncClient(
        head_response=_FakeResponse(200),
        get_response=_FakeResponse(200, _png_bytes(size=(800, 600), color=(255, 255, 255))),
    )
    info = asyncio.run(image.probe_one(client, "https://example.com/x.png"))
    assert info.status_code == 200
    assert (info.width, info.height) == (800, 600)
    assert info.corner_luminance > 250
    assert info.bytes == len(client.get_response.content)


def test_probe_one_skips_get_when_head_not_200():
    client = _FakeAsyncClient(head_response=_FakeResponse(404))
    info = asyncio.run(image.probe_one(client, "https://example.com/gone.png"))
    assert info.status_code == 404
    assert info.width is None
    assert client.get_called is False


def test_probe_one_head_network_error_yields_unreachable():
    client = _FakeAsyncClient(head_exc=httpx.ConnectError("boom"))
    info = asyncio.run(image.probe_one(client, "https://example.com/x.png"))
    assert info.status_code is None
    assert info.width is None


def test_probe_one_get_failure_after_head_ok_keeps_status_no_dims():
    client = _FakeAsyncClient(head_response=_FakeResponse(200), get_exc=httpx.ConnectError("boom"))
    info = asyncio.run(image.probe_one(client, "https://example.com/x.png"))
    assert info.status_code == 200
    assert info.width is None


def test_probe_images_empty_urls_no_network(conn, monkeypatch):
    def boom(*a, **kw):
        raise AssertionError("should not fetch")

    monkeypatch.setattr(image, "_probe_batch", boom)
    assert image.probe_images(conn, [None, "", None]) == {}


def test_probe_images_cache_hit_skips_network(conn, monkeypatch):
    url = "https://example.com/x.png"
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO image_cache (url, status_code, width, height, bytes, corner_luminance, checked_at) "
        "VALUES (?, 200, 800, 800, 1000, 250.0, ?)",
        (url, now),
    )
    conn.commit()

    def boom(*a, **kw):
        raise AssertionError("should not fetch cached url")

    monkeypatch.setattr(image, "_probe_batch", boom)
    result = image.probe_images(conn, [url])
    assert result[url].status_code == 200
    assert result[url].width == 800


def test_probe_images_cache_miss_fetches_and_stores(conn, monkeypatch):
    url = "https://example.com/new.png"
    fresh = image.ImageInfo(
        url=url, status_code=200, width=900, height=900, bytes=2000,
        corner_luminance=245.0, checked_at=datetime.now(timezone.utc).isoformat(),
    )
    async def fake_probe_batch(urls, concurrency):
        return [fresh]

    monkeypatch.setattr(image, "_probe_batch", lambda urls, concurrency: fake_probe_batch(urls, concurrency))

    result = image.probe_images(conn, [url])
    assert result[url].width == 900
    stored = conn.execute("SELECT width FROM image_cache WHERE url = ?", (url,)).fetchone()
    assert stored[0] == 900


def test_probe_images_stale_cache_is_refetched(conn, monkeypatch):
    url = "https://example.com/x.png"
    stale_time = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
    conn.execute(
        "INSERT INTO image_cache (url, status_code, width, height, bytes, corner_luminance, checked_at) "
        "VALUES (?, 200, 100, 100, 500, 250.0, ?)",
        (url, stale_time),
    )
    conn.commit()

    fresh = image.ImageInfo(
        url=url, status_code=200, width=900, height=900, bytes=2000,
        corner_luminance=245.0, checked_at=datetime.now(timezone.utc).isoformat(),
    )

    async def fake_probe_batch(urls, concurrency):
        return [fresh]

    monkeypatch.setattr(image, "_probe_batch", lambda urls, concurrency: fake_probe_batch(urls, concurrency))

    result = image.probe_images(conn, [url], max_age_hours=24)
    assert result[url].width == 900  # refetched, not the stale cached 100
