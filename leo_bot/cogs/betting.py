from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from ..betting import (
    MarketInfo,
    OutcomeInfo,
    WalletError,
    WalletStore,
    determine_session_code,
    fetch_markets,
    from_cents,
    normalise_market_type,
    refresh_toto,
    run_in_thread,
    to_cents,
)
from ..config import BotConfig
from ..f1 import countdown, format_local, to_utc
from .. import f1
from toto_f1_api import TotoF1Client, canonical_key

logger = logging.getLogger(__name__)

MAX_MARKETS_DISPLAYED = 10


def _split_market_name(name: str) -> tuple[Optional[str], str]:
    separators = [" – ", " — ", " - ", ": "]
    for sep in separators:
        if sep in name:
            head, tail = name.split(sep, 1)
            head = head.strip()
            tail = tail.strip()
            if head:
                return head, tail or name
    return None, name


class BettingCog(commands.Cog):
    """FIT currency, wallet commands and Toto betting replication."""

    wallet = app_commands.Group(name="wallet", description="Manage your FIT wallet")

    def __init__(self, bot: commands.Bot, config: BotConfig):
        self.bot = bot
        self.config = config
        self._toto = TotoF1Client(db_path=str(config.toto_db_path))
        self._wallets = WalletStore(config.wallet_db_path)
        self._latest_markets: list[MarketInfo] = []
        self._last_cache_at: Optional[datetime] = None
        self._cache_ttl = timedelta(minutes=45)

    async def cog_load(self) -> None:
        if self.config.betting_channel_id:
            await self.bot.wait_until_ready()
            await self.sync_betting_channel()
            self.hourly_update.start()
        else:
            logger.warning("BETTING_CHANNEL not configured; betting channel updates disabled")

    def cog_unload(self) -> None:
        if self.hourly_update.is_running():
            self.hourly_update.cancel()
        self._wallets.close()
        self._toto.close()

    @tasks.loop(hours=1)
    async def hourly_update(self) -> None:
        await self.sync_betting_channel()

    @hourly_update.error
    async def hourly_update_error(self, exc: Exception) -> None:  # pragma: no cover - logging only
        logger.exception("Hourly betting update failed: %s", exc)

    async def sync_betting_channel(self) -> None:
        channel_id = self.config.betting_channel_id
        if not channel_id:
            return
        channel = self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                logger.warning("Unable to fetch betting channel %s", channel_id)
                return
        if not isinstance(channel, discord.TextChannel):
            logger.warning("Configured betting channel %s is not a text channel", channel_id)
            return

        markets = await self._refresh_market_cache(force=True)
        if not markets:
            return

        existing = await run_in_thread(self._wallets.get_market_messages, channel.id)
        updated: set[int] = set()

        for market in markets[:MAX_MARKETS_DISPLAYED]:
            embed = self._build_market_embed(market)
            message_id = existing.get(market.id)
            message: Optional[discord.Message] = None
            if message_id:
                try:
                    message = await channel.fetch_message(message_id)
                    await message.edit(embed=embed)
                except discord.NotFound:
                    message = None
                except discord.HTTPException:
                    logger.exception("Failed to edit betting message %s", message_id)
            if message is None:
                try:
                    message = await channel.send(embed=embed)
                except discord.HTTPException:
                    logger.exception("Failed to send betting embed for market %s", market.name)
                    continue
            updated.add(market.id)
            if market.is_closed:
                await run_in_thread(self._wallets.mark_market_closed, market.id)
            await run_in_thread(
                self._wallets.upsert_market_message,
                market.id,
                channel.id,
                message.id,
                closes_at=market.closes_at,
                session_code=market.session_code,
                event_name=market.event_name,
                is_closed=market.is_closed,
            )

        for market_id, message_id in existing.items():
            if market_id in updated:
                continue
            try:
                message = await channel.fetch_message(message_id)
            except discord.NotFound:
                message = None
            except discord.HTTPException:
                message = None
            if message is not None:
                try:
                    await message.delete()
                except discord.HTTPException:
                    logger.debug("Failed to delete betting message %s", message_id)
            await run_in_thread(self._wallets.remove_market_message, market_id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or not message.guild:
            return
        try:
            await run_in_thread(
                self._wallets.try_award_message,
                message.author.id,
                message.created_at or datetime.now(timezone.utc),
            )
        except WalletError:
            logger.debug("Failed to award FIT for message from %s", message.author.id)

    def _build_market_embed(self, market: MarketInfo) -> discord.Embed:
        color = 0x3D85C6 if not market.is_closed else 0x7F8C8D
        embed = discord.Embed(title=market.name, color=color)
        if market.event_name:
            embed.description = market.event_name
        if market.closes_at:
            local = format_local(market.closes_at, self.config.default_timezone)
            embed.add_field(
                name="Closes",
                value=f"{local}\n{countdown(market.closes_at)}",
                inline=False,
            )
        outcomes = sorted(market.outcomes, key=lambda o: o.odds_decimal)
        lines = []
        for outcome in outcomes[:10]:
            prob = outcome.implied_probability * 100
            lines.append(
                f"**{outcome.selection_name}** — {outcome.odds_decimal:.2f} ({prob:.1f}%)"
            )
        if lines:
            embed.add_field(name="Outcomes", value="\n".join(lines), inline=False)
        else:
            embed.add_field(name="Outcomes", value="No odds available", inline=False)
        status = "Closed" if market.is_closed else "Open"
        embed.set_footer(text=f"Status: {status}")
        return embed

    async def _refresh_market_cache(self, *, force: bool = False) -> list[MarketInfo]:
        now = datetime.now(timezone.utc)
        if (
            not force
            and self._last_cache_at is not None
            and now - self._last_cache_at < self._cache_ttl
        ):
            return self._latest_markets
        try:
            await refresh_toto(self._toto, mode="requests")
        except Exception:
            logger.exception("Failed to refresh Toto data")
        markets = await run_in_thread(self._toto.list_markets)
        market_ids = [market.id for market in markets]
        if not market_ids:
            self._latest_markets = []
            self._last_cache_at = now
            return []
        outcomes_map = await fetch_markets(self._toto, market_ids)
        processed = await run_in_thread(
            self._build_market_infos,
            markets,
            outcomes_map,
        )
        self._latest_markets = processed
        self._last_cache_at = now
        return processed

    def _build_market_infos(
        self,
        markets,
        outcomes_map: dict[int, Iterable],
    ) -> list[MarketInfo]:
        now = datetime.now(timezone.utc)
        event, _, _ = f1.find_next_session(self.config.default_timezone)
        next_event_keys: set[str] = set()
        event_obj = None
        if event:
            event_obj = event
            for key in (
                getattr(event, "EventName", None),
                getattr(event, "OfficialEventName", None),
                getattr(event, "Location", None),
                getattr(event, "Country", None),
            ):
                if key:
                    next_event_keys.add(canonical_key(str(key)))

        infos: list[MarketInfo] = []
        for market in markets:
            raw_name = market.name
            event_name, display_name = _split_market_name(raw_name)
            session_code = determine_session_code(display_name)
            closes_at = None
            related_event = None
            if event_obj and event_name:
                key = canonical_key(event_name)
                if any(token and token in key for token in next_event_keys):
                    related_event = event_obj
            if related_event and session_code:
                try:
                    closes_at = to_utc(related_event.get_session_date(session_code, utc=True))
                except Exception:
                    closes_at = None
            outcomes = []
            for outcome in outcomes_map.get(market.id, []):
                outcomes.append(
                    OutcomeInfo(
                        market_id=market.id,
                        selection_name=outcome.selection_name,
                        odds_decimal=outcome.odds_decimal,
                        implied_probability=outcome.implied_prob,
                        canonical_key=canonical_key(outcome.selection_name),
                    )
                )
            if not outcomes:
                continue
            is_closed = bool(closes_at and closes_at <= now)
            infos.append(
                MarketInfo(
                    id=market.id,
                    name=display_name,
                    event_name=event_name,
                    session_code=session_code,
                    closes_at=closes_at,
                    is_closed=is_closed,
                    type_tags=normalise_market_type(display_name),
                    outcomes=outcomes,
                )
            )

        if not infos:
            return []

        if next_event_keys:
            grouped = defaultdict(list)
            for info in infos:
                key = canonical_key(info.event_name) if info.event_name else ""
                grouped[key].append(info)
            for event_key in next_event_keys:
                for candidate_key, items in grouped.items():
                    if event_key and event_key in candidate_key:
                        return sorted(items, key=lambda m: m.closes_at or datetime.max)

        return sorted(infos, key=lambda m: m.closes_at or datetime.max)

    def _find_market_for_type(self, bet_type: str) -> Optional[MarketInfo]:
        now = datetime.now(timezone.utc)
        for market in self._latest_markets:
            if bet_type not in market.type_tags:
                continue
            if market.closes_at and market.closes_at <= now:
                continue
            return market
        return None

    @wallet.command(name="info", description="Show FIT balance and history")
    async def wallet_info(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        balance = await run_in_thread(self._wallets.get_balance, interaction.user.id)
        transactions = await run_in_thread(self._wallets.recent_transactions, interaction.user.id, 10)
        bets = await run_in_thread(self._wallets.list_open_bets, interaction.user.id)

        embed = discord.Embed(title="FIT Wallet", color=0x2ECC71)
        embed.add_field(
            name="Balance",
            value=f"{from_cents(balance):,.2f} FITs",
            inline=False,
        )
        if transactions:
            lines = []
            for txn in transactions:
                amount = from_cents(txn.amount)
                prefix = "➕" if amount >= 0 else "➖"
                timestamp = txn.created_at.strftime("%Y-%m-%d %H:%M UTC")
                lines.append(
                    f"{prefix} {amount:+.2f} FITs — {txn.description} ({timestamp})"
                )
            embed.add_field(
                name="Recent Transactions",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Recent Transactions",
                value="No transactions yet.",
                inline=False,
            )

        if bets:
            lines = []
            for bet in bets[:10]:
                amount = from_cents(bet.amount)
                closes = (
                    bet.closes_at.strftime("%Y-%m-%d %H:%M UTC")
                    if bet.closes_at
                    else "TBA"
                )
                lines.append(
                    f"#{bet.id} {bet.market_name}: {amount:.2f} FITs on {bet.outcome_name} (closes {closes})"
                )
            embed.add_field(
                name="Open Bets",
                value="\n".join(lines),
                inline=False,
            )
        else:
            embed.add_field(
                name="Open Bets",
                value="No open bets.",
                inline=False,
            )

        await interaction.followup.send(embed=embed, ephemeral=True)

    @wallet.command(name="send", description="Send FITs to another user")
    @app_commands.describe(user="Recipient", amount="FIT amount to send")
    async def wallet_send(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        amount: app_commands.Range[float, 0.01, 100000.0],
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        if user.bot:
            await interaction.followup.send("You cannot send FITs to a bot.", ephemeral=True)
            return
        try:
            cents = to_cents(float(amount))
            sender_balance, _ = await run_in_thread(
                self._wallets.transfer_tokens,
                interaction.user.id,
                user.id,
                cents,
            )
        except WalletError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Sent {float(amount):.2f} FITs to {user.mention}. "
            f"New balance: {from_cents(sender_balance):.2f} FITs.",
            ephemeral=True,
        )

    @wallet.command(name="generate", description="Generate FITs (admin only)")
    @app_commands.describe(amount="FIT amount to mint")
    async def wallet_generate(
        self,
        interaction: discord.Interaction,
        amount: app_commands.Range[float, 0.01, 100000.0],
    ) -> None:
        if interaction.user.id not in self.config.admin_ids:
            await interaction.response.send_message(
                "You are not permitted to mint FITs.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        cents = to_cents(float(amount))
        try:
            new_balance = await run_in_thread(
                self._wallets.add_tokens,
                interaction.user.id,
                cents,
                "Admin mint",
            )
        except WalletError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"Minted {float(amount):.2f} FITs. "
            f"New balance: {from_cents(new_balance):.2f} FITs.",
            ephemeral=True,
        )

    @app_commands.command(name="bet", description="Place a bet using FITs")
    @app_commands.choices(
        bet_type=[
            app_commands.Choice(name="Race Winner", value="winner"),
            app_commands.Choice(name="Top 3", value="top3"),
            app_commands.Choice(name="Top 6", value="top6"),
            app_commands.Choice(name="Qualifying", value="qualifying"),
            app_commands.Choice(name="Sprint", value="sprint"),
        ]
    )
    @app_commands.describe(
        bet_type="Type of market to bet on",
        argument="Driver or selection name",
        amount="Amount of FITs to wager",
    )
    async def bet(
        self,
        interaction: discord.Interaction,
        bet_type: app_commands.Choice[str],
        argument: str,
        amount: app_commands.Range[float, 0.01, 100000.0],
    ) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        await self._refresh_market_cache()
        market = self._find_market_for_type(bet_type.value)
        if not market:
            await interaction.followup.send(
                "No open market found for that bet type right now.", ephemeral=True
            )
            return
        if market.closes_at and market.closes_at <= datetime.now(timezone.utc):
            await interaction.followup.send(
                "This market is already closed.", ephemeral=True
            )
            return
        key = canonical_key(argument)
        outcome = None
        for candidate in market.outcomes:
            if candidate.canonical_key == key:
                outcome = candidate
                break
        if outcome is None:
            await interaction.followup.send(
                "Selection not found in this market. Check spelling and try again.",
                ephemeral=True,
            )
            return
        cents = to_cents(float(amount))
        try:
            bet_id = await run_in_thread(
                self._wallets.create_bet,
                interaction.user.id,
                market.id,
                market.name,
                outcome.selection_name,
                bet_type.value,
                argument,
                cents,
                outcome.odds_decimal,
                market.closes_at,
            )
        except WalletError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        potential = from_cents(cents) * outcome.odds_decimal
        closes = (
            format_local(market.closes_at, self.config.default_timezone)
            if market.closes_at
            else "TBA"
        )
        await interaction.followup.send(
            f"Bet #{bet_id} placed on **{outcome.selection_name}** in {market.name}.\n"
            f"Stake: {float(amount):.2f} FITs\n"
            f"Potential return: {potential:.2f} FITs\n"
            f"Market closes: {closes}",
            ephemeral=True,
        )

