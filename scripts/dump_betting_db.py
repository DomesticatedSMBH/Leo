#!/usr/bin/env python3
"""Dump the wallet betting database as Markdown tables."""

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

TOKEN_MULTIPLIER = 100
MONETARY_COLUMNS: frozenset[str] = frozenset({"amount", "balance", "balance_after", "payout"})


def from_cents(amount: int) -> float:
    """Convert the stored integer amount into FIT units."""

    return amount / TOKEN_MULTIPLIER


def format_value(column: str, value: object) -> str:
    if isinstance(value, int) and column in MONETARY_COLUMNS:
        return f"{from_cents(value):.2f} ({value})"
    return default_format_value(column, value)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db",
        type=Path,
        default=Path("wallet.sqlite"),
        help="Path to the wallet sqlite database (default: wallet.sqlite)",
    )
    args = parser.parse_args()

    if not args.db.exists():
        raise SystemExit(f"Database not found: {args.db}")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    try:
        tables = dump_all_tables(conn, formatter=format_value)
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
