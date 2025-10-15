from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

import discord
import pytz

from .config import BotConfig

logger = logging.getLogger(__name__)


@dataclass
class ScheduledJob:
    id: str
    kind: str
    guild_id: Optional[int]
    channel_id: int
    run_at: datetime
    created_by: int
    content: Optional[str] = None
    question: Optional[str] = None
    options: Optional[List[str]] = None
    emojis: Optional[List[str]] = None
    allow_multi: bool = False
    duration_s: Optional[int] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ScheduledJob":
        data = data.copy()
        data["run_at"] = datetime.fromisoformat(data["run_at"]).astimezone(pytz.utc)
        return cls(**data)

    def to_dict(self) -> Dict[str, Any]:
        data = self.__dict__.copy()
        data["run_at"] = self.run_at.isoformat()
        return data


class ScheduleManager:
    def __init__(self, config: BotConfig):
        self._config = config
        self._path = config.schedule_path
        self._jobs: list[ScheduledJob] = []

    @property
    def jobs(self) -> list[ScheduledJob]:
        return list(self._jobs)

    def load(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        if not self._path.exists():
            logger.info("No schedules file found at %s; starting fresh", self._path)
            self._jobs = []
            return
        try:
            with self._path.open("r", encoding="utf-8") as handle:
                raw_jobs = json.load(handle)
        except Exception as exc:
            logger.error("Failed to load schedules: %s", exc)
            self._jobs = []
            return
        self._jobs = [ScheduledJob.from_dict(job) for job in raw_jobs]
        logger.info("Loaded %d scheduled jobs", len(self._jobs))

    def save(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._path.open("w", encoding="utf-8") as handle:
                json.dump([job.to_dict() for job in self._jobs], handle, ensure_ascii=False, indent=2)
        except Exception as exc:
            logger.error("Failed to save schedules: %s", exc)

    def add_job(self, job: ScheduledJob) -> None:
        self._jobs.append(job)
        self.save()

    def remove_job(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [job for job in self._jobs if job.id != job_id]
        if len(self._jobs) < before:
            self.save()
            return True
        return False

    def due_jobs(self, now: datetime) -> Iterable[ScheduledJob]:
        due, remaining = [], []
        for job in self._jobs:
            if job.run_at <= now:
                due.append(job)
            else:
                remaining.append(job)
        if due:
            self._jobs = remaining
            self.save()
        return due


WHEN_FORMAT = "%d.%m.%Y %H:%M"
DURATION_RE = re.compile(r"(\d+)\s*([hdw])")


def parse_when(value: str, tz: pytz.BaseTzInfo) -> datetime:
    dt = datetime.strptime(value, WHEN_FORMAT)
    localized = tz.localize(dt)
    return localized.astimezone(pytz.utc)


def parse_duration(value: Optional[str], config: BotConfig) -> Optional[int]:
    if not value:
        return None
    tokens = DURATION_RE.findall(value.strip().lower())
    if not tokens:
        return None
    total_hours = 0
    for amount, unit in tokens:
        hours = int(amount)
        if unit == "h":
            total_hours += hours
        elif unit == "d":
            total_hours += hours * 24
        elif unit == "w":
            total_hours += hours * 7 * 24
    if total_hours <= 0:
        return None
    if total_hours > config.max_poll_hours:
        total_hours = config.max_poll_hours
    return total_hours * 3600


def build_poll(job: ScheduledJob) -> discord.Poll:
    if not job.duration_s:
        raise ValueError("Poll jobs must define a duration")
    poll = discord.Poll(
        question=job.question or "",
        duration=timedelta(seconds=job.duration_s),
        multiple=bool(job.allow_multi),
    )
    if not job.options:
        return poll
    for index, option in enumerate(job.options):
        kwargs: dict[str, Any] = {"text": option}
        if job.emojis and index < len(job.emojis) and job.emojis[index]:
            emoji_value = job.emojis[index]
            try:
                kwargs["emoji"] = discord.PartialEmoji.from_str(emoji_value)
            except Exception:
                kwargs["emoji"] = emoji_value
        poll.add_answer(**kwargs)
    return poll
