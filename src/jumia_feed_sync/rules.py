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
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict
from simpleeval import simple_eval


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
    # Image checks land in M2 alongside the image probe pipeline (Readme.md #12) --
    # accepted here so config validates, but evaluate_row() defers them.
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


_DEFERRED_CHECKS = {"http_status", "min_width", "min_height", "corner_luminance_gt"}


def load_rules(path: str) -> list[Rule]:
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    return RuleConfig(**raw).rules


def _value(rule: Rule, row: dict) -> object:
    return row.get(rule.field)


def _check_not_empty(rule: Rule, row: dict, batch: list[dict]) -> bool:
    return _value(rule, row) not in (None, "")


def _check_max_length(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value in (None, "") or len(str(value)) <= rule.check.max_length


def _check_min_length(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value in (None, "") or len(str(value)) >= rule.check.min_length


def _check_unique_in_batch(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    if value in (None, ""):
        return True
    return sum(1 for r in batch if r.get(rule.field) == value) <= 1


def _check_gt(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value is None or float(value) > rule.check.gt


def _check_gte(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value is None or float(value) >= rule.check.gte


def _check_integer(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    if value is None:
        return True
    try:
        return float(value) == int(float(value))
    except (TypeError, ValueError):
        return False


def _check_matches(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value in (None, "") or re.match(rule.check.matches, str(value)) is not None


def _check_not_matches(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    return value in (None, "") or re.search(rule.check.not_matches, str(value)) is None


def _check_expr(rule: Rule, row: dict, batch: list[dict]) -> bool:
    return bool(simple_eval(rule.check.expr, names=row))


def _check_allowed_tags(rule: Rule, row: dict, batch: list[dict]) -> bool:
    value = _value(rule, row)
    if not value:
        return True
    tags_found = {tag.lower() for tag in re.findall(r"</?\s*([a-zA-Z0-9]+)", str(value))}
    allowed = {tag.lower() for tag in rule.check.allowed_tags}
    return tags_found.issubset(allowed)


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
}


def evaluate_row(rule: Rule, row: dict, batch: list[dict]) -> bool:
    """True if `row` passes every (non-deferred) check in `rule`."""
    for check_name, check_value in rule.check.model_dump(exclude_none=True).items():
        if check_name in _DEFERRED_CHECKS:
            continue
        if not _CHECK_FUNCS[check_name](rule, row, batch):
            return False
    return True


def validate_batch(rules: list[Rule], batch: list[dict]) -> list[Issue]:
    """Every rule runs against every row -- never short-circuits (Readme.md #15)."""
    issues = []
    for row in batch:
        sku = row.get("SellerSKU", "")
        for rule in rules:
            if not evaluate_row(rule, row, batch):
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
