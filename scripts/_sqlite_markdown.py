#!/usr/bin/env python3
"""Helpers for dumping SQLite databases as Markdown tables."""

from __future__ import annotations

import sqlite3
from typing import Callable, Iterable

ValueFormatter = Callable[[str, object], str]


def iter_tables(conn: sqlite3.Connection) -> Iterable[str]:
    """Yield the non-internal table names in the database, sorted alphabetically."""

    cursor = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return (row[0] for row in cursor.fetchall())


def default_format_value(column: str, value: object) -> str:
    """Format a database value for presentation in Markdown."""

    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return value.hex()
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".")
    return str(value)


def dump_table(
    conn: sqlite3.Connection,
    table: str,
    *,
    formatter: ValueFormatter = default_format_value,
) -> str:
    """Return the given table formatted as a Markdown table."""

    cursor = conn.execute(f"SELECT * FROM {table}")
    rows = cursor.fetchall()
    if not rows:
        return f"## {table}\n\n_No rows._\n"

    columns = rows[0].keys()
    header = " | ".join(columns)
    separator = " | ".join(["---"] * len(columns))

    lines = [f"## {table}", "", f"| {header} |", f"| {separator} |"]
    for row in rows:
        formatted = [formatter(column, row[column]) for column in columns]
        lines.append(f"| {' | '.join(formatted)} |")
    lines.append("")
    return "\n".join(lines)


def dump_all_tables(
    conn: sqlite3.Connection,
    *,
    formatter: ValueFormatter = default_format_value,
) -> list[str]:
    """Dump every table in the database using the provided formatter."""

    return [dump_table(conn, table, formatter=formatter) for table in iter_tables(conn)]
