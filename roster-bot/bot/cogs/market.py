"""
/market command group — public read-only market surfaces.

Commands:
  Phase 3:
    /market view tier:<code> [page]     paginated tier market
    /market movers tier:<code>          top risers & fallers
    /market driver member:<@user>       driver card: value + trend
    /market dashboard                   cross-tier top-of-tier summary
  Phase 4:
    /market team name:<key>             cap sheet + per-driver P/L
    /market surplus tier:<code>         best P/L (market − contract)
    /market underwater tier:<code>      worst P/L

Every reply is ephemeral (matching `/roster view`'s convention) —
public boards are the shared artifact and are posted separately via
`/market-admin board add`.
"""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, limits, queries
from bot.contracts import render as contract_render
from bot.market import budget_ops
from bot.market import render as market_render


class MarketCog(commands.Cog):
    market = app_commands.Group(
        name="market",
        description="Browse the driver market",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /market view ─────────────────────────────────────────────────────

    @market.command(name="view", description="Paginated market table for a tier")
    @app_commands.describe(
        tier="Tier code (e.g. t1)",
        page="Page number (1-indexed; leave blank for page 1)",
    )
    async def market_view(
        self,
        interaction: discord.Interaction,
        tier: str,
        page: int = 1,
    ) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            ctx = await _resolve_tier_view_context(conn, interaction.guild_id, tier)
            if isinstance(ctx, str):
                await interaction.followup.send(ctx, ephemeral=True)
                return
            season, tier_row = ctx
            rows = await queries.fetch_market_table_for_tier(conn, tier_row.id)
            round_label = await _latest_round_label(conn, tier_row.id)

        embed = market_render.render_market_page(
            tier_label=tier_row.label,
            round_label=round_label,
            accent_color=tier_row.accent_color,
            drivers=rows,
            page=page,
        )
        view = _MarketPagerView(
            tier_id=tier_row.id,
            tier_label=tier_row.label,
            accent_color=tier_row.accent_color,
        )
        view.sync_with_embed(embed)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)

    # ── /market movers ───────────────────────────────────────────────────

    @market.command(name="movers", description="Top risers and fallers in a tier")
    @app_commands.describe(tier="Tier code (e.g. t1)")
    async def market_movers(
        self, interaction: discord.Interaction, tier: str
    ) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            ctx = await _resolve_tier_view_context(conn, interaction.guild_id, tier)
            if isinstance(ctx, str):
                await interaction.followup.send(ctx, ephemeral=True)
                return
            season, tier_row = ctx
            risers, fallers = await queries.fetch_movers_for_tier(
                conn, tier_row.id, limits.MOVERS_PER_DIRECTION
            )
            round_label = await _latest_round_label(conn, tier_row.id)

        embed = market_render.render_movers(
            tier_label=tier_row.label,
            round_label=round_label,
            accent_color=tier_row.accent_color,
            risers=risers,
            fallers=fallers,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /market driver ───────────────────────────────────────────────────

    @market.command(name="driver", description="Driver card: value, movement, trend")
    @app_commands.describe(member="The driver's Discord account")
    async def market_driver(
        self, interaction: discord.Interaction, member: discord.Member
    ) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send(
                    "No active season yet.", ephemeral=True
                )
                return
            driver = await queries.fetch_driver_by_member(conn, season.id, member.id)
            if driver is None:
                await interaction.followup.send(
                    f"{member.display_name} isn't registered as a driver in "
                    f"**{season.name}**.",
                    ephemeral=True,
                )
                return
            tier = await queries.fetch_tier_by_id(conn, driver.tier_id)
            history = await queries.fetch_driver_valuation_history(
                conn, driver.id, limits.DRIVER_TREND_ENTRIES
            )

        latest = history[0] if history else None
        embed = market_render.render_driver_card(
            display_name=driver.display_name,
            tier_label=tier.label if tier else "?",
            accent_color=tier.accent_color if tier else None,
            latest=latest,
            history=history,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /market team ─────────────────────────────────────────────────────

    @market.command(name="team", description="Cap sheet + per-driver P/L for a team")
    @app_commands.describe(name="Team key (e.g. red-bull)")
    async def market_team(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            team_row = await queries.fetch_team(
                conn, interaction.guild_id, name.lower()
            )
            if team_row is None:
                await interaction.followup.send(
                    f"No team `{name}`.", ephemeral=True
                )
                return
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send(
                    "No active season.", ephemeral=True
                )
                return
            cfg = (
                await queries.fetch_league_config_row(conn, season.id, None)
            )
            if cfg is None:
                await interaction.followup.send(
                    "No league_config for the active season.", ephemeral=True
                )
                return
            payroll = await queries.fetch_team_payroll(conn, team_row.id)
            slots_used = await queries.fetch_team_active_slot_count(conn, team_row.id)
            cap_rows = await queries.fetch_team_cap_sheet_rows(conn, team_row.id)
            dead_money_total = await queries.fetch_dead_money_total(
                conn, team_row.id, season.id
            )
            dead_money_rows = await queries.fetch_dead_money_for_team(
                conn, team_row.id, season.id
            )
            budget_snap = await budget_ops.snapshot(
                conn, season_id=season.id, tier_id=None, team_id=team_row.id,
            )

        embed = contract_render.render_cap_sheet(
            team_name=team_row.name,
            color=team_row.color,
            contracts_with_market=cap_rows,
            payroll=payroll,
            salary_cap=cfg.salary_cap,
            active_slots_used=slots_used,
            active_slots_max=cfg.active_driver_slots,
            dead_money=dead_money_total,
            dead_money_rows=[
                {"amount": r.amount, "note": r.note}
                for r in dead_money_rows
            ],
            budget_balance=budget_snap.balance if budget_snap else None,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /market surplus | underwater ────────────────────────────────────

    @market.command(name="surplus", description="Best contracts by P/L in a tier")
    @app_commands.describe(tier="Tier code (e.g. t1)")
    async def market_surplus(
        self, interaction: discord.Interaction, tier: str
    ) -> None:
        await self._render_pl_for_tier(interaction, tier, top_first=True)

    @market.command(name="underwater", description="Worst contracts by P/L in a tier")
    @app_commands.describe(tier="Tier code (e.g. t1)")
    async def market_underwater(
        self, interaction: discord.Interaction, tier: str
    ) -> None:
        await self._render_pl_for_tier(interaction, tier, top_first=False)

    async def _render_pl_for_tier(
        self,
        interaction: discord.Interaction,
        tier_code: str,
        *,
        top_first: bool,
    ) -> None:
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            ctx = await _resolve_tier_view_context(
                conn, interaction.guild_id, tier_code
            )
            if isinstance(ctx, str):
                await interaction.followup.send(ctx, ephemeral=True)
                return
            season, tier_row = ctx
            rows = await queries.fetch_tier_contracts_with_market(
                conn, tier_row.id
            )
            round_label = await _latest_round_label(conn, tier_row.id)

        title = (
            f"Surplus — {tier_row.label}" if top_first
            else f"Underwater — {tier_row.label}"
        )
        embed = contract_render.render_pl_table(
            title=title,
            color=tier_row.accent_color,
            rows=rows,
            top_first=top_first,
            round_label=round_label,
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /market dashboard ────────────────────────────────────────────────

    @market.command(name="dashboard", description="Cross-tier top-of-tier summary")
    async def market_dashboard(self, interaction: discord.Interaction) -> None:
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send(
                    "No active season yet.", ephemeral=True
                )
                return
            rows = await queries.fetch_cross_tier_top(
                conn, season.id, limits.DASHBOARD_PER_TIER
            )

        embed = market_render.render_dashboard(
            season_name=season.name, tier_rows=rows
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


# ── helpers ──────────────────────────────────────────────────────────────


async def _resolve_tier_view_context(conn, guild_id: int, tier_code: str):
    """
    Fetch active season + tier row, or return a user-facing error
    string when either is missing. Returning either a tuple or a str
    keeps every command's error handling to one branch.
    """
    season = await queries.fetch_active_season(conn, guild_id)
    if season is None:
        return "No active season yet."
    tier_row = await queries.fetch_tier(conn, season.id, tier_code)
    if tier_row is None:
        return f"No tier `{tier_code}` in **{season.name}**."
    return (season, tier_row)


async def _latest_round_label(conn, tier_id: int) -> str | None:
    return await conn.fetchval(
        """
        SELECT round_label FROM valuation_runs
        WHERE tier_id = $1 AND published
        ORDER BY published_at DESC NULLS LAST, created_at DESC
        LIMIT 1
        """,
        tier_id,
    )


class _MarketPagerView(discord.ui.View):
    """
    Prev/Next buttons on the ephemeral market view. State is a single
    integer (current page); every button press re-queries the DB so a
    stale view after a publish shows fresh numbers on the next click.
    """

    def __init__(
        self, *, tier_id: int, tier_label: str, accent_color: int | None
    ) -> None:
        super().__init__(timeout=180)
        self._tier_id = tier_id
        self._tier_label = tier_label
        self._accent_color = accent_color
        self._page = 1
        self._total_pages = 1

    def sync_with_embed(self, embed: discord.Embed) -> None:
        # Parse "Page N/M" out of the footer to align button state with
        # what the render actually produced (after clamping).
        footer = embed.footer.text if embed.footer else ""
        page = 1
        total = 1
        for chunk in footer.split("·"):
            chunk = chunk.strip()
            if chunk.startswith("Page "):
                _, spec = chunk.split(" ", 1)
                try:
                    page_str, total_str = spec.split("/", 1)
                    page = int(page_str)
                    total = int(total_str)
                except ValueError:
                    pass
        self._page = page
        self._total_pages = total
        self._sync_button_state()

    def _sync_button_state(self) -> None:
        self.prev_button.disabled = self._page <= 1
        self.next_button.disabled = self._page >= self._total_pages

    async def _rerender(self, interaction: discord.Interaction) -> None:
        async with db.connect() as conn:
            rows = await queries.fetch_market_table_for_tier(conn, self._tier_id)
            round_label = await _latest_round_label(conn, self._tier_id)
        embed = market_render.render_market_page(
            tier_label=self._tier_label,
            round_label=round_label,
            accent_color=self._accent_color,
            drivers=rows,
            page=self._page,
        )
        self.sync_with_embed(embed)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀ Prev", style=discord.ButtonStyle.secondary)
    async def prev_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self._page = max(1, self._page - 1)
        await self._rerender(interaction)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.secondary)
    async def next_button(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        self._page = min(self._total_pages, self._page + 1)
        await self._rerender(interaction)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(MarketCog(bot))
