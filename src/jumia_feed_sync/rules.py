"""YAML-driven rule engine. See Readme.md #8, #15.

Config is schema-validated on load (extra="forbid" everywhere) so a typo
in a check name fails loudly at load time, not silently mid-run.
Evaluation never short-circuits: every rule runs against every row, so
row_issues carries a row's complete problem list, not just the first
failure (Readme.md #15 principle 4).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict
from simpleeval import simple_eval

if TYPE_CHECKING:
    from jumia_feed_sync.image import ImageInfo


class RuleCheck(BaseModel):
    model_config = ConfigDict(extra="forbid")

    not_empty: bool | None = None
    max_length: int | None = None
    min_length: int | None = None
    unique_in_batch: bool | None = None
    gt: float | None = None
    gte: float | None = None
    integer: bool | None = None
    matches: str | None = None
    not_matches: str | None = None
    expr: str | None = None
    allowed_tags: list[str] | None = None
    # Image checks (Readme.md #9) -- looked up by the row's field value
    # (a MainImage URL) against the image_cache passed to validate_batch.
    # A URL with no cache entry (image pipeline never ran, or the field
    # is empty) skips the check rather than failing it, same as every
    # other check here on a missing value.
    http_status: int | None = None
    min_width: int | None = None
    min_height: int | None = None
    corner_luminance_gt: int | None = None


class Rule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    field: str | None = None
    severity: Literal["block", "warn"]
    check: RuleCheck
    message: str | None = None


class RuleConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[Rule]


@dataclass
class Issue:
    sku: str
    field: str | None
    severity: str
    rule_id: str
    message: str


def load_rules(path: str) -> list[Rule]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return RuleConfig(**raw).rules


def _value(rule: Rule, row: dict) -> object:
    return row.get(rule.field)


def _image_info(rule: Rule, row: dict, image_cache: dict[str, "ImageInfo"]) -> "ImageInfo | None":
    url = _value(rule, row)
    return image_cache.get(url) if url else None


def _check_not_empty(rule, row, batch, image_cache) -> bool:
    return _value(rule, row) not in (None, "")


def _check_max_length(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value in (None, "") or len(str(value)) <= rule.check.max_length


def _check_min_length(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value in (None, "") or len(str(value)) >= rule.check.min_length


def _check_unique_in_batch(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    if value in (None, ""):
        return True
    return sum(1 for r in batch if r.get(rule.field) == value) <= 1


def _check_gt(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value is None or float(value) > rule.check.gt


def _check_gte(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value is None or float(value) >= rule.check.gte


def _check_integer(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    if value is None:
        return True
    try:
        return float(value) == int(float(value))
    except (TypeError, ValueError):
        return False


def _check_matches(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value in (None, "") or re.match(rule.check.matches, str(value)) is not None


def _check_not_matches(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    return value in (None, "") or re.search(rule.check.not_matches, str(value)) is None


def _check_expr(rule, row, batch, image_cache) -> bool:
    return bool(simple_eval(rule.check.expr, names=row))


def _check_allowed_tags(rule, row, batch, image_cache) -> bool:
    value = _value(rule, row)
    if not value:
        return True
    tags_found = {tag.lower() for tag in re.findall(r"</?\s*([a-zA-Z0-9]+)", str(value))}
    allowed = {tag.lower() for tag in rule.check.allowed_tags}
    return tags_found.issubset(allowed)


def _check_http_status(rule, row, batch, image_cache) -> bool:
    info = _image_info(rule, row, image_cache)
    return info is None or info.status_code == rule.check.http_status


def _check_min_width(rule, row, batch, image_cache) -> bool:
    info = _image_info(rule, row, image_cache)
    return info is None or info.width is None or info.width >= rule.check.min_width


def _check_min_height(rule, row, batch, image_cache) -> bool:
    info = _image_info(rule, row, image_cache)
    return info is None or info.height is None or info.height >= rule.check.min_height


def _check_corner_luminance_gt(rule, row, batch, image_cache) -> bool:
    info = _image_info(rule, row, image_cache)
    return info is None or info.corner_luminance is None or info.corner_luminance > rule.check.corner_luminance_gt


_CHECK_FUNCS = {
    "not_empty": _check_not_empty,
    "max_length": _check_max_length,
    "min_length": _check_min_length,
    "unique_in_batch": _check_unique_in_batch,
    "gt": _check_gt,
    "gte": _check_gte,
    "integer": _check_integer,
    "matches": _check_matches,
    "not_matches": _check_not_matches,
    "expr": _check_expr,
    "allowed_tags": _check_allowed_tags,
    "http_status": _check_http_status,
    "min_width": _check_min_width,
    "min_height": _check_min_height,
    "corner_luminance_gt": _check_corner_luminance_gt,
}


def evaluate_row(rule: Rule, row: dict, batch: list[dict], image_cache: dict | None = None) -> bool:
    """True if `row` passes every check in `rule`."""
    image_cache = image_cache or {}
    for check_name in rule.check.model_dump(exclude_none=True):
        if not _CHECK_FUNCS[check_name](rule, row, batch, image_cache):
            return False
    return True


def validate_batch(rules: list[Rule], batch: list[dict], image_cache: dict | None = None) -> list[Issue]:
    """Every rule runs against every row -- never short-circuits (Readme.md #15)."""
    image_cache = image_cache or {}
    issues = []
    for row in batch:
        sku = row.get("SellerSKU", "")
        for rule in rules:
            if not evaluate_row(rule, row, batch, image_cache):
                issues.append(
                    Issue(
                        sku=sku,
                        field=rule.field,
                        severity=rule.severity,
                        rule_id=rule.id,
                        message=rule.message or f"{rule.id} failed",
                    )
                )
    return issues
