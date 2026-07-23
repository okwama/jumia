"""Feed fetch, normalize, upsert into staging. See Readme.md #4 (INGEST), #6, #11."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx
from lxml import etree

FEED_NS = {"g": "http://base.google.com/ns/1.0"}
_PRICE_RE = re.compile(r"^\s*([\d.]+)\s*[A-Z]{3}?\s*$")


@dataclass
class FeedItem:
    sku: str
    title: str
    description: str
    image_link: str
    price_kes: float
    brand_raw: str
    product_type_raw: str
    availability: str
    condition: str
    feed_hash: str


@dataclass
class IngestSummary:
    total: int
    new: int
    updated: int
    unchanged: int


def fetch_feed(url: str, timeout: float = 30.0) -> bytes:
    response = httpx.get(url, timeout=timeout, follow_redirects=True)
    response.raise_for_status()
    return response.content


def _text(item: etree._Element, tag: str) -> str:
    el = item.find(f"g:{tag}", FEED_NS)
    return (el.text or "").strip() if el is not None else ""


def _parse_price(raw: str) -> float:
    match = _PRICE_RE.match(raw)
    if not match:
        raise ValueError(f"Unparseable price: {raw!r}")
    return float(match.group(1))


def _feed_hash(*fields: str) -> str:
    return hashlib.sha256("|".join(fields).encode("utf-8")).hexdigest()


def parse_feed(xml_bytes: bytes) -> list[FeedItem]:
    root = etree.fromstring(xml_bytes)
    items = []
    for item in root.findall(".//item"):
        title = _text(item, "title")
        description = _text(item, "description")
        image_link = _text(item, "image_link")
        price_kes = _parse_price(_text(item, "price"))
        brand_raw = _text(item, "brand")
        product_type_raw = _text(item, "product_type")
        availability = _text(item, "availability")
        condition = _text(item, "condition")

        items.append(
            FeedItem(
                sku=_text(item, "id"),
                title=title,
                description=description,
                image_link=image_link,
                price_kes=price_kes,
                brand_raw=brand_raw,
                product_type_raw=product_type_raw,
                availability=availability,
                condition=condition,
                feed_hash=_feed_hash(
                    title, description, image_link, str(price_kes),
                    brand_raw, product_type_raw, availability, condition,
                ),
            )
        )
    return items


def upsert_products(conn: sqlite3.Connection, items: list[FeedItem]) -> IngestSummary:
    fetched_at = datetime.now(timezone.utc).isoformat()
    existing = dict(conn.execute("SELECT sku, feed_hash FROM products").fetchall())

    new = updated = unchanged = 0
    for item in items:
        prior_hash = existing.get(item.sku)
        if prior_hash is None:
            new += 1
        elif prior_hash != item.feed_hash:
            updated += 1
        else:
            unchanged += 1

        conn.execute(
            """
            INSERT INTO products (sku, title, description, image_link, price_kes,
                                   brand_raw, product_type_raw, availability, condition,
                                   fetched_at, feed_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(sku) DO UPDATE SET
                title=excluded.title, description=excluded.description,
                image_link=excluded.image_link, price_kes=excluded.price_kes,
                brand_raw=excluded.brand_raw, product_type_raw=excluded.product_type_raw,
                availability=excluded.availability, condition=excluded.condition,
                fetched_at=excluded.fetched_at, feed_hash=excluded.feed_hash
            """,
            (
                item.sku, item.title, item.description, item.image_link, item.price_kes,
                item.brand_raw, item.product_type_raw, item.availability, item.condition,
                fetched_at, item.feed_hash,
            ),
        )
    conn.commit()
    return IngestSummary(total=len(items), new=new, updated=updated, unchanged=unchanged)
