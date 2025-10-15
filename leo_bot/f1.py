from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import fastf1
import pytz

from .config import BotConfig

logger = logging.getLogger(__name__)

_SESSION_ORDER = ["FP1", "FP2", "FP3", "SQ", "S", "Q", "R"]
_SESSION_LABELS = {
    "FP1": "FP1",
    "FP2": "FP2",
    "FP3": "FP3",
    "SQ": "Sprint Quali",
    "S": "Sprint",
    "Q": "Quali",
    "R": "GRAND PRIX",
}
MAX_CHANNEL_NAME = 100


def initialise_cache(config: BotConfig) -> None:
    cache_path = config.f1_cache_path
    if not cache_path.exists():
        cache_path.mkdir(parents=True, exist_ok=True)
        logger.info("Created fastf1 cache directory at %s", cache_path)
    fastf1.Cache.enable_cache(str(cache_path))


def to_utc(dt: Any) -> Optional[datetime]:
    if dt is None:
        return None
    py_dt = dt.to_pydatetime() if hasattr(dt, "to_pydatetime") else dt
    if py_dt.tzinfo is None:
        return py_dt.replace(tzinfo=timezone.utc)
    return py_dt.astimezone(timezone.utc)


def format_local(dt_utc: datetime, tz: pytz.BaseTzInfo) -> str:
    return dt_utc.astimezone(tz).strftime("%a %d %b %Y • %H:%M UTC")


