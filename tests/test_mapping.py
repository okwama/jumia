from jumia_feed_sync.mapping import map_product


def _product(**overrides):
    base = {
        "sku": "UG-1", "title": "UGREEN Charger", "description": "desc",
        "image_link": "https://example.com/x.png", "price_kes": 3200.0,
        "brand_raw": "UGREEN", "product_type_raw": "Components & Accessories",
        "availability": "in stock",
    }
    base.update(overrides)
    return base


def test_maps_core_fields():
    row = map_product(_product(), resolutions={})
    assert row["SellerSKU"] == "UG-1"
    assert row["Name"] == "UGREEN Charger"
    assert row["Price_KES"] == 3200.0
    assert row["MainImage"] == "https://example.com/x.png"


def test_unresolved_brand_and_category_are_none():
    row = map_product(_product(), resolutions={})
    assert row["Brand"] is None
    assert row["PrimaryCategory"] is None


def test_resolved_brand_and_category_formatted_as_id_dash_label():
    resolutions = {
        ("brand", "UGREEN"): ("1118344", "Ugreen"),
        ("category", "Components & Accessories"): ("1000473", "Computing / Computer Accessories / Cables & Interconnects"),
    }
    row = map_product(_product(), resolutions)
    assert row["Brand"] == "1118344 - Ugreen"
    assert row["PrimaryCategory"] == "1000473 - Computing / Computer Accessories / Cables & Interconnects"


def test_stock_default_when_in_stock(monkeypatch):
    from jumia_feed_sync import config
    monkeypatch.setattr(config, "STOCK_DEFAULT", 7)
    row = map_product(_product(availability="in stock"), resolutions={})
    assert row["Stock"] == 7


def test_stock_zero_when_not_in_stock():
    row = map_product(_product(availability="out of stock"), resolutions={})
    assert row["Stock"] == 0
