#!/usr/bin/env python3
"""Dump the wallet betting database as Markdown tables."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from typing import Iterable

TOKEN_MULTIPLIER = 100


def from_cents(amount: int) -> float:
    """Convert the stored integer amount into FIT units."""

    return amount / TOKEN_MULTIPLIER

MONETARY_COLUMNS: frozenset[str] = frozenset(
    {
        "amount",
        "balance",
        "balance_after",
        "payout",
    }
)


def iter_tables(conn: sqlite3.Connection) -> Iterable[str]:
    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return (row[0] for row in cursor.fetchall())


def format_value(column: str, value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    if isinstance(value, int) and column in MONETARY_COLUMNS:
        return f"{from_cents(value):.2f} ({value})"
    return str(value)


def dump_table(conn: sqlite3.Connection, table: str) -> str:
    cursor = conn.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    if not rows:
        return f"## {table}\n\n_No rows._\n"

    columns = rows[0].keys()
    header = " | ".join(columns)
    separator = " | ".join(["---"] * len(columns))

    lines = [f"## {table}", "", f"| {header} |", f"| {separator} |"]
    for row in rows:
        formatted = [format_value(column, row[column]) for column in columns]
        lines.append(f"| {' | '.join(formatted)} |")
    lines.append("")
    return "\n".join(lines)


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
        tables = list(iter_tables(conn))
        if not tables:
            print("No tables found.")
            return 0
        for table in tables:
            print(dump_table(conn, table))
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
