"""Map a staged `products` row to an Upload_Template.xlsx-shaped dict.

Category/brand resolution is the exact-match tier (Readme.md #7 tier 1:
`resolutions` lookup on raw_value) -- an unresolved brand/category comes
through as None, which the rule engine's brand_format/category_format
checks correctly turn into a blocked row. Fuzzy suggestion (tier 2) and
manual entry (tier 3) are the dashboard's Unresolved screen; see
resolve.py.

ParentSKU is left unmapped: real data shows it isn't reliably derivable
(Readme.md #13 Open Decision 4), so it needs the same resolutions-backed
lookup as brand/category, not built here.

field_overrides (Readme.md #10) lets a human correct Name/Description/
MainImage/Price_KES without editing `products` directly -- `products`
is overwritten on every feed ingest, so an edit made there would be
silently lost on the next fetch. Overrides live in a separate table and
are applied here, on top of the ingested value, every time a product is
mapped.
"""

from __future__ import annotations

import sqlite3

from jumia_feed_sync import config

ResolutionMap = dict[tuple[str, str], tuple[str, str]]
OverrideMap = dict[tuple[str, str], str]


def load_resolutions(conn: sqlite3.Connection) -> ResolutionMap:
    return {
        (kind, raw_value): (jumia_id, jumia_label)
        for kind, raw_value, jumia_id, jumia_label in conn.execute(
            "SELECT kind, raw_value, jumia_id, jumia_label FROM resolutions"
        )
    }


def load_overrides(conn: sqlite3.Connection) -> OverrideMap:
    return {(sku, field): value for sku, field, value in conn.execute("SELECT sku, field, value FROM field_overrides")}


def map_product(product: dict, resolutions: ResolutionMap, overrides: OverrideMap | None = None) -> dict:
    overrides = overrides or {}
    sku = product["sku"]
    brand = resolutions.get(("brand", product.get("brand_raw")))
    category = resolutions.get(("category", product.get("product_type_raw")))
    in_stock = (product.get("availability") or "").strip().lower() == "in stock"
    price_override = overrides.get((sku, "Price_KES"))

    return {
        "Name": overrides.get((sku, "Name"), product["title"]),
        "Description": overrides.get((sku, "Description"), product.get("description")),
        "SellerSKU": sku,
        "Brand": f"{brand[0]} - {brand[1]}" if brand else None,
        "PrimaryCategory": f"{category[0]} - {category[1]}" if category else None,
        "Price_KES": float(price_override) if price_override is not None else product.get("price_kes"),
        "Sale_Price_KES": product.get("sale_price_kes"),
        "Stock": config.STOCK_DEFAULT if in_stock else 0,
        "MainImage": overrides.get((sku, "MainImage"), product.get("image_link")),
    }
