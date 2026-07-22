"""Developer helper: report or apply schema migrations for a housekeeper database."""

import argparse
from pathlib import Path

from housekeeper.constants import SCHEMA_VERSION
from housekeeper.database import Database


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    db = Database(args.database)
    if args.dry_run:
        db.connect()
        current = db.fetch_one("SELECT COALESCE(MAX(version),0) FROM schema_migrations")
        print(
            {
                "current_version": current[0] if current else 0,
                "target_version": SCHEMA_VERSION,
                "backup_recommended": True,
            }
        )
    else:
        db.initialize()
        print(db.database_stats())


if __name__ == "__main__":
    main()
