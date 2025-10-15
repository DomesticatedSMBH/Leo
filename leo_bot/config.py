from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

import discord
import pytz
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BotConfig:
    """Runtime configuration for the Discord bot."""

    token: str
    guild_id: int
    test_guild_id: int
    admin_ids: tuple[int, ...]
    ready_channel_id: int
    report_log_channel_id: int
    f1_channels: Mapping[str, int]
    schedule_path: Path
    default_timezone: pytz.BaseTzInfo
    betting_channel_id: Optional[int] = None
    max_poll_hours: int = 32 * 24
    f1_cache_path: Path = Path(".fastf1cache")
    toto_db_path: Path = Path("toto_f1.sqlite")
    wallet_db_path: Path = Path("wallet.sqlite")
    toto_requests_only: bool = False


def _require_env(name: str) -> str:
    try:
        value = os.environ[name]
    except KeyError as exc:
        raise RuntimeError(f"Missing required environment variable: {name}") from exc
    if not value:
        raise RuntimeError(f"Environment variable {name} is empty")
    return value


def _parse_int_env(name: str, *, default: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        if default is None:
            raise RuntimeError(f"Missing required environment variable: {name}")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _parse_admin_ids(*env_names: str) -> tuple[int, ...]:
    ids: list[int] = []
    for env in env_names:
        try:
            ids.append(_parse_int_env(env))
        except RuntimeError:
            logger.warning("Admin ID environment variable %s not set; skipping", env)
    if not ids:
        raise RuntimeError("At least one admin ID must be configured")
    return tuple(ids)


def _parse_optional_int_env(name: str) -> Optional[int]:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"Environment variable {name} must be an integer") from exc


def _parse_bool_env(name: str, *, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    normalized = raw.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def build_config() -> BotConfig:
    """Assemble :class:`BotConfig` from environment variables."""

    guild_id = _parse_int_env("DISCORD_GUILD")
    config = BotConfig(
        token=_require_env("DISCORD_TOKEN"),
        guild_id=guild_id,
        test_guild_id=_parse_int_env("DISCORD_TEST_GUILD"),
        admin_ids=_parse_admin_ids("STAFF_ID1", "STAFF_ID2"),
        ready_channel_id=_parse_int_env("READY_CHANNEL_ID", default=1421152351888085135),
        report_log_channel_id=_parse_int_env(
            "REPORT_LOG_CHANNEL_ID", default=1421156169308700874
        ),
        f1_channels={
            "eventname": _parse_int_env("F1_CHANNEL_EVENT", default=1425959683264610394),
            "date": _parse_int_env("F1_CHANNEL_DATE", default=1425959704307175494),
            "countdown": _parse_int_env("F1_CHANNEL_COUNTDOWN", default=1425959786062545099),
        },
        betting_channel_id=_parse_optional_int_env("BETTING_CHANNEL"),
        schedule_path=Path(os.getenv("SCHEDULES_PATH", "schedules.json")),
        default_timezone=pytz.utc,
        toto_db_path=Path(os.getenv("TOTO_F1_DB", "toto_f1.sqlite")),
        wallet_db_path=Path(os.getenv("WALLET_DB_PATH", "wallet.sqlite")),
        toto_requests_only=_parse_bool_env("TOTO_REQUESTS_ONLY", default=False),
    )
    return config


def build_intents() -> discord.Intents:
    intents = discord.Intents.all()
    intents.members = True
    intents.reactions = True
    return intents


DOMAIN_REPLACEMENTS: Mapping[str, str] = {
    "x.com": "fixupx.com",
    "instagram.com": "ddinstagram.com",
    "twitter.com": "vxtwitter.com",
    "reddit.com": "rxddit.com",
}
