"""Map a staged `products` row to an Upload_Template.xlsx-shaped dict.

This is the exact-match tier only (Readme.md #7 tier 1: `resolutions`
lookup on raw_value). Fuzzy suggestion (tier 2) and manual entry (tier 3)
are dashboard-driven UX that land in M4 -- an unresolved brand/category
here just comes through as None, which the rule engine's brand_format/
category_format checks correctly turn into a blocked row.

ParentSKU is left unmapped: real data shows it isn't reliably derivable
(Readme.md #13 Open Decision 4), so it needs the same resolutions-backed
lookup as brand/category, not built here.
"""

from __future__ import annotations

import sqlite3

from jumia_feed_sync import config

ResolutionMap = dict[tuple[str, str], tuple[str, str]]


def load_resolutions(conn: sqlite3.Connection) -> ResolutionMap:
    return {
        (kind, raw_value): (jumia_id, jumia_label)
        for kind, raw_value, jumia_id, jumia_label in conn.execute(
            "SELECT kind, raw_value, jumia_id, jumia_label FROM resolutions"
        )
    }


def map_product(product: dict, resolutions: ResolutionMap) -> dict:
    brand = resolutions.get(("brand", product.get("brand_raw")))
    category = resolutions.get(("category", product.get("product_type_raw")))
    in_stock = (product.get("availability") or "").strip().lower() == "in stock"

    return {
        "Name": product["title"],
        "Description": product.get("description"),
        "SellerSKU": product["sku"],
        "Brand": f"{brand[0]} - {brand[1]}" if brand else None,
        "PrimaryCategory": f"{category[0]} - {category[1]}" if category else None,
        "Price_KES": product.get("price_kes"),
        "Sale_Price_KES": product.get("sale_price_kes"),
        "Stock": config.STOCK_DEFAULT if in_stock else 0,
        "MainImage": product.get("image_link"),
    }
