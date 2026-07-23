"""Async image probe: reachability, dimensions, corner-luminance white-
background heuristic. See Readme.md #9, #15.

Results are cached in SQLite keyed by URL with a config-driven max age
(config.IMAGE_CACHE_MAX_AGE_HOURS). This -- not per-run resumption -- is
the actual mechanism behind "resumable runs" (Readme.md #15 principle 1):
re-probing after a crash only re-fetches images whose cache entry is
missing or stale, regardless of which validation run asks, because the
cache is committed durably as each batch completes, independent of
whether the run that triggered it ever finishes.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import BytesIO

import httpx
from PIL import Image

from jumia_feed_sync import config

CORNER_PATCH_SIZE = 10


@dataclass
class ImageInfo:
    url: str
    status_code: int | None
    width: int | None
    height: int | None
    bytes: int | None
    format: str | None
    corner_luminance: float | None
    checked_at: str


def _corner_luminance(image: Image.Image) -> float:
    patch = image.convert("L").crop((0, 0, CORNER_PATCH_SIZE, CORNER_PATCH_SIZE))
    pixels = patch.tobytes()
    return sum(pixels) / len(pixels)


async def probe_one(client: httpx.AsyncClient, url: str) -> ImageInfo:
    checked_at = datetime.now(timezone.utc).isoformat()
    try:
        head = await client.head(url, timeout=15.0, follow_redirects=True)
    except httpx.HTTPError:
        return ImageInfo(url, None, None, None, None, None, None, checked_at)

    if head.status_code != 200:
        return ImageInfo(url, head.status_code, None, None, None, None, None, checked_at)

    try:
        response = await client.get(url, timeout=30.0, follow_redirects=True)
        response.raise_for_status()
        image = Image.open(BytesIO(response.content))
        width, height = image.size
        luminance = _corner_luminance(image)
        return ImageInfo(
            url, response.status_code, width, height, len(response.content),
            image.format, luminance, checked_at,
        )
    except (httpx.HTTPError, OSError):
        return ImageInfo(url, head.status_code, None, None, None, None, None, checked_at)


async def _probe_batch(urls: list[str], concurrency: int) -> list[ImageInfo]:
    semaphore = asyncio.Semaphore(concurrency)

    async with httpx.AsyncClient() as client:

        async def bound(url: str) -> ImageInfo:
            async with semaphore:
                return await probe_one(client, url)

        return await asyncio.gather(*(bound(url) for url in urls))


def _load_cached(conn: sqlite3.Connection, urls: list[str], max_age_hours: float) -> dict[str, ImageInfo]:
    if not urls:
        return {}
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=max_age_hours)).isoformat()
    placeholders = ",".join("?" for _ in urls)
    rows = conn.execute(
        f"""
        SELECT url, status_code, width, height, bytes, format, corner_luminance, checked_at
        FROM image_cache WHERE url IN ({placeholders}) AND checked_at >= ?
        """,
        (*urls, cutoff),
    ).fetchall()
    return {r[0]: ImageInfo(*r) for r in rows}


def _store(conn: sqlite3.Connection, infos: list[ImageInfo]) -> None:
    for info in infos:
        conn.execute(
            """
            INSERT INTO image_cache (url, status_code, width, height, bytes, format, corner_luminance, checked_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(url) DO UPDATE SET
                status_code=excluded.status_code, width=excluded.width, height=excluded.height,
                bytes=excluded.bytes, format=excluded.format, corner_luminance=excluded.corner_luminance,
                checked_at=excluded.checked_at
            """,
            (
                info.url, info.status_code, info.width, info.height, info.bytes,
                info.format, info.corner_luminance, info.checked_at,
            ),
        )
    conn.commit()


def probe_images(
    conn: sqlite3.Connection,
    urls: list[str],
    concurrency: int | None = None,
    max_age_hours: float | None = None,
) -> dict[str, ImageInfo]:
    """Cache-first: only misses/stale entries hit the network."""
    unique_urls = sorted({u for u in urls if u})
    max_age = config.IMAGE_CACHE_MAX_AGE_HOURS if max_age_hours is None else max_age_hours
    concurrency = config.IMAGE_PROBE_CONCURRENCY if concurrency is None else concurrency

    cached = _load_cached(conn, unique_urls, max_age)
    to_fetch = [u for u in unique_urls if u not in cached]
    if to_fetch:
        fetched = asyncio.run(_probe_batch(to_fetch, concurrency))
        _store(conn, fetched)
        for info in fetched:
            cached[info.url] = info
    return cached
