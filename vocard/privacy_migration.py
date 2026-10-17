"""Explicit local administrator migration; never called by the request path."""

import argparse
import sqlite3
from pathlib import Path


def remove_legacy_conversations(path: Path, *, confirmed: bool = False):
    if not confirmed:
        raise ValueError("Explicit confirmation is required to delete legacy conversations")
    # Existing DB only. Do not silently create a database at a mistyped path.
    with sqlite3.connect(path.resolve().as_uri() + "?mode=rw", uri=True) as con:
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("PRAGMA secure_delete=ON")
        con.execute("BEGIN EXCLUSIVE")
        con.execute("DROP TABLE IF EXISTS ai_conversations")
        con.commit()
        result = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0] != 0:
            raise RuntimeError("WAL checkpoint busy; stop all processes and repeat migration")
        con.execute("VACUUM")
        result = con.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0] != 0:
            raise RuntimeError("Final WAL checkpoint busy; stop all processes and repeat migration")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database", type=Path)
    parser.add_argument(
        "--confirm-delete-conversations",
        action="store_true",
        help="Irreversibly remove legacy messages; stop all API/worker processes first",
    )
    args = parser.parse_args()
    if not args.confirm_delete_conversations:
        parser.error("Deletion requires --confirm-delete-conversations")
    remove_legacy_conversations(args.database, confirmed=True)
    print("Legacy conversation table removed; book data and usage counters retained.")


if __name__ == "__main__":
    main()
