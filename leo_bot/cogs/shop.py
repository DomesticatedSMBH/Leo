from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from ..config import BotConfig


class ShopCog(commands.Cog):
    """FIT shop placeholder commands."""

    def __init__(self, bot: commands.Bot, config: BotConfig):
        self.bot = bot
        self.config = config

    @app_commands.command(name="shop", description="Browse the FIT shop")
    async def shop(self, interaction: discord.Interaction) -> None:
        """Placeholder command while the FIT shop is under construction."""
        embed = discord.Embed(
            title="FIT Shop",
            description="The shop feature is currently a work in progress.",
            color=0xFF9117,
        )
        embed.set_footer(text="Check back soon for goodies!")
        await interaction.response.send_message(embed=embed, ephemeral=True)

