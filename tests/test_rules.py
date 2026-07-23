from pathlib import Path

import pytest
from pydantic import ValidationError

from jumia_feed_sync.rules import Rule, RuleConfig, load_rules, validate_batch

REAL_RULES_PATH = Path(__file__).parent.parent / "config" / "rules.yaml"


def test_real_rules_yaml_is_schema_valid():
    rules = load_rules(str(REAL_RULES_PATH))
    assert len(rules) == 14
    assert {r.id for r in rules} >= {"sku_required", "price_positive", "sale_price_lower"}


def test_malformed_check_key_fails_loudly():
    with pytest.raises(ValidationError):
        RuleConfig(rules=[{"id": "bad", "severity": "block", "check": {"not_a_real_check": True}}])


def test_not_empty_and_max_length():
    rule = Rule(id="r1", field="SellerSKU", severity="block", check={"not_empty": True, "max_length": 5})
    assert validate_batch([rule], [{"SellerSKU": "A1"}]) == []
    issues = validate_batch([rule], [{"SellerSKU": ""}])
    assert len(issues) == 1 and issues[0].rule_id == "r1"
    issues = validate_batch([rule], [{"SellerSKU": "TOOLONGSKU"}])
    assert len(issues) == 1


def test_unique_in_batch():
    rule = Rule(id="r1", field="SellerSKU", severity="block", check={"unique_in_batch": True})
    batch = [{"SellerSKU": "A1"}, {"SellerSKU": "A1"}, {"SellerSKU": "A2"}]
    issues = validate_batch([rule], batch)
    assert len(issues) == 2  # both A1 rows flagged


def test_expr_check_with_is_none():
    rule = Rule(
        id="r1", severity="block",
        check={"expr": "Sale_Price_KES is None or Sale_Price_KES < Price_KES"},
    )
    assert validate_batch([rule], [{"Price_KES": 100, "Sale_Price_KES": None}]) == []
    assert validate_batch([rule], [{"Price_KES": 100, "Sale_Price_KES": 50}]) == []
    issues = validate_batch([rule], [{"Price_KES": 100, "Sale_Price_KES": 150}])
    assert len(issues) == 1


def test_unresolved_brand_is_blocked_not_silently_skipped():
    """Regression: matches() alone treats None as "not applicable" (correct
    for genuinely optional fields), which would let an unresolved brand
    (Brand=None) pass silently -- exactly the case RESOLVE is supposed to
    catch. The real rules.yaml pairs not_empty with matches for this
    reason (config/rules.yaml brand_format/category_format)."""
    rule = Rule(
        id="brand_format", field="Brand", severity="block",
        check={"not_empty": True, "matches": r"^\d+ - .+$"},
    )
    issues = validate_batch([rule], [{"Brand": None}])
    assert len(issues) == 1


def test_matches_and_not_matches():
    brand_rule = Rule(id="brand_format", field="Brand", severity="block", check={"matches": r"^\d+ - .+$"})
    assert validate_batch([brand_rule], [{"Brand": "1045133 - Generic"}]) == []
    assert len(validate_batch([brand_rule], [{"Brand": "Generic"}])) == 1

    promo_rule = Rule(
        id="name_no_promo", field="Name", severity="block",
        check={"not_matches": r"(?i)\b(best|cheap)\b"},
    )
    assert validate_batch([promo_rule], [{"Name": "A great charger"}]) == []
    assert len(validate_batch([promo_rule], [{"Name": "Best charger ever"}])) == 1


def test_allowed_tags():
    rule = Rule(
        id="html", field="short_description", severity="warn",
        check={"allowed_tags": ["ul", "li", "p", "br", "strong"]},
    )
    assert validate_batch([rule], [{"short_description": "<ul><li>ok</li></ul>"}]) == []
    assert len(validate_batch([rule], [{"short_description": "<script>bad</script>"}])) == 1


def test_deferred_image_checks_never_fire_without_image_pipeline():
    rule = Rule(id="image_reachable", field="MainImage", severity="block", check={"http_status": 200})
    assert validate_batch([rule], [{"MainImage": "https://example.com/x.png"}]) == []


def test_never_short_circuits_collects_all_failures_per_row():
    rules = [
        Rule(id="r1", field="Name", severity="block", check={"not_empty": True}),
        Rule(id="r2", field="Price_KES", severity="block", check={"gt": 0}),
    ]
    issues = validate_batch(rules, [{"SellerSKU": "A1", "Name": "", "Price_KES": -5}])
    assert {i.rule_id for i in issues} == {"r1", "r2"}
