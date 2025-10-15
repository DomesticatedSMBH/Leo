from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks
import pytz

from ..config import BotConfig
from ..scheduler import ScheduleManager, ScheduledJob, build_poll, parse_duration, parse_when

logger = logging.getLogger(__name__)


class ScheduleCog(commands.Cog):
    def __init__(self, bot: commands.Bot, config: BotConfig, manager: ScheduleManager):
        self.bot = bot
        self.config = config
        self.manager = manager
        self.scheduler_loop.start()

    def cog_unload(self) -> None:
        self.scheduler_loop.cancel()

    @tasks.loop(seconds=30)
    async def scheduler_loop(self) -> None:
        now = datetime.utcnow().replace(tzinfo=pytz.utc)
        for job in list(self.manager.due_jobs(now)):
            await self._execute_job(job)

    async def _execute_job(self, job: ScheduledJob) -> None:
        channel = self.bot.get_channel(job.channel_id)
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Scheduled channel %s not found", job.channel_id)
            return
        try:
            if job.kind == "message" and job.content is not None:
                await channel.send(job.content)
            elif job.kind == "poll":
                poll = build_poll(job)
                await channel.send(poll=poll)
            else:
                logger.error("Unknown job kind: %s", job.kind)
        except Exception as exc:  # pragma: no cover - network failures
            logger.exception("Failed to execute scheduled job %s: %s", job.id, exc)

    @scheduler_loop.before_loop
    async def before_scheduler_loop(self) -> None:
        await self.bot.wait_until_ready()

    @app_commands.command(name="schedule", description="Schedule a message or poll")
    @app_commands.guild_only()
    @app_commands.describe(
        kind="What to schedule (message or poll)",
        when="Time in dd.mm.yyyy hh:mm",
        channel="Channel to send into",
        content="Message content or poll question",
        answers="Comma-separated list of poll answers",
        emojis="Optional emojis for poll answers",
        multi="Allow multiple answers",
        duration="Poll duration (Nh/Nd/Nw)",
    )
    async def schedule(
        self,
        interaction: discord.Interaction,
        kind: Literal["message", "poll"] = "message",
        when: str = None,
        channel: Optional[discord.TextChannel] = None,
        content: Optional[str] = None,
        answers: Optional[str] = None,
        emojis: Optional[str] = None,
        multi: bool = False,
        duration: Optional[str] = None,
    ) -> None:
        if interaction.user.id not in self.config.admin_ids:
            await interaction.response.send_message("Unauthorised.", ephemeral=True)
            return
        try:
            run_at_utc = parse_when(when, self.config.default_timezone)
        except Exception:
            await interaction.response.send_message(
                "Invalid time. Use dd.mm.yyyy hh:mm.", ephemeral=True
            )
            return
        kind = kind or "message"
        target_channel = channel or interaction.channel
        if not isinstance(target_channel, discord.TextChannel):
            await interaction.response.send_message(
                "Target must be a text channel.", ephemeral=True
            )
            return
        job_id = f"{int(datetime.utcnow().timestamp() * 1000)}-{interaction.id}"
        job = ScheduledJob(
            id=job_id,
            kind=kind,
            guild_id=interaction.guild.id if interaction.guild else None,
            channel_id=target_channel.id,
            run_at=run_at_utc,
            created_by=interaction.user.id,
        )
        if kind == "message":
            if content is None or not content.strip():
                await interaction.response.send_message(
                    "Provide message text for scheduled messages.",
                    ephemeral=True,
                )
                return
            job.content = content.strip()
        else:
            if not answers:
                await interaction.response.send_message(
                    "Provide answers for the poll (comma-separated).",
                    ephemeral=True,
                )
                return
            options = [answer.strip() for answer in answers.split(",") if answer.strip()]
            if len(options) < 2 or len(options) > 25:
                await interaction.response.send_message(
                    "Poll needs 2–25 answers.", ephemeral=True
                )
                return
            emoji_list = []
            if emojis:
                emoji_list = [emoji.strip() for emoji in emojis.split(",")]
                if len(emoji_list) != len(options):
                    await interaction.response.send_message(
                        "Number of emojis must match number of answers.",
                        ephemeral=True,
                    )
                    return
            duration_seconds = parse_duration(duration or "24h", self.config)
            if not duration_seconds:
                await interaction.response.send_message(
                    "Invalid duration. Usage: e.g., 1h or 48h or 3w5d13h; max. 32d",
                    ephemeral=True,
                )
                return
            job.question = content
            job.options = options
            job.emojis = emoji_list
            job.allow_multi = bool(multi)
            job.duration_s = duration_seconds
        self.manager.add_job(job)
        local_time = run_at_utc.astimezone(self.config.default_timezone)
        await interaction.response.send_message(
            f"Scheduled {kind} for <#{target_channel.id}> at {local_time:%d.%m.%Y %H:%M UTC}. ID: `{job.id}`",
            ephemeral=True,
        )

    @app_commands.command(name="schedule_list", description="List scheduled items")
    @app_commands.guild_only()
    async def schedule_list(self, interaction: discord.Interaction) -> None:
        if interaction.user.id not in self.config.admin_ids:
            await interaction.response.send_message("Unauthorised.", ephemeral=True)
            return
        jobs = self.manager.jobs
        if not jobs:
            await interaction.response.send_message("No scheduled items.", ephemeral=True)
            return
        embed = discord.Embed(title="Scheduled Items", color=0x00AAFF)
        for job in jobs:
            local_dt = job.run_at.astimezone(self.config.default_timezone)
            embed.add_field(
                name=f"ID: {job.id}",
                value=(
                    f"**Type:** {job.kind}\n"
                    f"**Channel:** <#{job.channel_id}>\n"
                    f"**When:** {local_dt:%d.%m.%Y %H:%M}"
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="schedule_remove", description="Remove a scheduled item")
    @app_commands.guild_only()
    async def schedule_remove(self, interaction: discord.Interaction, job_id: str) -> None:
        if interaction.user.id not in self.config.admin_ids:
            await interaction.response.send_message("Unauthorised.", ephemeral=True)
            return
        if self.manager.remove_job(job_id):
            await interaction.response.send_message(
                f"Removed scheduled job `{job_id}`.", ephemeral=True
            )
        else:
            await interaction.response.send_message(
                f"No scheduled job found with ID `{job_id}`.", ephemeral=True
            )