def countdown(dt_utc: datetime) -> str:
    now = datetime.now(timezone.utc)
    remaining = int((dt_utc - now).total_seconds())
    if remaining <= 0:
        return "started"
    days, remainder = divmod(remaining, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if days or hours:
        parts.append(f"{hours}h")
    parts.append(f"<{minutes}m")
    return "in " + " ".join(parts)


def add_session_fields(embed, event, identifiers: Iterable[str], label: str, tz: pytz.BaseTzInfo) -> None:
    for ident in identifiers:
        try:
            dt = event.get_session_date(ident, utc=True)
        except Exception:  # pragma: no cover - defensive, matches legacy behaviour
            logger.debug("Unable to retrieve session %s for event %s", ident, event)
            continue
        dt_utc = to_utc(dt)
        if dt_utc is None:
            continue
        embed.add_field(
            name=label,
            value=f"{format_local(dt_utc, tz)}\n{countdown(dt_utc)}",
            inline=False,
        )
        return


def _is_testing_row(row) -> bool:
    fields = []
    for column in ("EventName", "OfficialEventName", "EventFormat", "EventType", "Name"):
        if column in row:
            fields.append(str(row[column]))
    return "test" in " ".join(fields).lower()


def _iter_race_rounds(schedule) -> list[int]:
    rounds = []
    for value in schedule["RoundNumber"]:
        text = str(value).strip()
        if text.isdigit():
            rounds.append(int(text))
    return sorted(set(rounds))


def _iter_existing_sessions(event, *, utc: bool = True):
    for code in _SESSION_ORDER:
        try:
            dt = event.get_session_date(code, utc=utc)
        except Exception:  # pragma: no cover - defensive
            dt = None
        if dt:
            yield code, to_utc(dt)


def _short_event_label(event: dict) -> str:
    name = (event.get("EventName") or event.get("OfficialEventName") or "").strip()
    country = (event.get("Country") or "").strip()
    location = (event.get("Location") or "").strip()
    lower_name = name.lower()
    lower_location = location.lower()

    if "emilia" in lower_name or "romagna" in lower_name or "imola" in lower_location:
        return "Imola"
    if ("italian" in lower_name or "italy" in lower_name) and "monza" in lower_location:
        return "Monza"
    if "united states" in lower_name and ("austin" in lower_location or "cota" in lower_name):
        return "CIRCUIT OF THE AMERICAS"
    if "united states" in lower_name and "miami" in lower_location:
        return "Miami International Autodrome"
    if "united states" in lower_name and "las vegas" in lower_location:
        return "Las Vegas Street Circuit"
    if "british" in lower_name and "silverstone" in lower_location:
        return "Silverstone"
    return country or name or "Grand Prix"


def _format_session_strings(event, session_code: str, session_dt_utc: datetime, tz: pytz.BaseTzInfo) -> tuple[str, str, str, str]:
    short_event = _short_event_label(event)
    session_label = _SESSION_LABELS.get(session_code, session_code)
    local_dt = session_dt_utc.astimezone(tz)

    name = f"{short_event} – {session_label}"
    date_str = local_dt.strftime("%a %d %b %Y")
    time_str = local_dt.strftime("%H:%M UTC")
    countdown_str = countdown(session_dt_utc)

    clip = lambda value: value[:MAX_CHANNEL_NAME]
    return clip(name), clip(date_str), clip(time_str), clip(countdown_str)


def _format_race_strings(event, race_dt_utc: datetime, tz: pytz.BaseTzInfo) -> tuple[str, str, str, str]:
    name = event.get("OfficialEventName") or event.get("EventName") or "Grand Prix"
    local_dt = race_dt_utc.astimezone(tz)
    date_str = local_dt.strftime("%a %d %b %Y")
    time_str = local_dt.strftime("%H:%M UTC")
    countdown_str = countdown(race_dt_utc)

    clip = lambda value: value[:MAX_CHANNEL_NAME]
    return clip(name), clip(date_str), clip(time_str), clip(countdown_str)


def find_next_session(tz: pytz.BaseTzInfo) -> tuple[Any, Optional[str], Optional[datetime]]:
    now = datetime.now(timezone.utc)
    year = now.year
    for _ in range(2):
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
        except TypeError:  # older fastf1 versions
            schedule = fastf1.get_event_schedule(year)
        filtered = schedule.loc[~schedule.apply(_is_testing_row, axis=1)]
        candidates = []
        for rnd in _iter_race_rounds(filtered):
            try:
                event = schedule.get_event_by_round(rnd)
            except Exception:  # pragma: no cover - defensive
                continue
            for code, dt_utc in _iter_existing_sessions(event, utc=True):
                if dt_utc and dt_utc > now:
                    order = _SESSION_ORDER.index(code)
                    candidates.append((dt_utc, order, code, event))
        if candidates:
            dt_utc, _, code, event = min(candidates, key=lambda item: (item[0], item[1]))
            return event, code, dt_utc
        year += 1
    return None, None, None


def find_next_race(tz: pytz.BaseTzInfo):
    now = datetime.now(timezone.utc)
    year = now.year
    for _ in range(2):
        try:
            schedule = fastf1.get_event_schedule(year, include_testing=False)
        except TypeError:
            schedule = fastf1.get_event_schedule(year)
        filtered = schedule.loc[~schedule.apply(_is_testing_row, axis=1)]
        candidates = []
        for rnd in _iter_race_rounds(filtered):
            try:
                event = schedule.get_event_by_round(rnd)
            except Exception as exc:
                if "testing" in str(exc).lower():
                    continue
                continue
            race_dt = event.get_session_date("R", utc=True)
            race_dt_utc = to_utc(race_dt)
            if race_dt_utc and race_dt_utc > now:
                candidates.append((race_dt_utc, event))
        if candidates:
            race_dt_utc, event = min(candidates, key=lambda item: item[0])
            return event, race_dt_utc
        year += 1
    return None, None


def format_f1_channel_strings(event, race_dt_utc: datetime, tz: pytz.BaseTzInfo) -> tuple[str, str, str, str]:
    return _format_race_strings(event, race_dt_utc, tz)


def format_session_channel_strings(event, session_code: str, session_dt_utc: datetime, tz: pytz.BaseTzInfo) -> tuple[str, str, str, str]:
    return _format_session_strings(event, session_code, session_dt_utc, tz)
