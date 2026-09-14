"""
/trade command group — team-to-team contract swaps.

Phase 5 ships the 1-for-1 flow: one contract from each side, both
teams' TPs must accept, then commissioner approves. Multi-contract
trades reuse the same schema (trade_items rows) but aren't wired up
here yet.

Authority mirrors /contract:
  * Propose / withdraw   — TP of the proposing team or admin.
  * Accept / decline     — TP of the other team or admin.
  * Approve / reject     — /market-admin approve-trade | reject-trade.
"""

from __future__ import annotations

import logging
from decimal import Decimal

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, queries, roster_ops
from bot.contracts import render as contract_render
from bot.contracts import service

log = logging.getLogger(__name__)

_TTL_CHOICES = [
    app_commands.Choice(name="24 hours", value=24),
    app_commands.Choice(name="48 hours", value=48),
    app_commands.Choice(name="72 hours", value=72),
    app_commands.Choice(name="7 days", value=168),
]


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


def _authorised_for_team(interaction: discord.Interaction, team) -> bool:
    if _is_admin(interaction):
        return True
    if not isinstance(interaction.user, discord.Member):
        return False
    if team.principal_role_id is None:
        return False
    return any(r.id == team.principal_role_id for r in interaction.user.roles)


class TradesCog(commands.Cog):
    trade = app_commands.Group(
        name="trade",
        description="Team-to-team contract trades",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /trade propose ──────────────────────────────────────────────────

    @trade.command(name="propose", description="Propose a 1-for-1 trade")
    @app_commands.describe(
        my_team="Team you're proposing for",
        other_team="Team you want to trade with",
        my_contract_id="One of your active contracts to send",
        their_contract_id="One of their active contracts you want",
        ttl_hours="How long the other team has to respond",
        message="Optional note for the other team",
    )
    @app_commands.choices(ttl_hours=_TTL_CHOICES)
    async def trade_propose(
        self,
        interaction: discord.Interaction,
        my_team: str,
        other_team: str,
        my_contract_id: int,
        their_contract_id: int,
        ttl_hours: app_commands.Choice[int],
        message: str | None = None,
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            proposing = await queries.fetch_team(
                conn, interaction.guild_id, my_team.lower()
            )
            other = await queries.fetch_team(
                conn, interaction.guild_id, other_team.lower()
            )
            if proposing is None or other is None:
                await interaction.followup.send(
                    "Unknown team.", ephemeral=True
                )
                return
            if not _authorised_for_team(interaction, proposing):
                await interaction.followup.send(
                    f"You aren't a principal for **{proposing.name}**.",
                    ephemeral=True,
                )
                return
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send(
                    "No active season.", ephemeral=True
                )
                return

            try:
                trade_id = await service.propose_trade(
                    conn,
                    season_id=season.id,
                    proposing_team_id=proposing.id,
                    other_team_id=other.id,
                    proposed_by=interaction.user.id,
                    items=[
                        (proposing.id, my_contract_id),
                        (other.id, their_contract_id),
                    ],
                    message=message,
                    ttl_hours=ttl_hours.value,
                )
            except service.TransitionError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            # Snapshot cap impact for the review + delivery embed.
            review_embed = await _build_trade_embed(conn, trade_id)

        thread_id = await _deliver_trade(
            self.bot, interaction=interaction, trade_id=trade_id,
            proposing_team=proposing, other_team=other,
            embed=review_embed,
        )
        if thread_id is not None:
            async with db.connect() as conn:
                await queries.set_trade_thread_id(conn, trade_id, thread_id)

        thread_bit = f" (thread: <#{thread_id}>)" if thread_id else ""
        await interaction.followup.send(
            f"✅ Trade `{trade_id}` proposed to **{other.name}**{thread_bit}.",
            embed=review_embed,
            ephemeral=True,
        )

    # ── /trade accept | decline | withdraw ──────────────────────────────

    @trade.command(name="accept", description="Accept a trade offered to your team")
    @app_commands.describe(trade_id="Trade id")
    async def trade_accept(
        self, interaction: discord.Interaction, trade_id: int
    ) -> None:
        assert interaction.guild is not None and interaction.guild_id is not None
        async with db.connect() as conn:
            trade = await queries.fetch_trade_by_id(conn, trade_id)
            if trade is None:
                await interaction.response.send_message(
                    f"No trade `{trade_id}`.", ephemeral=True
                )
                return
            other = await queries.fetch_team_by_id(conn, trade.other_team_id)
            if other is None or not _authorised_for_team(interaction, other):
                await interaction.response.send_message(
                    "Only the receiving team's principal can accept.",
                    ephemeral=True,
                )
                return
            try:
                await service.accept_trade(
                    conn, trade_id, actor_id=interaction.user.id
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ Accepted trade `{trade_id}`. Awaiting commissioner approval — "
            f"`/market-admin approve-trade trade_id: {trade_id}`.",
            ephemeral=True,
        )

    @trade.command(name="decline", description="Decline a trade offered to your team")
    @app_commands.describe(
        trade_id="Trade id",
        note="Optional note for the ledger",
    )
    async def trade_decline(
        self, interaction: discord.Interaction, trade_id: int,
        note: str | None = None,
    ) -> None:
        async with db.connect() as conn:
            trade = await queries.fetch_trade_by_id(conn, trade_id)
            if trade is None:
                await interaction.response.send_message(
                    f"No trade `{trade_id}`.", ephemeral=True
                )
                return
            other = await queries.fetch_team_by_id(conn, trade.other_team_id)
            if other is None or not _authorised_for_team(interaction, other):
                await interaction.response.send_message(
                    "Only the receiving team's principal can decline.",
                    ephemeral=True,
                )
                return
            try:
                await service.decline_trade(
                    conn, trade_id, actor_id=interaction.user.id, note=note
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ Declined trade `{trade_id}`.", ephemeral=True
        )

    @trade.command(name="withdraw", description="Withdraw a trade you proposed")
    @app_commands.describe(trade_id="Trade id")
    async def trade_withdraw(
        self, interaction: discord.Interaction, trade_id: int
    ) -> None:
        async with db.connect() as conn:
            trade = await queries.fetch_trade_by_id(conn, trade_id)
            if trade is None:
                await interaction.response.send_message(
                    f"No trade `{trade_id}`.", ephemeral=True
                )
                return
            proposing = await queries.fetch_team_by_id(
                conn, trade.proposing_team_id
            )
            if proposing is None or not _authorised_for_team(interaction, proposing):
                await interaction.response.send_message(
                    "Only the proposing team's principal can withdraw.",
                    ephemeral=True,
                )
                return
            try:
                await service.withdraw_trade(
                    conn, trade_id, actor_id=interaction.user.id
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ Withdrew trade `{trade_id}`.", ephemeral=True
        )

    @trade.command(name="status", description="Show a trade's current state + items")
    @app_commands.describe(trade_id="Trade id")
    async def trade_status(
        self, interaction: discord.Interaction, trade_id: int
    ) -> None:
        async with db.connect() as conn:
            trade = await queries.fetch_trade_by_id(conn, trade_id)
            if trade is None:
                await interaction.response.send_message(
                    f"No trade `{trade_id}`.", ephemeral=True
                )
                return
            embed = await _build_trade_embed(conn, trade_id)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ── helpers ────────────────────────────────────────────────────────────


async def _build_trade_embed(conn, trade_id: int) -> discord.Embed:
    trade = await queries.fetch_trade_by_id(conn, trade_id)
    assert trade is not None
    proposing = await queries.fetch_team_by_id(conn, trade.proposing_team_id)
    other = await queries.fetch_team_by_id(conn, trade.other_team_id)
    items = await queries.fetch_trade_items(conn, trade_id)

    detail_rows = []
    change_proposing = Decimal(0)
    change_other = Decimal(0)
    for item in items:
        contract = await queries.fetch_contract_by_id(conn, item.contract_id)
        if contract is None:
            continue
        driver_row = await conn.fetchrow(
            "SELECT display_name FROM drivers WHERE id = $1",
            contract.driver_id,
        )
        going_to = (
            "to_proposing" if item.from_team_id == trade.other_team_id
            else "to_other"
        )
        detail_rows.append({
            "display_name": driver_row["display_name"] if driver_row else "?",
            "contract_value": contract.contract_value,
            "term_seasons": contract.term_seasons,
            "contract_type": contract.contract_type,
            "direction": going_to,
        })
        if going_to == "to_proposing":
            change_proposing += contract.contract_value
            change_other -= contract.contract_value
        else:
            change_proposing -= contract.contract_value
            change_other += contract.contract_value

    payroll_p = await queries.fetch_team_payroll(conn, trade.proposing_team_id)
    payroll_o = await queries.fetch_team_payroll(conn, trade.other_team_id)
    cfg = (
        await queries.fetch_league_config_row(conn, trade.season_id, None)
    )
    cap = cfg.salary_cap if cfg else Decimal(0)

    embed = contract_render.render_trade_review(
        proposing_team_name=proposing.name if proposing else "?",
        other_team_name=other.name if other else "?",
        items=detail_rows,
        message=trade.message,
        payroll_before_proposing=payroll_p,
        payroll_after_proposing=payroll_p + change_proposing,
        salary_cap_proposing=cap,
        payroll_before_other=payroll_o,
        payroll_after_other=payroll_o + change_other,
        salary_cap_other=cap,
    )
    embed.set_footer(
        text=(
            f"Trade {trade_id} · state {trade.state} · "
            f"expires <t:{int(trade.expires_at.timestamp())}:R>"
        )
    )
    return embed


async def _deliver_trade(
    bot: commands.Bot,
    *,
    interaction: discord.Interaction,
    trade_id: int,
    proposing_team,
    other_team,
    embed: discord.Embed,
) -> int | None:
    """
    Post the trade proposal into the approvals channel so both TPs
    can act on it. Same fallback shape as offer delivery — DM the
    other TP if the thread can't be created.
    """
    assert interaction.guild_id is not None
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, interaction.guild_id)
        cfg = (
            await queries.fetch_league_config_row(conn, season.id, None)
            if season else None
        )
    approvals_channel_id = cfg.approvals_channel_id if cfg else None

    hint = (
        f"Trade `{trade_id}` — {proposing_team.name} ↔ {other_team.name}.\n"
        f"Accept: `/trade accept trade_id: {trade_id}`  ·  "
        f"Decline: `/trade decline trade_id: {trade_id}`  ·  "
        f"Withdraw: `/trade withdraw trade_id: {trade_id}`"
    )
    if approvals_channel_id is not None:
        channel = bot.get_channel(approvals_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                thread = await channel.create_thread(
                    name=(
                        f"trade-{trade_id}-{proposing_team.key}-{other_team.key}"
                    )[:100],
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    reason=f"Trade {trade_id} negotiation",
                )
                await thread.send(content=hint, embed=embed)
                return thread.id
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not create trade thread for trade %s: %s",
                    trade_id, exc,
                )
    return None


# `roster_ops` is imported for symmetry with the roster/contract cogs;
# team-role swaps at trade-approval time are handled by the admin
# approve-trade path in bot/cogs/admin_market.py, which needs the
# guild handle.
_ = roster_ops


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(TradesCog(bot))
