from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta

import discord
from discord import abc as discord_abc
from discord.ext import commands

from ..config import BotConfig
from ..f1 import find_next_session, format_session_channel_strings

logger = logging.getLogger(__name__)


class F1ClockCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig):
        self.bot = bot
        self.config = config
        self._rename_tasks: dict[int, tuple[str, asyncio.Task[None]]] = {}
        self._clock_task: asyncio.Task[None] | None = None

    async def cog_load(self) -> None:
        self._clock_task = asyncio.create_task(self._clock_loop())

    def cog_unload(self) -> None:
        if self._clock_task is not None:
            self._clock_task.cancel()
            self._clock_task = None
        for _, task in self._rename_tasks.values():
            task.cancel()
        self._rename_tasks.clear()

    async def _clock_loop(self) -> None:
        await self.bot.wait_until_ready()
        first_run = True
        while True:
            try:
                if not first_run:
                    await self._sleep_until_next_mark()
                    logger.info(
                        "Running scheduled F1 clock update at %s",
                        datetime.utcnow().isoformat(),
                    )
                else:
                    first_run = False
                await self.update_channels()
            except asyncio.CancelledError:
                raise
            except Exception:  # pragma: no cover - defensive loop guard
                logger.exception("F1 clock loop encountered an error; continuing")

    async def _sleep_until_next_mark(self) -> None:
        now = datetime.utcnow()
        next_run = now.replace(second=0, microsecond=0)
        minutes_to_add = (10 - (now.minute % 10)) % 10
        next_run += timedelta(minutes=minutes_to_add)
        if next_run <= now:
            next_run += timedelta(minutes=10)
        sleep_seconds = (next_run - now).total_seconds()
        logger.debug(
            "Next F1 clock update scheduled for %s (in %.2f seconds)",
            next_run.isoformat(),
            sleep_seconds,
        )
        await asyncio.sleep(sleep_seconds)

    async def update_channels(self) -> None:
        try:
            event, session_code, session_dt = find_next_session(self.config.default_timezone)
            if event is None or session_code is None or session_dt is None:
                logger.info("No upcoming session found.")
                return
            eventname, date_str, time_str, countdown_str = format_session_channel_strings(
                event, session_code, session_dt, self.config.default_timezone
            )
            desired = {
                self.config.f1_channels["eventname"]: eventname,
                self.config.f1_channels["date"]: f"{date_str} • {time_str}",
                self.config.f1_channels["countdown"]: countdown_str,
            }
            for channel_id, target_name in desired.items():
                channel = self.bot.get_channel(channel_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(channel_id)
                    except discord.NotFound:
                        logger.warning("F1 clock channel %s not found", channel_id)
                        continue
                    except discord.DiscordException as exc:  # pragma: no cover - network failure
                        logger.exception("Failed to fetch channel %s: %s", channel_id, exc)
                        continue
                if not isinstance(channel, discord_abc.GuildChannel):
                    logger.warning("F1 clock channel %s not found", channel_id)
                    continue
                if channel.name != target_name:
                    self._schedule_channel_rename(channel, channel_id, target_name)
        except Exception as exc:  # pragma: no cover
            logger.exception("Error updating F1 channels: %s", exc)

    def _schedule_channel_rename(
        self,
        channel: discord_abc.GuildChannel,
        channel_id: int,
        target_name: str,
    ) -> None:
        if channel_id in self._rename_tasks:
            previous_target, previous_task = self._rename_tasks[channel_id]
            if previous_target == target_name and not previous_task.done():
                return
            previous_task.cancel()

        async def _runner() -> None:
            try:
                await channel.edit(name=target_name, reason="F1 next session update")
            except asyncio.CancelledError:  # pragma: no cover - cancellation during shutdown
                raise
            except Exception as exc:  # pragma: no cover - network failure
                logger.exception("Failed to rename channel %s: %s", channel_id, exc)
            finally:
                current_task = asyncio.current_task()
                stored = self._rename_tasks.get(channel_id)
                if stored is not None and stored[1] is current_task:
                    self._rename_tasks.pop(channel_id, None)

        task = asyncio.create_task(_runner())
        self._rename_tasks[channel_id] = (target_name, task)
