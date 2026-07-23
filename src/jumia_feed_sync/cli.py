"""CLI entry point. Ingest/resolve/validate/export subcommands land in M0-M1."""

import argparse
import os

from jumia_feed_sync import db


def main() -> None:
    parser = argparse.ArgumentParser(prog="jumia-feed-sync")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("migrate", help="Apply pending SQLite migrations")

    args = parser.parse_args()

    if args.command == "migrate":
        db_path = os.environ.get("DB_PATH", "jumia_feed_sync.db")
        conn = db.connect(db_path)
        db.migrate(conn)
        print(f"Migrations applied to {db_path}")


if __name__ == "__main__":
    main()
