"""RESOLVE stage: unresolved brand/category queue, fuzzy suggestions,
human confirmation. See Readme.md #7, #10, #15.

Grouped by raw_value, not by product -- many products share the same
raw brand/category text (58 real UGREEN products share one raw_value),
so resolving "UGREEN" once should cover all of them, not require
confirming the same value 58 times.

Fuzzy suggestions are lazy (see suggest()), not computed for every
unresolved group up front: scoring a single query against the real
175K-row brand catalog takes ~1s of matching time alone (confirmed
2026-07-23) -- fine for one row, too slow to do for every row on page
load.

Measured the same day: fetching the 175K-row brand catalog from SQLite
cost ~1.7s *on top of* the ~1s match, on every single request -- most
of a 3.5s round trip was re-reading data that hadn't changed since the
last request. `id_label_catalog` only changes via an explicit bootstrap
harvest, which doesn't happen while someone is actively resolving
brands, so it's cached in-process per `kind` for the life of the
running server. Tried a cheaper scorer (QRatio) as the other lever:
same speed win, but it ranked category matches visibly worse on real
data (buried "Computing / Computer Accessories" under unrelated
categories for a "Components & Accessories" query) -- not worth it.
Restart the dashboard after re-running a bootstrap harvest to pick up
new catalog entries.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone

from rapidfuzz import fuzz, process, utils

_RAW_COLUMN = {"brand": "brand_raw", "category": "product_type_raw"}
_catalog_cache: dict[str, list[tuple[str, str]]] = {}


@dataclass
class Suggestion:
    jumia_id: str
    jumia_label: str
    score: float


@dataclass
class UnresolvedGroup:
    raw_value: str
    product_count: int


def list_unresolved(conn: sqlite3.Connection, kind: str) -> list[UnresolvedGroup]:
    column = _RAW_COLUMN[kind]
    rows = conn.execute(
        f"""
        SELECT p.{column} AS raw_value, COUNT(*) AS product_count
        FROM products p
        WHERE p.{column} IS NOT NULL AND p.{column} != ''
          AND NOT EXISTS (
              SELECT 1 FROM resolutions r WHERE r.kind = ? AND r.raw_value = p.{column}
          )
        GROUP BY p.{column}
        ORDER BY product_count DESC
        """,
        (kind,),
    ).fetchall()
    return [UnresolvedGroup(raw_value=row[0], product_count=row[1]) for row in rows]


def _load_catalog(conn: sqlite3.Connection, kind: str) -> list[tuple[str, str]]:
    if kind not in _catalog_cache:
        _catalog_cache[kind] = conn.execute(
            "SELECT jumia_id, jumia_label FROM id_label_catalog WHERE kind = ?", (kind,)
        ).fetchall()
    return _catalog_cache[kind]


def suggest(conn: sqlite3.Connection, kind: str, raw_value: str, limit: int = 5) -> list[Suggestion]:
    catalog = _load_catalog(conn, kind)
    if not catalog:
        return []
    labels = [label for _, label in catalog]
    matches = process.extract(raw_value, labels, scorer=fuzz.WRatio, processor=utils.default_process, limit=limit)
    return [Suggestion(jumia_id=catalog[idx][0], jumia_label=catalog[idx][1], score=score) for _, score, idx in matches]


def confirm(
    conn: sqlite3.Connection,
    kind: str,
    raw_value: str,
    jumia_id: str,
    jumia_label: str,
    confirmed_by_human: bool = True,
) -> None:
    """Writes/updates resolutions and appends to resolutions_history
    (Readme.md #15 principle 3 -- a bad manual pick must be recoverable,
    not silently overwritten)."""
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO resolutions (kind, raw_value, jumia_id, jumia_label, confidence, confirmed_by_human, updated_at)
        VALUES (?, ?, ?, ?, NULL, ?, ?)
        ON CONFLICT(kind, raw_value) DO UPDATE SET
            jumia_id = excluded.jumia_id, jumia_label = excluded.jumia_label,
            confirmed_by_human = excluded.confirmed_by_human, updated_at = excluded.updated_at
        """,
        (kind, raw_value, jumia_id, jumia_label, int(confirmed_by_human), now),
    )
    conn.execute(
        """
        INSERT INTO resolutions_history (kind, raw_value, jumia_id, jumia_label, confirmed_by_human, changed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (kind, raw_value, jumia_id, jumia_label, int(confirmed_by_human), now),
    )
    conn.commit()
