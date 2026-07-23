import pytest
from pydantic import ValidationError

from jumia_feed_sync.models import ExportRow


def test_requires_name_and_sku():
    with pytest.raises(ValidationError):
        ExportRow()


def test_minimal_valid_row():
    row = ExportRow(Name="Widget", SellerSKU="A1")
    assert row.SellerSKU == "A1"
    assert row.Brand is None


def test_coerces_price_string_to_float():
    row = ExportRow(Name="Widget", SellerSKU="A1", Price_KES="1500")
    assert row.Price_KES == 1500.0


def test_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        ExportRow(Name="Widget", SellerSKU="A1", NotARealColumn="x")
