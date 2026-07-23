"""Orchestrates INGEST/VALIDATE/EXPORT: fetch -> map -> validate -> persist -> write.
See Readme.md #4, #11, #15. CLI- and dashboard-independent so it's directly testable.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError

from jumia_feed_sync import config, export, image, ingest, mapping
from jumia_feed_sync.models import ExportRow
from jumia_feed_sync.rules import Issue, load_rules, validate_batch


@dataclass
class ValidationResult:
    run_id: int
    total: int
    passed: int
    blocked: int


@dataclass
class ExportResult:
    run_id: int
    category: str
    rows_written: int
    rows_rejected: int
    output_path: str
    rejects_path: str


def _fetch_all_products(conn: sqlite3.Connection) -> list[dict]:
    cursor = conn.execute("SELECT * FROM products")
    columns = [d[0] for d in cursor.description]
    return [dict(zip(columns, row)) for row in cursor.fetchall()]


def _fetch_products_by_sku(conn: sqlite3.Connection, skus: list[str]) -> dict[str, dict]:
    if not skus:
        return {}
    placeholders = ",".join("?" for _ in skus)
    cursor = conn.execute(f"SELECT * FROM products WHERE sku IN ({placeholders})", skus)
    columns = [d[0] for d in cursor.description]
    return {row["sku"]: row for row in (dict(zip(columns, r)) for r in cursor.fetchall())}


def start_run(conn: sqlite3.Connection) -> int:
    started_at = datetime.now(timezone.utc).isoformat()
    run_id = conn.execute(
        "INSERT INTO validation_runs (started_at, status, feed_item_count) VALUES (?, 'running', 0)",
        (started_at,),
    ).lastrowid
    conn.commit()
    return run_id


def _fail_run(conn: sqlite3.Connection, run_id: int, exc: Exception) -> None:
    conn.execute(
        "UPDATE validation_runs SET status = 'failed', finished_at = ?, error_message = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), str(exc), run_id),
    )
    conn.commit()


def _validate_core(conn: sqlite3.Connection, run_id: int, rules_path: str | None) -> ValidationResult:
    """Maps every staged product, probes each MainImage (cache-first, see
    image.py), runs the rule engine (never short-circuits, Readme.md
    #15), and persists run_products + row_issues.

    Resumability (principle 1) lives in the image cache, not in per-run
    stage bookkeeping here: image probing is the slow, network-bound
    step, and it's committed to image_cache as it completes, keyed by
    URL with a TTL -- independent of run_id. Re-running validate after a
    crash recomputes mapping/rules instantly and only re-fetches images
    that were never cached or have expired. run_products.stage is
    written as 'validated' in one pass; it isn't a partial-progress log
    within a single invocation."""
    rules = load_rules(rules_path or config.RULES_PATH)
    products = _fetch_all_products(conn)
    resolutions = mapping.load_resolutions(conn)
    overrides = mapping.load_overrides(conn)

    batch: list[dict] = []
    meta: dict[str, dict] = {}
    structural_issues: list[Issue] = []

    for product in products:
        sku = product["sku"]
        mapped = mapping.map_product(product, resolutions, overrides)
        try:
            row_dict = ExportRow(**mapped).model_dump()
        except ValidationError as exc:
            row_dict = mapped
            structural_issues.append(
                Issue(sku=sku, field=None, severity="block", rule_id="structural", message=str(exc))
            )
        batch.append(row_dict)
        meta[sku] = {
            "title": product.get("title"),
            "price_kes": product.get("price_kes"),
            "feed_hash": product.get("feed_hash"),
            "brand_resolved": mapped.get("Brand"),
            "category_resolved": mapped.get("PrimaryCategory"),
        }

    image_urls = [row.get("MainImage") for row in batch]
    image_cache = image.probe_images(conn, image_urls)

    issues = validate_batch(rules, batch, image_cache) + structural_issues
    issues_by_sku: dict[str, list[Issue]] = {}
    for issue in issues:
        issues_by_sku.setdefault(issue.sku, []).append(issue)

    passed = blocked = 0
    for sku, info in meta.items():
        sku_issues = issues_by_sku.get(sku, [])
        if any(i.severity == "block" for i in sku_issues):
            status, blocked = "blocked", blocked + 1
        else:
            status, passed = ("warned" if sku_issues else "passed"), passed + 1

        conn.execute(
            """
            INSERT INTO run_products (run_id, sku, title, price_kes, brand_resolved, category_resolved,
                                       stage, status, feed_hash)
            VALUES (?, ?, ?, ?, ?, ?, 'validated', ?, ?)
            """,
            (
                run_id, sku, info["title"], info["price_kes"], info["brand_resolved"],
                info["category_resolved"], status, info["feed_hash"],
            ),
        )
        for issue in sku_issues:
            conn.execute(
                "INSERT INTO row_issues (run_id, sku, field, severity, rule_id, message) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, issue.sku, issue.field, issue.severity, issue.rule_id, issue.message),
            )

    conn.execute(
        """
        UPDATE validation_runs
        SET status = 'completed', finished_at = ?, feed_item_count = ?, passed = ?, failed = ?
        WHERE id = ?
        """,
        (datetime.now(timezone.utc).isoformat(), len(products), passed, blocked, run_id),
    )
    conn.commit()
    return ValidationResult(run_id=run_id, total=len(products), passed=passed, blocked=blocked)


def run_validation(conn: sqlite3.Connection, rules_path: str | None = None) -> ValidationResult:
    """VALIDATE only, against whatever is already staged in `products`. On
    any unhandled exception the run is marked 'failed' with the error
    captured, not left spinning (Readme.md #15 principle 5)."""
    run_id = start_run(conn)
    try:
        return _validate_core(conn, run_id, rules_path)
    except Exception as exc:
        _fail_run(conn, run_id, exc)
        raise


def run_ingest_and_validate(
    conn: sqlite3.Connection,
    feed_url: str | None = None,
    rules_path: str | None = None,
    run_id: int | None = None,
) -> ValidationResult:
    """INGEST + VALIDATE as one tracked run -- what the dashboard's Run
    screen triggers (Readme.md #4 Run execution model, #10). Unlike
    run_validation, a feed-fetch failure is also captured on the run
    (started before ingest runs), not just a rule-evaluation failure.

    Pass an existing `run_id` (from a prior `start_run` call) when the
    caller needs the 'running' row to exist synchronously before this
    function's work begins -- the dashboard does this to avoid a race
    between its "already running?" check and the background task that
    actually does the work (Readme.md #4 Run execution model)."""
    if run_id is None:
        run_id = start_run(conn)
    try:
        xml_bytes = ingest.fetch_feed(feed_url or config.GOOGLE_FEED_API_ENDPOINT)
        items = ingest.parse_feed(xml_bytes)
        ingest.upsert_products(conn, items)
        return _validate_core(conn, run_id, rules_path)
    except Exception as exc:
        _fail_run(conn, run_id, exc)
        raise


def run_export(conn: sqlite3.Connection, run_id: int | None = None, out_dir: str = "./out") -> list[ExportResult]:
    """Rows are re-derived from live products+resolutions+overrides at
    export time (not stored verbatim in run_products, which only keeps a
    status summary) -- the approved SKU *set* comes from the run,
    current field values come from staging. If products/resolutions
    changed since validation, re-run validation before exporting.

    A row's automated status can be overridden by a human in the Review
    Grid (run_products.human_override, Readme.md #10): 'excluded' drops
    an otherwise passed/warned row, 'approved' includes an otherwise
    blocked one.

    Jumia's Seller Center upload flow requires selecting one category
    before uploading (confirmed 2026-07-23) -- a single file mixing rows
    from several categories isn't something you can actually upload. So
    this writes one .xlsx per resolved category ID among the approved
    rows, not one file for the whole run. A row with no resolved
    category (only possible via a human 'approved' override on a row
    that was blocked for missing category -- normal validation would
    never let that through) lands in a clearly-named 'uncategorized'
    file rather than being silently dropped."""
    if run_id is None:
        row = conn.execute(
            "SELECT id FROM validation_runs WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise ValueError("No completed validation run found -- run validation first")
        run_id = row[0]

    approved_skus = [
        r[0]
        for r in conn.execute(
            """
            SELECT sku FROM run_products
            WHERE run_id = ?
              AND (
                    (status IN ('passed', 'warned') AND (human_override IS NULL OR human_override != 'excluded'))
                    OR human_override = 'approved'
                  )
            """,
            (run_id,),
        )
    ]
    products = _fetch_products_by_sku(conn, approved_skus)
    resolutions = mapping.load_resolutions(conn)
    overrides = mapping.load_overrides(conn)

    rows_by_category: dict[str, list[dict]] = {}
    for sku in approved_skus:
        product = products.get(sku)
        if product is None:
            continue
        row = ExportRow(**mapping.map_product(product, resolutions, overrides)).model_dump()
        category_value = row.get("PrimaryCategory")
        category_key = category_value.split(" - ", 1)[0] if category_value else "uncategorized"
        rows_by_category.setdefault(category_key, []).append(row)

    issues = [
        Issue(sku=r[0], field=r[1], severity=r[2], rule_id=r[3], message=r[4])
        for r in conn.execute(
            "SELECT sku, field, severity, rule_id, message FROM row_issues WHERE run_id = ?", (run_id,)
        )
    ]

    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    rejects_path = out_path / f"rejects_{timestamp}.csv"
    rejected = export.write_rejects_csv(issues, str(rejects_path))

    results = []
    for category_key, rows in rows_by_category.items():
        output_path = out_path / f"jumia_upload_{category_key}_{timestamp}.xlsx"
        written = export.write_export(rows, config.UPLOAD_TEMPLATE_PATH, str(output_path))
        results.append(
            ExportResult(
                run_id=run_id, category=category_key, rows_written=written, rows_rejected=rejected,
                output_path=str(output_path), rejects_path=str(rejects_path),
            )
        )

    conn.execute(
        "UPDATE validation_runs SET exported_path = ? WHERE id = ?",
        (",".join(r.output_path for r in results), run_id),
    )
    conn.commit()

    return results
