from __future__ import annotations

import logging
import random
import re

import discord
from discord import app_commands
from discord.ext import commands

from ..config import BotConfig, DOMAIN_REPLACEMENTS

logger = logging.getLogger(__name__)


class ModerationCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig):
        self.bot = bot
        self.config = config
        escaped = "|".join(re.escape(domain) for domain in DOMAIN_REPLACEMENTS.keys())
        self._replacement_pattern = re.compile(
            rf"\bhttps://(www\.)?({escaped})\b", re.IGNORECASE
        )

    def _replace_domain(self, match: re.Match[str]) -> str:
        domain = match.group(2).lower()
        replacement = DOMAIN_REPLACEMENTS.get(domain)
        return f"https://{replacement}" if replacement else match.group(0)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author == self.bot.user:
            return
        await self._handle_domain_replacements(message)
        await self._handle_bot_mentions(message)

    async def _handle_domain_replacements(self, message: discord.Message) -> None:
        if not message.content:
            return
        if not self._replacement_pattern.search(message.content):
            return
        new_content = self._replacement_pattern.sub(self._replace_domain, message.content)
        display_name = message.author.display_name
        avatar_url = message.author.display_avatar.url if message.author.display_avatar else None
        webhook = await message.channel.create_webhook(name=display_name or message.author.name)
        try:
            await webhook.send(new_content, username=display_name, avatar_url=avatar_url)
        finally:
            await webhook.delete()
        await message.delete()

    async def _handle_bot_mentions(self, message: discord.Message) -> None:
        if not self.bot.user or self.bot.user not in message.mentions:
            return
        parts = message.content.split(maxsplit=3)
        if len(parts) >= 4 and parts[1].lower() == "delete":
            if message.author.id not in self.config.admin_ids:
                return
            try:
                channel_id = int(parts[2])
                message_id = int(parts[3])
            except ValueError:
                await message.reply("Invalid IDs provided.")
                return
            channel = self.bot.get_channel(channel_id)
            if not isinstance(channel, (discord.TextChannel, discord.Thread)):
                await message.reply("Invalid channel ID.")
                return
            try:
                target_message = await channel.fetch_message(message_id)
            except discord.NotFound:
                await message.reply("Message not found.")
                return
            await target_message.delete()
            await message.delete()
            return
        if message.author.id in self.config.admin_ids:
            return
        responses = ["Woof?", "Bark!", "Arf arf!", "Grrr...", "Yip!"]
        await message.reply(random.choice(responses))

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.emoji.name != "✉️":
            return
        channel = self.bot.get_channel(payload.channel_id)
        if not isinstance(channel, discord.TextChannel):
            return
        try:
            message = await channel.fetch_message(payload.message_id)
        except discord.NotFound:
            return
        user = payload.member or self.bot.get_user(payload.user_id)
        if not user:
            return
        try:
            if message.content:
                await user.send(message.content)
            for attachment in message.attachments:
                await user.send(attachment.url)
        except discord.HTTPException:
            logger.exception("Failed to forward message via DM")

    @app_commands.context_menu(name="Report Message")
    async def report_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        await interaction.response.send_message(
            f"This message by {message.author.mention} has been reported to our staff.",
            ephemeral=True,
        )
        log_channel = interaction.guild.get_channel(self.config.report_log_channel_id) if interaction.guild else None
        if not isinstance(log_channel, discord.TextChannel):
            logger.warning("Report log channel %s not found", self.config.report_log_channel_id)
            return
        embed = discord.Embed(title="Reported Message")
        if message.content:
            embed.description = message.content
        if message.attachments:
            embed.set_thumbnail(url=message.attachments[0].url)
        embed.set_author(name=message.author.display_name, icon_url=message.author.display_avatar.url)
        embed.timestamp = message.created_at
        view = discord.ui.View()
        view.add_item(
            discord.ui.Button(
                label="Go to Message", style=discord.ButtonStyle.url, url=message.jump_url
            )
        )
        await log_channel.send(embed=embed, view=view)

    @app_commands.context_menu(name="Forward Message to DMs")
    async def forward_message(self, interaction: discord.Interaction, message: discord.Message) -> None:
        forwarded = False
        try:
            if message.content:
                await interaction.user.send(message.content)
                forwarded = True
            for attachment in message.attachments:
                await interaction.user.send(attachment.url)
                forwarded = True
        except discord.HTTPException:
            forwarded = False
        if forwarded:
            await interaction.response.send_message(
                f"Successfully forwarded {message.author.mention}'s message to your DMs.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "Something went wrong.", ephemeral=True
            )
