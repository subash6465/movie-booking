"""Consistent SQLite backup; safe while the monitor is running."""

import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="data/watch/state.sqlite3")
    parser.add_argument("--directory", default="data/backups")
    args = parser.parse_args()
    source = Path(args.db).resolve()
    if not source.is_file():
        parser.error("Source database does not exist")
    folder = Path(args.directory)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ("movie-watch-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ") + ".sqlite3")
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src,
        closing(sqlite3.connect(target)) as dst,
    ):
        src.backup(dst)
        if dst.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RuntimeError("Backup integrity check failed")
    print(f"Backup verified: {target}")


if __name__ == "__main__":
    main()
