"""CLI entry point. Resolve/validate/export subcommands land through M1."""

import argparse
from pathlib import Path

from jumia_feed_sync import bootstrap, config, db, ingest, pipeline


def _connect():
    config.ensure_db_parent()
    conn = db.connect(config.DB_PATH)
    db.migrate(conn)
    return conn


def cmd_migrate(_args: argparse.Namespace) -> None:
    _connect()
    print(f"Migrations applied to {config.DB_PATH}")


def cmd_ingest(args: argparse.Namespace) -> None:
    conn = _connect()
    xml_bytes = Path(args.file).read_bytes() if args.file else ingest.fetch_feed(config.GOOGLE_FEED_API_ENDPOINT)
    items = ingest.parse_feed(xml_bytes)
    summary = ingest.upsert_products(conn, items)
    print(
        f"Ingested {summary.total} items: "
        f"{summary.new} new, {summary.updated} updated, {summary.unchanged} unchanged"
    )


def cmd_bootstrap(args: argparse.Namespace) -> None:
    conn = _connect()
    path = args.file or config.UPLOAD_TEMPLATE_PATH
    summary = bootstrap.harvest(conn, path)
    print(
        f"Scanned {summary.rows_scanned} rows: "
        f"{summary.pairs_found} known id/label pairs, {summary.pairs_new} new to the catalog"
    )


def cmd_bootstrap_guidelines(args: argparse.Namespace) -> None:
    conn = _connect()
    path = args.file or config.JUMIA_GUIDELINES_PATH
    summary = bootstrap.harvest_guidelines(conn, path)
    print(
        f"Scanned {summary.rows_scanned} rows: "
        f"{summary.pairs_found} known id/label pairs, {summary.pairs_new} new to the catalog"
    )


def cmd_validate(_args: argparse.Namespace) -> None:
    conn = _connect()
    result = pipeline.run_validation(conn)
    print(f"Run {result.run_id}: {result.total} products validated -- {result.passed} passed/warned, {result.blocked} blocked")


def cmd_export(args: argparse.Namespace) -> None:
    conn = _connect()
    try:
        result = pipeline.run_export(conn, run_id=args.run_id)
    except ValueError as exc:
        print(exc)
        return
    print(
        f"Exported {result.rows_written} rows to {result.output_path}; "
        f"{result.rows_rejected} blocked rows logged to {result.rejects_path}"
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="jumia-feed-sync")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("migrate", help="Apply pending SQLite migrations").set_defaults(func=cmd_migrate)

    ingest_parser = subparsers.add_parser("ingest", help="Fetch the feed and stage products")
    ingest_parser.add_argument("--file", help="Parse a local feed XML file instead of fetching GOOGLE_FEED_API_ENDPOINT")
    ingest_parser.set_defaults(func=cmd_ingest)

    bootstrap_parser = subparsers.add_parser(
        "bootstrap", help="Harvest known brand/category ID-label pairs from a filled Upload_Template.xlsx"
    )
    bootstrap_parser.add_argument("--file", help="Path to the filled template (defaults to UPLOAD_TEMPLATE_PATH)")
    bootstrap_parser.set_defaults(func=cmd_bootstrap)

    guidelines_parser = subparsers.add_parser(
        "bootstrap-guidelines", help="Harvest the full brand/category catalog from Jumia's guidelines workbook"
    )
    guidelines_parser.add_argument("--file", help="Path to the guidelines workbook (defaults to JUMIA_GUIDELINES_PATH)")
    guidelines_parser.set_defaults(func=cmd_bootstrap_guidelines)

    subparsers.add_parser("validate", help="Map + run rules against staged products").set_defaults(func=cmd_validate)

    export_parser = subparsers.add_parser("export", help="Write the approved rows from a validation run to xlsx")
    export_parser.add_argument("--run-id", type=int, help="Run to export (defaults to the latest completed run)")
    export_parser.set_defaults(func=cmd_export)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
