#!/usr/bin/env python3
"""Dump the Toto API database as Markdown tables."""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    sys.path.append(str(Path(__file__).resolve().parent))
    from _sqlite_markdown import default_format_value, dump_all_tables
else:  # pragma: no cover - executed when invoked as a module
    from ._sqlite_markdown import default_format_value, dump_all_tables


def _toto_formatter(column: str, value: object) -> str:
    """Format Toto DB values, trimming large HTML blobs."""

    if column == "html" and isinstance(value, str):
        # The Toto snapshots table stores the raw fetched markup, which quickly
        # becomes unwieldy in a Markdown dump. Replace the body with a compact
        # placeholder that still advertises how much HTML was captured.
        return f"<html {len(value)} chars>"
    return default_format_value(column, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("toto_f1.sqlite"),
        help="Path to the Toto F1 sqlite database (default: toto_f1.sqlite)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        tables = dump_all_tables(conn, formatter=_toto_formatter)
        if not tables:
            print("No tables found.")
            return 0
        for table in tables:
            print(table)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
