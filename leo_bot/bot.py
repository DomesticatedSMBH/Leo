from __future__ import annotations

import logging

import discord
from discord.ext import commands

from .config import BotConfig, build_config, build_intents
from .f1 import initialise_cache
from .scheduler import ScheduleManager
from .cogs.betting import BettingCog
from .cogs.f1_clock import F1ClockCog
from .cogs.moderation import ModerationCog
from .cogs.scheduler import ScheduleCog
from .cogs.shop import ShopCog

logger = logging.getLogger(__name__)


class LeoBot(commands.Bot):
    def __init__(self, config: BotConfig):
        super().__init__(command_prefix="!", intents=build_intents())
        self.config = config
        self.schedule_manager = ScheduleManager(config)
        self.schedule_manager.load()
        self._ready_notified = False

    async def setup_hook(self) -> None:
        # Remove any previously registered global commands before loading the
        # cogs so that we can perform clean synchronisation afterwards.
        self.tree.clear_commands(guild=None)

        await self.add_cog(ScheduleCog(self, self.config, self.schedule_manager))
        await self.add_cog(F1ClockCog(self, self.config))
        await self.add_cog(BettingCog(self, self.config))
        await self.add_cog(ShopCog(self, self.config))
        await self.add_cog(ModerationCog(self, self.config))

        guild_ids = [
            guild_id
            for guild_id in (self.config.guild_id, self.config.test_guild_id)
            if guild_id is not None
        ]

        # Ensure global commands – such as /f1_next – remain registered so they
        # are available across every guild.
        await self.tree.sync()

        for guild_id in guild_ids:
            await self.tree.sync(guild=discord.Object(id=guild_id))

    async def on_ready(self) -> None:
        await self.change_presence(activity=discord.Game("with Charles"))
        if not self._ready_notified:
            await self.send_ready_message()
            self._ready_notified = True
        logger.info("%s is ready!", self.user)

    async def send_ready_message(self) -> None:
        channel = self.get_channel(self.config.ready_channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Ready channel %s not found", self.config.ready_channel_id)
            return
        embed = discord.Embed(title="Leo is up and ready!", color=0xFF9117)
        await channel.send(embed=embed)


def run_bot() -> None:
    logging.basicConfig(level=logging.INFO)
    config = build_config()
    initialise_cache(config)
    bot = LeoBot(config)
    bot.run(config.token)
