from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Optional

from toto_f1_api import TotoF1Client, canonical_key

TOKEN_MULTIPLIER = 100  # store FITs as integer cents


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_cents(amount: float) -> int:
    return int(round(amount * TOKEN_MULTIPLIER))


def from_cents(amount: int) -> float:
    return amount / TOKEN_MULTIPLIER


class WalletError(Exception):
    """Base class for wallet related errors."""


class InsufficientFundsError(WalletError):
    """Raised when a wallet has insufficient FITs for an operation."""


class InvalidAmountError(WalletError):
    """Raised when an amount is invalid."""


@dataclass
class TransactionRecord:
    user_id: int
    amount: int
    balance_after: int
    description: str
    created_at: datetime
    meta: Optional[str]


@dataclass
class BetRecord:
    id: int
    user_id: int
    market_id: int
    market_name: str
    outcome_name: str
    bet_type: str
    argument: str
    amount: int
    odds: float
    status: str
    created_at: datetime
    closes_at: Optional[datetime]
    closed_at: Optional[datetime]


class WalletStore:
    """Persistence layer for FIT wallets, bets and betting channel metadata."""

    def __init__(self, path: Path):
        self._path = path
        self._lock = threading.Lock()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._initialise()

    def _initialise(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS wallets (
                    user_id INTEGER PRIMARY KEY,
                    balance INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES wallets(user_id) ON DELETE CASCADE,
                    amount INTEGER NOT NULL,
                    balance_after INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    meta TEXT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS message_cooldowns (
                    user_id INTEGER PRIMARY KEY,
                    last_awarded REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS market_messages (
                    market_id INTEGER PRIMARY KEY,
                    channel_id INTEGER NOT NULL,
                    message_id INTEGER NOT NULL,
                    session_code TEXT NULL,
                    closes_at TEXT NULL,
                    event_name TEXT NULL,
                    is_closed INTEGER NOT NULL DEFAULT 0,
                    last_updated TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS bets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES wallets(user_id) ON DELETE CASCADE,
                    market_id INTEGER NOT NULL,
                    market_name TEXT NOT NULL,
                    outcome_name TEXT NOT NULL,
                    bet_type TEXT NOT NULL,
                    argument TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    odds REAL NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    closes_at TEXT NULL,
                    closed_at TEXT NULL,
                    payout INTEGER NULL,
                    notes TEXT NULL
                );
                """
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- wallet helpers -------------------------------------------------
    def _ensure_wallet_locked(self, cursor: sqlite3.Cursor, user_id: int) -> None:
        cursor.execute("INSERT OR IGNORE INTO wallets (user_id) VALUES (?)", (user_id,))

    def _add_transaction_locked(
        self,
        cursor: sqlite3.Cursor,
        user_id: int,
        amount: int,
        description: str,
        *,
        meta: Optional[dict] = None,
    ) -> int:
        if amount == 0:
            raise InvalidAmountError("Amount must be non-zero")
        cursor.execute("SELECT balance FROM wallets WHERE user_id=?", (user_id,))
        row = cursor.fetchone()
        if row is None:
            balance = 0
        else:
            balance = row["balance"]
        new_balance = balance + amount
        if new_balance < 0:
            raise InsufficientFundsError("Insufficient FITs for this operation")
        cursor.execute(
            "UPDATE wallets SET balance=? WHERE user_id=?",
            (new_balance, user_id),
        )
        cursor.execute(
            """
            INSERT INTO transactions (user_id, amount, balance_after, description, meta, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                amount,
                new_balance,
                description,
                json.dumps(meta) if meta is not None else None,
                utcnow().isoformat(),
            ),
        )
        return new_balance

    def try_award_message(self, user_id: int, at: datetime) -> bool:
        ts = at.timestamp()
        with self._lock:
            cursor = self._conn.cursor()
            cursor.execute(
                "SELECT last_awarded FROM message_cooldowns WHERE user_id=?",
                (user_id,),
            )
            row = cursor.fetchone()
            if row and ts - float(row["last_awarded"]) < 60:
                return False
            cursor.execute(
                """
                INSERT INTO message_cooldowns (user_id, last_awarded)
                VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET last_awarded=excluded.last_awarded
                """,
                (user_id, ts),
            )
            self._ensure_wallet_locked(cursor, user_id)
            self._add_transaction_locked(
                cursor,
                user_id,
                TOKEN_MULTIPLIER,
                "Message activity reward",
            )
            self._conn.commit()
            return True

    def add_tokens(self, user_id: int, amount: int, description: str) -> int:
        if amount <= 0:
            raise InvalidAmountError("Amount must be positive")
        with self._lock:
            cursor = self._conn.cursor()
            self._ensure_wallet_locked(cursor, user_id)
            new_balance = self._add_transaction_locked(cursor, user_id, amount, description)
            self._conn.commit()
            return new_balance

    def deduct_tokens(self, user_id: int, amount: int, description: str) -> int:
        if amount <= 0:
            raise InvalidAmountError("Amount must be positive")
        with self._lock:
            cursor = self._conn.cursor()
            self._ensure_wallet_locked(cursor, user_id)
            new_balance = self._add_transaction_locked(cursor, user_id, -amount, description)
            self._conn.commit()
            return new_balance

    def transfer_tokens(
        self,
        sender_id: int,
        recipient_id: int,
        amount: int,
    ) -> tuple[int, int]:
        if amount <= 0:
            raise InvalidAmountError("Amount must be positive")
        if sender_id == recipient_id:
            raise WalletError("Cannot transfer FITs to yourself")
        with self._lock:
            cursor = self._conn.cursor()
            self._ensure_wallet_locked(cursor, sender_id)
            self._ensure_wallet_locked(cursor, recipient_id)
            sender_balance = self._add_transaction_locked(
                cursor,
                sender_id,
                -amount,
                "FIT transfer",
                meta={"to": recipient_id},
            )
            recipient_balance = self._add_transaction_locked(
                cursor,
                recipient_id,
                amount,
                "FIT transfer",
                meta={"from": sender_id},
            )
            self._conn.commit()
            return sender_balance, recipient_balance

    def get_balance(self, user_id: int) -> int:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT balance FROM wallets WHERE user_id=?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row["balance"] if row else 0

    def recent_transactions(self, user_id: int, limit: int = 20) -> list[TransactionRecord]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT user_id, amount, balance_after, description, meta, created_at
                FROM transactions
                WHERE user_id=?
                ORDER BY id DESC
                LIMIT ?
                """,
                (user_id, limit),
            )
            records = []
            for row in cursor.fetchall():
                records.append(
                    TransactionRecord(
                        user_id=row["user_id"],
                        amount=row["amount"],
                        balance_after=row["balance_after"],
                        description=row["description"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        meta=row["meta"],
                    )
                )
            return records

    # --- betting metadata -----------------------------------------------
    def get_market_messages(self, channel_id: int) -> dict[int, int]:
        with self._lock:
            cursor = self._conn.execute(
                "SELECT market_id, message_id FROM market_messages WHERE channel_id=?",
                (channel_id,),
            )
            return {row["market_id"]: row["message_id"] for row in cursor.fetchall()}

    def upsert_market_message(
        self,
        market_id: int,
        channel_id: int,
        message_id: int,
        *,
        closes_at: Optional[datetime],
        session_code: Optional[str],
        event_name: Optional[str],
        is_closed: bool,
    ) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO market_messages (market_id, channel_id, message_id, closes_at, session_code, event_name, is_closed, last_updated)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(market_id) DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id,
                    closes_at=excluded.closes_at,
                    session_code=excluded.session_code,
                    event_name=excluded.event_name,
                    is_closed=excluded.is_closed,
                    last_updated=excluded.last_updated
                """,
                (
                    market_id,
                    channel_id,
                    message_id,
                    closes_at.isoformat() if closes_at else None,
                    session_code,
                    event_name,
                    1 if is_closed else 0,
                    utcnow().isoformat(),
                ),
            )
            self._conn.commit()

    def remove_market_message(self, market_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "DELETE FROM market_messages WHERE market_id=?",
                (market_id,),
            )
            self._conn.commit()

    def mark_market_closed(self, market_id: int) -> None:
        now_iso = utcnow().isoformat()
        with self._lock:
            self._conn.execute(
                "UPDATE market_messages SET is_closed=1, last_updated=? WHERE market_id=?",
                (now_iso, market_id),
            )
            self._conn.execute(
                """
                UPDATE bets
                SET status='closed', closed_at=?
                WHERE market_id=? AND status='open'
                """,
                (now_iso, market_id),
            )
            self._conn.commit()

    # --- bets -----------------------------------------------------------
    def create_bet(
        self,
        user_id: int,
        market_id: int,
        market_name: str,
        outcome_name: str,
        bet_type: str,
        argument: str,
        amount: int,
        odds: float,
        closes_at: Optional[datetime],
    ) -> int:
        if amount <= 0:
            raise InvalidAmountError("Amount must be positive")
        with self._lock:
            cursor = self._conn.cursor()
            self._ensure_wallet_locked(cursor, user_id)
            self._add_transaction_locked(
                cursor,
                user_id,
                -amount,
                f"Bet on {outcome_name}",
                meta={"market_id": market_id, "bet_type": bet_type},
            )
            cursor.execute(
                """
                INSERT INTO bets (
                    user_id, market_id, market_name, outcome_name, bet_type, argument, amount, odds, status, created_at, closes_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)
                """,
                (
                    user_id,
                    market_id,
                    market_name,
                    outcome_name,
                    bet_type,
                    argument,
                    amount,
                    odds,
                    utcnow().isoformat(),
                    closes_at.isoformat() if closes_at else None,
                ),
            )
            bet_id = cursor.lastrowid
            self._conn.commit()
            return int(bet_id)

    def list_open_bets(self, user_id: int) -> list[BetRecord]:
        with self._lock:
            cursor = self._conn.execute(
                """
                SELECT id, user_id, market_id, market_name, outcome_name, bet_type, argument, amount, odds, status, created_at, closes_at, closed_at
                FROM bets
                WHERE user_id=? AND status='open'
                ORDER BY created_at DESC
                """,
                (user_id,),
            )
            bets: list[BetRecord] = []
            for row in cursor.fetchall():
                bets.append(
                    BetRecord(
                        id=row["id"],
                        user_id=row["user_id"],
                        market_id=row["market_id"],
                        market_name=row["market_name"],
                        outcome_name=row["outcome_name"],
                        bet_type=row["bet_type"],
                        argument=row["argument"],
                        amount=row["amount"],
                        odds=row["odds"],
                        status=row["status"],
                        created_at=datetime.fromisoformat(row["created_at"]),
                        closes_at=datetime.fromisoformat(row["closes_at"]) if row["closes_at"] else None,
                        closed_at=datetime.fromisoformat(row["closed_at"]) if row["closed_at"] else None,
                    )
                )
            return bets


@dataclass
class OutcomeInfo:
    market_id: int
    selection_name: str
    odds_decimal: float
    implied_probability: float
    canonical_key: str


@dataclass
class MarketInfo:
    id: int
    name: str
    event_name: Optional[str]
    session_code: Optional[str]
    closes_at: Optional[datetime]
    is_closed: bool
    type_tags: set[str]
    outcomes: list[OutcomeInfo]


def normalise_market_type(name: str) -> set[str]:
    lower = name.lower()
    tags: set[str] = set()
    if "winner" in lower or "winnaar" in lower:
        tags.add("winner")
    if "top 3" in lower or "podium" in lower:
        tags.add("top3")
    if "top 6" in lower:
        tags.add("top6")
    if "top 10" in lower:
        tags.add("top10")
    if "qual" in lower or "kwal" in lower or "pole" in lower:
        tags.add("qualifying")
    if "sprint" in lower:
        tags.add("sprint")
    return tags


def determine_session_code(name: str) -> Optional[str]:
    lower = name.lower()
    if any(token in lower for token in ["fp1", "free practice 1"]):
        return "FP1"
    if any(token in lower for token in ["fp2", "free practice 2"]):
        return "FP2"
    if any(token in lower for token in ["fp3", "free practice 3"]):
        return "FP3"
    if "shootout" in lower or "sprint kwal" in lower:
        return "SQ"
    if "sprint" in lower and "shootout" not in lower:
        return "S"
    if any(token in lower for token in ["qual", "kwal", "pole"]):
        return "Q"
    if any(token in lower for token in ["race", "grand prix", "gp", "winner"]):
        return "R"
    return None


async def run_in_thread(func, *args, **kwargs):
    return await asyncio.to_thread(func, *args, **kwargs)


async def refresh_toto(client: TotoF1Client, mode: str = "requests") -> None:
    await run_in_thread(client.refresh, mode)


async def fetch_markets(client: TotoF1Client, market_ids: Iterable[int]):
    return await run_in_thread(
        lambda: {m_id: client.db.list_outcomes_latest(m_id) for m_id in market_ids}
    )

