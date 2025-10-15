from __future__ import annotations

import logging

import discord
from discord import abc as discord_abc
from discord.ext import commands, tasks

from ..config import BotConfig
from ..f1 import find_next_session, format_session_channel_strings

logger = logging.getLogger(__name__)


class F1ClockCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig):
        self.bot = bot
        self.config = config
        self.clock_loop.start()
        self.bot.loop.create_task(self._update_once_ready())

    def cog_unload(self) -> None:
        self.clock_loop.cancel()

    @tasks.loop(minutes=5, reconnect=True)
    async def clock_loop(self) -> None:
        await self.update_channels()

    async def _update_once_ready(self) -> None:
        await self.bot.wait_until_ready()
        await self.update_channels()

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
                    try:
                        await channel.edit(name=target_name, reason="F1 next session update")
                    except Exception as exc:  # pragma: no cover - network failure
                        logger.exception("Failed to rename channel %s: %s", channel_id, exc)
        except Exception as exc:  # pragma: no cover
            logger.exception("Error updating F1 channels: %s", exc)
