"""
/market-admin command group — commissioner-only setup for seasons, tiers,
and league config.

Phase 1 surface:
  /market-admin season create <name> [preset]
  /market-admin season activate <name>
  /market-admin season list
  /market-admin tier add|edit|list
  /market-admin config show|edit|channel|role|free-agency

Phase 2 surface:
  /market-admin valuation run tier:<code> round:<label>   (dry-run)
  /market-admin valuation preview run:<id>                (re-show)
  /market-admin valuation publish run:<id>
  /market-admin valuation list [tier:<code>]

Phase 3 surface:
  /market-admin board add kind:<kind> channel:<#chan> [tier:<code>]
  /market-admin board remove board_id:<id>
  /market-admin board refresh [board_id:<id>]
  /market-admin board list

Phase 6 surface:
  /market-admin results import tier:<code> round:<label> sheet:<url> [tab] [held_on]
  /market-admin results list [tier:<code>]
  /market-admin results show tier:<code> round:<label>

Driver enrolment surface:
  /market-admin driver add member:<@user> tier:<code> status:<status>
  /market-admin driver sync tier:<code>          (enrol every member of the tier role)
  /market-admin driver sync-all                  (every tier that has a role set)

Phase 4 surface:
  /market-admin approve offer_id:<id>
  /market-admin reject offer_id:<id> [note]
  /market-admin void contract_id:<id> [note]
  /market-admin set-status driver:<@member> status:<code>
  /market-admin adjust-cap team:<key> delta:<amount> [note]

Phase 5 surface:
  /market-admin promote driver:<@member> new_tier:<code> [note]
  /market-admin relegate driver:<@member> new_tier:<code> [note]
  /market-admin approve-trade trade_id:<id>
  /market-admin reject-trade trade_id:<id> [note]

This module is `/market-admin` only; public /market is Phase 3,
/contract is Phase 4, /trade is Phase 5.

Authority: Manage Server (mirrors `_is_admin` in cogs/roster.py). A
commissioner role assigned via `/market-admin config role commissioner`
is stored for later phases to consult but does not yet grant access —
Phase 1 stays with Manage Server so we do not diverge from the existing
permission model until the contract flow needs the split.
"""

import logging
from decimal import Decimal, InvalidOperation

import discord
from discord import app_commands
from discord.ext import commands

from bot import approvals, db, queries, sheets, workflow
from bot.contracts import service as contracts_service
from bot.market import driver_ops
from bot.market import results as results_engine
from bot.market.money import format_money, format_pl
from bot.ui.config_modal import ConfigSectionView, build_config_embed

log = logging.getLogger(__name__)


# Default sheet range for a results import: a generous window over the
# first tab, so a commissioner can paste just the sheet URL.
_DEFAULT_RESULTS_RANGE = "A1:Z100"

# Discord message limits — these govern rendering only, never league maths.
_DISCORD_MSG_LIMIT = 1900
_MAX_LISTED_NAMES = 8
# Ledger rows shown by `/market-admin budget show`.
_BUDGET_RECENT_LIMIT = 8
_MAX_LISTED_ROUNDS = 25
_MAX_LISTED_RESULTS = 22
_MAX_LISTED_ERRORS = 12


def _render_import_errors(headline: str, errors: list[str]) -> str:
    shown = errors[:_MAX_LISTED_ERRORS]
    body = "\n".join(f"• {e}" for e in shown)
    if len(errors) > _MAX_LISTED_ERRORS:
        body += f"\n• …and {len(errors) - _MAX_LISTED_ERRORS} more."
    return f"❌ {headline}\n{body}"[:_DISCORD_MSG_LIMIT]


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


async def _admin_or_deny(interaction: discord.Interaction) -> bool:
    if _is_admin(interaction):
        return True
    await interaction.response.send_message(
        "You need **Manage Server** to use this command.", ephemeral=True
    )
    return False


def _fmt_money(value: Decimal | None) -> str:
    if value is None:
        return "—"
    return f"${value:,.2f}M"


def _fmt_pct(value: Decimal) -> str:
    return f"{value * Decimal('100'):.1f}%"


class AdminMarketCog(commands.Cog):
    admin = app_commands.Group(
        name="market-admin",
        description="Commissioner setup for seasons, tiers, and league config",
        default_permissions=discord.Permissions(manage_guild=True),
    )
    season = app_commands.Group(
        name="season", description="Season management", parent=admin
    )
    tier = app_commands.Group(
        name="tier", description="Tier management", parent=admin
    )
    config = app_commands.Group(
        name="config", description="League config", parent=admin
    )
    valuation = app_commands.Group(
        name="valuation", description="Run and publish market valuations", parent=admin
    )
    board = app_commands.Group(
        name="board", description="Self-updating market boards", parent=admin
    )
    results = app_commands.Group(
        name="results", description="Import and inspect race results", parent=admin
    )
    driver = app_commands.Group(
        name="driver",
        description="Enrol members into a tier's market",
        parent=admin,
    )
    budget = app_commands.Group(
        name="budget",
        description="Team budgets: prize money, penalties, rollover, config",
        parent=admin,
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /market-admin season ─────────────────────────────────────────────

    @season.command(name="create", description="Create a new season")
    @app_commands.describe(
        name="Season name (e.g. 'F1 2026 Season')",
        preset="Optional preset to seed tiers, lookups, and default config",
    )
    @app_commands.choices(
        preset=[app_commands.Choice(name="F1 25/26", value="f1")]
    )
    async def season_create(
        self,
        interaction: discord.Interaction,
        name: str,
        preset: app_commands.Choice[str] | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            created = await workflow.create_season(
                guild_id=interaction.guild_id,
                name=name,
                preset=(preset.value if preset is not None else None),
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        preset_note = (
            "\nSeeded F1 preset: 3 tiers (t1/t2/t3), lookups, "
            "valuation factors, default league config."
            if created.preset_seeded
            else ""
        )
        await interaction.followup.send(
            f"Created season **{created.name}** (id `{created.season_id}`)."
            f"{preset_note}\nUse `/market-admin season activate {created.name}` "
            "to make it active.",
            ephemeral=True,
        )

    @season.command(name="activate", description="Make a season the active one for this server")
    @app_commands.describe(name="Season name")
    async def season_activate(
        self, interaction: discord.Interaction, name: str
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        try:
            await workflow.activate_season(guild_id=interaction.guild_id, name=name)
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"\u2705 **{name}** is now the active season.", ephemeral=True
        )

    @season.command(
        name="carry-over",
        description="Carry active contracts from a past season into the active one",
    )
    @app_commands.describe(from_season="Name of the season that just finished")
    async def season_carry_over(
        self, interaction: discord.Interaction, from_season: str,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await approvals.carry_over_season(
                guild=interaction.guild,
                actor=interaction.user,
                from_season_name=from_season,
            )
        except approvals.ApprovalError as exc:
            await interaction.followup.send(f"\u274c {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            embed=_render_carry_over(result), ephemeral=True
        )

    @season.command(name="list", description="List all seasons for this server")
    async def season_list(self, interaction: discord.Interaction) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            seasons = await queries.fetch_all_seasons(conn, interaction.guild_id)

        if not seasons:
            await interaction.response.send_message(
                "No seasons yet. Use `/market-admin season create <name>` to add one.",
                ephemeral=True,
            )
            return

        lines = [
            f"{'🟢' if s.is_active else '⚪'} **{s.name}** — id `{s.id}`"
            for s in seasons
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /market-admin tier ───────────────────────────────────────────────

    @tier.command(name="add", description="Add a tier to the active season")
    @app_commands.describe(
        code="Short code (e.g. 't1')",
        label="Display label (e.g. 'Tier 1')",
        rank_order="Sort rank (lower first)",
        role="Discord role marking membership in this tier",
        accent_color="Hex color for embeds (e.g. #ff1801)",
    )
    async def tier_add(
        self,
        interaction: discord.Interaction,
        code: str,
        label: str,
        rank_order: int,
        role: discord.Role | None = None,
        accent_color: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        color_int = _parse_color(accent_color)
        if accent_color is not None and color_int is None:
            await interaction.response.send_message(
                f"Could not parse color `{accent_color}`. Use `#RRGGBB` or a decimal.",
                ephemeral=True,
            )
            return

        try:
            await workflow.add_tier(
                guild_id=interaction.guild_id,
                code=code,
                label=label,
                rank_order=rank_order,
                tier_role_id=role.id if role else None,
                accent_color=color_int,
            )
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"\u2705 Added tier `{code}` \u2014 **{label}** to the active season.",
            ephemeral=True,
        )

    @tier.command(name="edit", description="Edit an existing tier in the active season")
    @app_commands.describe(
        code="Tier code to edit",
        label="New display label",
        rank_order="Sort rank",
        role="Tier role (leave unset to keep current)",
        accent_color="Hex color for embeds (leave unset to keep current)",
    )
    async def tier_edit(
        self,
        interaction: discord.Interaction,
        code: str,
        label: str,
        rank_order: int,
        role: discord.Role | None = None,
        accent_color: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        color_int = _parse_color(accent_color) if accent_color else None
        if accent_color is not None and color_int is None:
            await interaction.response.send_message(
                f"Could not parse color `{accent_color}`. Use `#RRGGBB` or a decimal.",
                ephemeral=True,
            )
            return

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season. Create one with `/market-admin season create` first.",
                    ephemeral=True,
                )
                return
            existing = await queries.fetch_tier(conn, season.id, code)
            if existing is None:
                await interaction.response.send_message(
                    f"No tier `{code}` in **{season.name}**.", ephemeral=True
                )
                return
            new_role_id = role.id if role is not None else existing.tier_role_id
            new_color = color_int if accent_color else existing.accent_color
            await queries.update_tier(
                conn, existing.id, label, rank_order, new_role_id, new_color
            )

        await interaction.response.send_message(
            f"✅ Updated tier `{code}` in **{season.name}**.", ephemeral=True
        )

    @tier.command(name="list", description="List tiers for the active season")
    async def tier_list(self, interaction: discord.Interaction) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season yet.", ephemeral=True
                )
                return
            tiers = await queries.fetch_all_tiers(conn, season.id)

        if not tiers:
            await interaction.response.send_message(
                f"No tiers configured for **{season.name}**.", ephemeral=True
            )
            return

        lines = [f"**Tiers — {season.name}**"]
        for t in tiers:
            role = f"<@&{t.tier_role_id}>" if t.tier_role_id else "*(no role)*"
            color = f" #{t.accent_color:06x}" if t.accent_color else ""
            lines.append(f"`{t.code}` · **{t.label}** · rank {t.rank_order} · {role}{color}")

        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /market-admin config ─────────────────────────────────────────────

    @config.command(name="show", description="Show league config for the active season")
    @app_commands.describe(tier="Show tier override (leave unset for season default)")
    async def config_show(
        self, interaction: discord.Interaction, tier: str | None = None
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_id = await _resolve_tier_id(conn, season.id, tier)
            if tier is not None and tier_id is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)

        if cfg is None:
            scope = f"tier `{tier}`" if tier else "the season default"
            await interaction.response.send_message(
                f"No league_config row for {scope}. "
                f"Run `/market-admin config edit` to create one.",
                ephemeral=True,
            )
            return

        scope_label = f"Tier `{tier}` override" if tier else "Season default"
        lines = [
            f"**League Config — {season.name} · {scope_label}**",
            f"Salary cap: {_fmt_money(cfg.salary_cap)}",
            f"Min salary: {_fmt_money(cfg.min_salary)}",
            f"Max salary: {_fmt_money(cfg.max_salary)}",
            f"Active driver slots: {cfg.active_driver_slots}",
            f"Weekly move cap: ±{_fmt_money(cfg.weekly_move_cap)}",
            f"Exceptional move cap: ±{_fmt_money(cfg.exceptional_move_cap)}",
            f"Contract length: {cfg.min_term_seasons}–{cfg.max_term_seasons} "
            f"season(s)",
            f"Max incentive %: {_fmt_pct(cfg.max_incentive_pct)}",
            f"Offer TTL: {cfg.offer_ttl_hours}h",
            f"Free agency: {'🟢 open' if cfg.free_agency_open else '🔴 closed'}",
        ]
        if cfg.market_channel_id:
            lines.append(f"Market channel: <#{cfg.market_channel_id}>")
        if cfg.transactions_channel_id:
            lines.append(f"Transactions channel: <#{cfg.transactions_channel_id}>")
        if cfg.approvals_channel_id:
            lines.append(f"Approvals channel: <#{cfg.approvals_channel_id}>")
        if cfg.commissioner_role_id:
            lines.append(f"Commissioner role: <@&{cfg.commissioner_role_id}>")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @config.command(
        name="edit",
        description="Edit league money limits or contract rules",
    )
    @app_commands.describe(tier="Edit tier override (leave unset for season default)")
    async def config_edit(
        self, interaction: discord.Interaction, tier: str | None = None
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_id = await _resolve_tier_id(conn, season.id, tier)
            if tier is not None and tier_id is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)

        if cfg is None:
            scope = f"tier `{tier}`" if tier else "the season default"
            await interaction.response.send_message(
                f"No league_config row for {scope}. Seed one first by creating a "
                f"season with `--preset f1`, or (for a tier override) copy from the "
                f"season default by editing without a tier once, then re-run with "
                f"a tier.",
                ephemeral=True,
            )
            return

        scope_label = f"tier `{tier}`" if tier else "the season default"
        await interaction.response.send_message(
            embed=build_config_embed(cfg, scope_label=scope_label),
            view=ConfigSectionView(
                season_id=season.id,
                tier_id=tier_id,
                current=cfg,
                opener_id=interaction.user.id,
            ),
            ephemeral=True,
        )

    @config.command(name="channel", description="Set a market channel for the active season")
    @app_commands.describe(
        kind="Which channel to set",
        channel="Channel to use",
        tier="Set on tier override (leave unset for season default)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Market", value="market"),
            app_commands.Choice(name="Transactions", value="transactions"),
            app_commands.Choice(name="Approvals", value="approvals"),
        ]
    )
    async def config_channel(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        channel: discord.TextChannel,
        tier: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_id = await _resolve_tier_id(conn, season.id, tier)
            if tier is not None and tier_id is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)
            if cfg is None:
                await interaction.response.send_message(
                    "No config row for that scope. Run `/market-admin config edit` first.",
                    ephemeral=True,
                )
                return
            kwargs = {f"{kind.value}_channel_id": channel.id}
            await queries.set_league_config_channels(
                conn, season_id=season.id, tier_id=tier_id, **kwargs
            )

        await interaction.response.send_message(
            f"✅ {kind.name} channel set to {channel.mention}.", ephemeral=True
        )

    @config.command(name="role", description="Set the commissioner role for the active season")
    @app_commands.describe(
        role="Commissioner role",
        tier="Set on tier override (leave unset for season default)",
    )
    async def config_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        tier: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        try:
            await workflow.set_commissioner_role(
                guild_id=interaction.guild_id, role_id=role.id, tier_code=tier
            )
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        await interaction.response.send_message(
            f"\u2705 Commissioner role set to {role.mention}.", ephemeral=True
        )

    @config.command(
        name="free-agency",
        description="Open or close free agency for the active season",
    )
    @app_commands.describe(
        state="Open or closed",
        tier="Set on tier override (leave unset for season default)",
    )
    @app_commands.choices(
        state=[
            app_commands.Choice(name="Open", value="open"),
            app_commands.Choice(name="Closed", value="closed"),
        ]
    )
    async def config_free_agency(
        self,
        interaction: discord.Interaction,
        state: app_commands.Choice[str],
        tier: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_id = await _resolve_tier_id(conn, season.id, tier)
            if tier is not None and tier_id is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)
            if cfg is None:
                await interaction.response.send_message(
                    "No config row for that scope. Run `/market-admin config edit` first.",
                    ephemeral=True,
                )
                return
            await queries.set_league_config_channels(
                conn,
                season_id=season.id,
                tier_id=tier_id,
                free_agency_open=(state.value == "open"),
            )

        icon = "🟢" if state.value == "open" else "🔴"
        await interaction.response.send_message(
            f"{icon} Free agency is now **{state.name.lower()}**.", ephemeral=True
        )

    # ── /market-admin valuation ──────────────────────────────────────────

    @valuation.command(
        name="run",
        description="Create a dry-run valuation for a tier (nothing is published)",
    )
    @app_commands.describe(
        tier="Tier code (e.g. t1)",
        round_label="Human label for this run (e.g. 'Post-Abu Dhabi')",
    )
    async def valuation_run(
        self,
        interaction: discord.Interaction,
        tier: str,
        round_label: str,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            result = await workflow.run_valuation(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                tier=tier,
                round_label=round_label,
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(f"\u274c {exc}", ephemeral=True)
            return

        await interaction.followup.send(
            _render_valuation_preview(
                run_id=result.run_id,
                tier_code=tier,
                round_label=round_label,
                published=False,
                outcomes=result.outcomes,
            ),
            ephemeral=True,
        )

    @valuation.command(name="preview", description="Re-show an existing valuation run")
    @app_commands.describe(run_id="Numeric id of the run to preview")
    async def valuation_preview(
        self, interaction: discord.Interaction, run_id: int
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            run = await queries.fetch_valuation_run(conn, run_id)
            if run is None:
                await interaction.followup.send(
                    f"No valuation run with id `{run_id}`.", ephemeral=True
                )
                return
            tier_row = await queries.fetch_tier_by_id(conn, run["tier_id"])
            valuation_rows = await queries.fetch_driver_valuations_for_run(conn, run_id)

        await interaction.followup.send(
            _render_valuation_preview_from_rows(
                run_id=run_id,
                tier_code=tier_row.code if tier_row else "?",
                round_label=run["round_label"],
                published=run["published"],
                rows=valuation_rows,
            ),
            ephemeral=True,
        )

    @valuation.command(name="publish", description="Publish a dry-run valuation")
    @app_commands.describe(run_id="Numeric id of the run to publish")
    async def valuation_publish(
        self, interaction: discord.Interaction, run_id: int
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            outcome = await workflow.publish_valuation(
                self.bot, guild_id=interaction.guild_id, run_id=run_id
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if outcome.already_published:
            await interaction.followup.send(
                f"Run `{run_id}` was already published.", ephemeral=True
            )
            return

        msg = f"\u2705 Published run `{run_id}` \u2014 market values are now live."
        if not outcome.boards_refreshed:
            msg += (
                "\n\u26a0 Board refresh failed; the boards will catch up on the "
                "next automatic poll."
            )
        await interaction.followup.send(msg, ephemeral=True)

    @valuation.command(name="list", description="List recent valuation runs")
    @app_commands.describe(tier="Filter to a single tier (leave unset for all tiers)")
    async def valuation_list(
        self, interaction: discord.Interaction, tier: str | None = None
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_id = await _resolve_tier_id(conn, season.id, tier)
            if tier is not None and tier_id is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            if tier_id is None:
                rows = await conn.fetch(
                    """
                    SELECT vr.id, vr.round_label, vr.published, vr.created_at,
                           t.code AS tier_code
                    FROM valuation_runs vr
                    JOIN tiers t ON t.id = vr.tier_id
                    WHERE vr.season_id = $1
                    ORDER BY vr.created_at DESC
                    LIMIT 20
                    """,
                    season.id,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT vr.id, vr.round_label, vr.published, vr.created_at,
                           t.code AS tier_code
                    FROM valuation_runs vr
                    JOIN tiers t ON t.id = vr.tier_id
                    WHERE vr.season_id = $1 AND vr.tier_id = $2
                    ORDER BY vr.created_at DESC
                    LIMIT 20
                    """,
                    season.id, tier_id,
                )

        if not rows:
            await interaction.response.send_message(
                "No valuation runs yet.", ephemeral=True
            )
            return
        lines = ["**Recent valuation runs**"]
        for r in rows:
            status = "🟢 published" if r["published"] else "⚪ dry-run"
            ts = f"<t:{int(r['created_at'].timestamp())}:d>"
            lines.append(
                f"`{r['id']}` · `{r['tier_code']}` · **{r['round_label']}** "
                f"· {status} · {ts}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /market-admin board ──────────────────────────────────────────────

    @board.command(name="add", description="Post a self-updating market board in a channel")
    @app_commands.describe(
        kind="Which board type (market / movers / dashboard)",
        channel="Channel to post the board in",
        tier="Tier code (required for market/movers; leave empty for dashboard)",
    )
    @app_commands.choices(
        kind=[
            app_commands.Choice(name="Market table", value="market"),
            app_commands.Choice(name="Movers (risers & fallers)", value="movers"),
            app_commands.Choice(name="Cross-tier dashboard", value="dashboard"),
            app_commands.Choice(name="Surplus (best P/L)", value="surplus"),
            app_commands.Choice(name="Underwater (worst P/L)", value="underwater"),
        ]
    )
    async def board_add(
        self,
        interaction: discord.Interaction,
        kind: app_commands.Choice[str],
        channel: discord.TextChannel,
        tier: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            board_id = await workflow.add_board(
                self.bot,
                guild_id=interaction.guild_id,
                kind=kind.value,
                channel_id=channel.id,
                tier_code=tier,
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await interaction.followup.send(
            f"\u2705 Posted **{kind.name}** board (`{board_id}`) in {channel.mention}. "
            "It updates automatically after each `/market-admin valuation publish` "
            "and every 15 minutes via the safety poll.",
            ephemeral=True,
        )

    @board.command(name="remove", description="Delete a market board")
    @app_commands.describe(board_id="Board id (see /market-admin board list)")
    async def board_remove(
        self, interaction: discord.Interaction, board_id: int
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            await workflow.remove_board(self.bot, board_id=board_id)
        except workflow.WorkflowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            f"\u2705 Removed board `{board_id}`.", ephemeral=True
        )

    @board.command(name="refresh", description="Re-render market boards")
    @app_commands.describe(
        board_id="Refresh only this board (leave blank to refresh all)"
    )
    async def board_refresh(
        self, interaction: discord.Interaction, board_id: int | None = None
    ) -> None:
        if not await _admin_or_deny(interaction):
            return

        await interaction.response.defer(ephemeral=True)
        try:
            await workflow.refresh_boards(self.bot, board_id=board_id)
        except workflow.WorkflowError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "\u2705 Refreshed all market boards."
            if board_id is None
            else f"\u2705 Refreshed board `{board_id}`.",
            ephemeral=True,
        )

    @board.command(name="list", description="List market boards for the active season")
    async def board_list(self, interaction: discord.Interaction) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            boards = await queries.fetch_market_boards_in_season(conn, season.id)
            tier_by_id = {
                t.id: t for t in await queries.fetch_all_tiers(conn, season.id)
            }

        if not boards:
            await interaction.response.send_message(
                "No boards configured yet.", ephemeral=True
            )
            return
        lines = ["**Market boards — active season**"]
        for b in boards:
            tier_label = tier_by_id[b.tier_id].code if b.tier_id else "cross"
            status = "✅" if b.message_id else "⚠️ broken (message deleted)"
            lines.append(
                f"`{b.id}` · **{b.kind}** · tier `{tier_label}` · "
                f"<#{b.channel_id}> · {status}"
            )
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    # ── /market-admin driver enrolment ────────────────────────────────────

    @driver.command(
        name="add",
        description="Enrol one member into a tier's market",
    )
    @app_commands.describe(
        member="Discord member to enrol",
        tier="Tier code (e.g. t1)",
        status="Initial driver status",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="Active", value="active"),
        app_commands.Choice(name="Reserve", value="reserve"),
        app_commands.Choice(name="Free agent", value="free_agent"),
        app_commands.Choice(name="Restricted FA", value="restricted_fa"),
        app_commands.Choice(name="Inactive", value="inactive"),
        app_commands.Choice(name="Suspended", value="suspended"),
    ])
    async def driver_add(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        tier: str,
        status: app_commands.Choice[str],
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        if member.bot:
            await interaction.response.send_message(
                f"{member.display_name} is a bot; bots cannot be drivers.",
                ephemeral=True,
            )
            return
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_row = await queries.fetch_tier(conn, season.id, tier.lower())
            if tier_row is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            result = await driver_ops.enrol_driver(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                seed=driver_ops.DriverSeed(
                    member_id=member.id,
                    display_name=member.display_name,
                ),
                status=status.value,
                actor_id=interaction.user.id,
            )
        if result.created:
            await interaction.response.send_message(
                f"✅ Enrolled **{member.display_name}** in tier "
                f"`{tier_row.code}` as `{status.value}`.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ **{member.display_name}** is already enrolled in tier "
                f"`{tier_row.code}`. Use `/market-admin set-status` to change "
                "their status.",
                ephemeral=True,
            )

    @driver.command(
        name="sync",
        description="Enrol every member with a tier's Discord role as active",
    )
    @app_commands.describe(tier="Tier code (e.g. t1)")
    async def driver_sync(
        self, interaction: discord.Interaction, tier: str
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild is not None and interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send("No active season.", ephemeral=True)
                return
            tier_row = await queries.fetch_tier(conn, season.id, tier.lower())
            if tier_row is None:
                await interaction.followup.send(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            reason = _tier_role_unavailable(interaction.guild, tier_row)
            if reason is not None:
                await interaction.followup.send(reason, ephemeral=True)
                return
            role = interaction.guild.get_role(tier_row.tier_role_id)  # type: ignore[arg-type]
            assert role is not None
            seeds = _seeds_from_role(role)
            results = await driver_ops.sync_tier(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                seeds=seeds,
                status="active",
                actor_id=interaction.user.id,
            )
        created = sum(1 for r in results if r.created)
        skipped = len(results) - created
        await interaction.followup.send(
            f"✅ Tier `{tier_row.code}` sync: enrolled **{created}**, "
            f"already-registered **{skipped}**.",
            ephemeral=True,
        )

    @driver.command(
        name="sync-all",
        description="Sync every tier that has a Discord role set",
    )
    async def driver_sync_all(self, interaction: discord.Interaction) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild is not None and interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True)
        lines: list[str] = []
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send("No active season.", ephemeral=True)
                return
            tiers = await queries.fetch_all_tiers(conn, season.id)
            for tier_row in tiers:
                reason = _tier_role_unavailable(interaction.guild, tier_row)
                if reason is not None:
                    lines.append(f"• `{tier_row.code}`: {reason}")
                    continue
                role = interaction.guild.get_role(tier_row.tier_role_id)  # type: ignore[arg-type]
                assert role is not None
                seeds = _seeds_from_role(role)
                results = await driver_ops.sync_tier(
                    conn,
                    season_id=season.id,
                    tier_id=tier_row.id,
                    seeds=seeds,
                    status="active",
                    actor_id=interaction.user.id,
                )
                created = sum(1 for r in results if r.created)
                skipped = len(results) - created
                lines.append(
                    f"• `{tier_row.code}`: enrolled **{created}**, "
                    f"already-registered **{skipped}**"
                )
        body = "\n".join(lines) if lines else "No tiers found."
        await interaction.followup.send(
            f"✅ Sync-all for **{season.name}**:\n{body}",
            ephemeral=True,
        )

    # ── /market-admin approvals + void + status + adjust-cap ──────────────

    @admin.command(
        name="approve",
        description="Approve an accepted offer → create the active contract",
    )
    @app_commands.describe(offer_id="Offer id (state must be pending_approval)")
    async def admin_approve(
        self, interaction: discord.Interaction, offer_id: int
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild is not None and interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            result = await approvals.approve_offer(
                self.bot,
                guild=interaction.guild,
                actor=interaction.user,
                offer_id=offer_id,
            )
        except approvals.ApprovalError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        role_note = (
            f"\n\u26a0 Role not assigned: {result.role_warning}"
            if result.role_warning
            else ""
        )
        await interaction.followup.send(
            f"\u2705 Approved offer `{offer_id}` \u2192 contract "
            f"`{result.contract_id}` (ref `{result.external_ref}`).{role_note}",
            ephemeral=True,
        )

    @admin.command(name="reject", description="Reject an offer awaiting approval")
    @app_commands.describe(
        offer_id="Offer id (state must be pending_approval)",
        note="Optional note for the audit log",
    )
    async def admin_reject(
        self,
        interaction: discord.Interaction,
        offer_id: int,
        note: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        try:
            await approvals.reject_offer(
                offer_id=offer_id, actor_id=interaction.user.id, note=note
            )
        except approvals.ApprovalError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"\u2705 Rejected offer `{offer_id}`.", ephemeral=True
        )

    @admin.command(name="void", description="Void an active contract")
    @app_commands.describe(
        contract_id="Contract id to void",
        note="Optional note for the audit log",
    )
    async def admin_void(
        self,
        interaction: discord.Interaction,
        contract_id: int,
        note: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        async with db.connect() as conn:
            try:
                await contracts_service.void_contract(
                    conn, contract_id, actor_id=interaction.user.id, note=note
                )
            except contracts_service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ Voided contract `{contract_id}`.", ephemeral=True
        )

    @admin.command(name="set-status", description="Set a driver's status")
    @app_commands.describe(
        driver="Driver's Discord account",
        status="New status",
    )
    @app_commands.choices(status=[
        app_commands.Choice(name="Active", value="active"),
        app_commands.Choice(name="Reserve", value="reserve"),
        app_commands.Choice(name="Free agent", value="free_agent"),
        app_commands.Choice(name="Restricted FA", value="restricted_fa"),
        app_commands.Choice(name="Inactive", value="inactive"),
        app_commands.Choice(name="Suspended", value="suspended"),
    ])
    async def admin_set_status(
        self,
        interaction: discord.Interaction,
        driver: discord.Member,
        status: app_commands.Choice[str],
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            driver_row = await queries.fetch_driver_by_member(
                conn, season.id, driver.id
            )
            if driver_row is None:
                await interaction.response.send_message(
                    f"{driver.display_name} isn't registered as a driver.",
                    ephemeral=True,
                )
                return
            prior_status = driver_row.status
            await queries.set_driver_status(conn, driver_row.id, status.value)
            await queries.append_ledger(
                conn,
                season_id=driver_row.season_id,
                tier_id=driver_row.tier_id,
                driver_id=driver_row.id,
                kind="status_change",
                detail={"from": prior_status, "to": status.value},
                actor_id=interaction.user.id,
            )
        await interaction.response.send_message(
            f"✅ {driver.display_name}: `{prior_status}` → `{status.value}`.",
            ephemeral=True,
        )

    @admin.command(
        name="adjust-cap",
        description="Log a cap adjustment for a team (audit trail only in Phase 4)",
    )
    @app_commands.describe(
        team="Team key",
        delta_m="Adjustment in $M (positive = more cap space, negative = less)",
        note="Reason (required — audit trail)",
    )
    async def admin_adjust_cap(
        self,
        interaction: discord.Interaction,
        team: str,
        delta_m: str,
        note: str,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        try:
            delta = Decimal(delta_m.strip().lstrip("$").rstrip("Mm"))
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse delta: {exc}", ephemeral=True
            )
            return
        async with db.connect() as conn:
            team_row = await queries.fetch_team(conn, interaction.guild_id, team.lower())
            if team_row is None:
                await interaction.response.send_message(
                    f"No team `{team}`.", ephemeral=True
                )
                return
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            # Cap adjustments are per-team, not per-tier, so the tier
            # column takes any tier in the season for schema
            # satisfaction. The ledger detail carries the semantic
            # meaning; render layers can filter on kind = 'cap_adjustment'.
            tier_row = (await queries.fetch_all_tiers(conn, season.id))[0]
            await queries.append_ledger(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                team_id=team_row.id,
                kind="cap_adjustment",
                amount=delta,
                detail={"note": note, "team_key": team_row.key},
                actor_id=interaction.user.id,
            )
        sign = "+" if delta >= Decimal("0") else ""
        await interaction.response.send_message(
            f"✅ Cap adjustment logged for **{team_row.name}**: {sign}{delta}$M.\n"
            f"Note: {note}\n"
            "*(Phase 4 records this in the ledger only; enforcement lands in Phase 5.)*",
            ephemeral=True,
        )

    # ── /market-admin promote | relegate | trade approval ────────────────

    @admin.command(name="promote", description="Move a driver to a higher tier")
    @app_commands.describe(
        driver="Driver's Discord account",
        new_tier="Tier code the driver moves to",
        note="Optional note for the audit ledger",
    )
    async def admin_promote(
        self,
        interaction: discord.Interaction,
        driver: discord.Member,
        new_tier: str,
        note: str | None = None,
    ) -> None:
        await self._move_between_tiers(interaction, driver, new_tier, note)

    @admin.command(name="relegate", description="Move a driver to a lower tier")
    @app_commands.describe(
        driver="Driver's Discord account",
        new_tier="Tier code the driver moves to",
        note="Optional note for the audit ledger",
    )
    async def admin_relegate(
        self,
        interaction: discord.Interaction,
        driver: discord.Member,
        new_tier: str,
        note: str | None = None,
    ) -> None:
        await self._move_between_tiers(interaction, driver, new_tier, note)

    async def _move_between_tiers(
        self,
        interaction: discord.Interaction,
        driver: discord.Member,
        new_tier: str,
        note: str | None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            new_tier_row = await queries.fetch_tier(conn, season.id, new_tier)
            if new_tier_row is None:
                await interaction.response.send_message(
                    f"No tier `{new_tier}` in **{season.name}**.",
                    ephemeral=True,
                )
                return
            driver_row = await queries.fetch_driver_by_member(
                conn, season.id, driver.id
            )
            if driver_row is None:
                await interaction.response.send_message(
                    f"{driver.display_name} isn't registered as a driver.",
                    ephemeral=True,
                )
                return
            try:
                await contracts_service.move_driver_between_tiers(
                    conn, driver_row.id,
                    new_tier_id=new_tier_row.id,
                    actor_id=interaction.user.id,
                    note=note,
                )
            except contracts_service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
        await interaction.response.send_message(
            f"✅ Moved {driver.display_name} to tier `{new_tier}`. "
            "The driver's active contract (if any) moved with them; "
            "the next valuation run will re-rank in the new tier.",
            ephemeral=True,
        )

    @admin.command(
        name="approve-trade",
        description="Approve an accepted trade → execute contract transfers",
    )
    @app_commands.describe(trade_id="Trade id (state must be pending_approval)")
    async def admin_approve_trade(
        self, interaction: discord.Interaction, trade_id: int
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild is not None and interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        try:
            result = await approvals.approve_trade(
                guild=interaction.guild,
                actor=interaction.user,
                trade_id=trade_id,
            )
        except approvals.ApprovalError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        tail = ""
        if result.role_warnings:
            tail = "\n\u26a0 Role swaps had issues:\n" + "\n".join(
                f"\u2022 {w}" for w in result.role_warnings
            )
        await interaction.followup.send(
            f"\u2705 Approved trade `{trade_id}`. Contracts transferred; "
            f"cap sheets refresh next `/market team` or board update.{tail}",
            ephemeral=True,
        )

    @admin.command(name="reject-trade", description="Reject an accepted trade")
    @app_commands.describe(
        trade_id="Trade id (state must be pending_approval)",
        note="Optional note for the audit log",
    )
    async def admin_reject_trade(
        self,
        interaction: discord.Interaction,
        trade_id: int,
        note: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        try:
            await approvals.reject_trade(
                trade_id=trade_id, actor_id=interaction.user.id, note=note
            )
        except approvals.ApprovalError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await interaction.response.send_message(
            f"\u2705 Rejected trade `{trade_id}`.", ephemeral=True
        )


    # ── /market-admin results ────────────────────────────────────────────

    @results.command(
        name="import",
        description="Import a round's race results from a Google Sheet",
    )
    @app_commands.describe(
        tier="Tier code (e.g. t1)",
        round_label="Round label, e.g. 'R14 Abu Dhabi'. Re-importing this label updates it.",
        sheet="Google Sheet URL or spreadsheet ID",
        tab="Sheet tab and range, e.g. 'Abu Dhabi!A1:I25'. Defaults to the first tab.",
        held_on="Race date as YYYY-MM-DD (optional)",
    )
    async def results_import(
        self,
        interaction: discord.Interaction,
        tier: str,
        round_label: str,
        sheet: str,
        tab: str | None = None,
        held_on: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)

        try:
            outcome = await workflow.import_round(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                tier=tier,
                round_label=round_label,
                sheet=sheet,
                sheet_range=tab or _DEFAULT_RESULTS_RANGE,
                held_on=held_on,
            )
        except workflow.ImportAborted as exc:
            await interaction.followup.send(
                _render_import_errors(str(exc), exc.errors), ephemeral=True
            )
            return
        except (workflow.WorkflowError, sheets.SheetsError) as exc:
            await interaction.followup.send(f"\u274c {exc}", ephemeral=True)
            return

        lines = [
            f"\u2705 Imported **{outcome.written}** result(s) for `{tier}` \u2014 "
            f"**{round_label}** (round {outcome.round_order}).",
        ]
        if outcome.missing_drivers:
            missing = outcome.missing_drivers
            lines.append(
                f"\u26a0 No row for {len(missing)} roster driver(s): "
                + ", ".join(missing[:_MAX_LISTED_NAMES])
                + ("\u2026" if len(missing) > _MAX_LISTED_NAMES else "")
                + ". They will score nothing for this round."
            )
        lines.extend(_render_budget_outcome(outcome))
        lines.append(
            f"Next: `/market-admin valuation run tier: {tier} "
            f"round_label: {round_label}` to price it (dry-run)."
        )
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @results.command(name="list", description="List imported rounds")
    @app_commands.describe(tier="Optional tier code to filter by")
    async def results_list(
        self, interaction: discord.Interaction, tier: str | None = None
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send("No active season.", ephemeral=True)
                return
            tier_id = None
            if tier is not None:
                tier_row = await queries.fetch_tier(conn, season.id, tier)
                if tier_row is None:
                    await interaction.followup.send(
                        f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                    )
                    return
                tier_id = tier_row.id
            rounds = await queries.list_race_rounds(conn, season.id, tier_id)

        if not rounds:
            await interaction.followup.send(
                "No rounds imported yet. Use `/market-admin results import`.",
                ephemeral=True,
            )
            return

        lines = [f"**Imported rounds — {season.name}**"]
        for r in rounds[:_MAX_LISTED_ROUNDS]:
            held = r["held_on"].isoformat() if r["held_on"] else "date not set"
            lines.append(
                f"`{r['tier_code']}` R{r['round_order']} — **{r['round_label']}** "
                f"· {r['result_count']} result(s) · {held}"
            )
        if len(rounds) > _MAX_LISTED_ROUNDS:
            lines.append(f"…and {len(rounds) - _MAX_LISTED_ROUNDS} more.")
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @results.command(
        name="show",
        description="Show the imported results and normalized scores for a round",
    )
    @app_commands.describe(tier="Tier code", round_label="Round label as imported")
    async def results_show(
        self, interaction: discord.Interaction, tier: str, round_label: str
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send("No active season.", ephemeral=True)
                return
            tier_row = await queries.fetch_tier(conn, season.id, tier)
            if tier_row is None:
                await interaction.followup.send(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            round_row = await queries.fetch_race_round(
                conn, season.id, tier_row.id, round_label
            )
            if round_row is None:
                await interaction.followup.send(
                    f"No round `{round_label}` imported for `{tier}`.", ephemeral=True
                )
                return
            current = await queries.fetch_results_for_round(conn, round_row["id"])
            scores = await queries.fetch_position_scores(conn, season.id)
            tuning = await queries.fetch_results_tuning(conn, season.id, tier_row.id)
            history = await queries.fetch_results_history(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                through_round_order=round_row["round_order"],
            )
            drivers = await queries.fetch_drivers_in_tier(conn, tier_row.id)

        names = {d.id: d.display_name for d in drivers}
        observations = {}
        if current and tuning is not None and scores:
            observations = {
                o.driver_id: o
                for o in results_engine.build_observations(
                    current=current,
                    history=history,
                    position_scores=scores,
                    tuning=tuning,
                )
            }

        lines = [
            f"**{round_label}** · `{tier}` · round {round_row['round_order']} "
            f"· {len(current)} result(s)"
        ]
        ordered = sorted(
            current,
            key=lambda r: (
                r.finish_position is None,
                r.finish_position or 0,
            ),
        )
        for r in ordered[:_MAX_LISTED_RESULTS]:
            name = names.get(r.driver_id, f"driver {r.driver_id}")
            if r.dns:
                pos = "DNS"
            elif r.dnf:
                pos = "DNF"
            else:
                pos = f"P{r.finish_position}"
            flags = []
            if r.grid_position:
                flags.append(f"grid P{r.grid_position}")
            if r.fastest_lap:
                flags.append("FL")
            if r.driver_of_day:
                flags.append("DOTD")
            if r.incident_points:
                flags.append(f"{r.incident_points} inc")
            obs = observations.get(r.driver_id)
            if obs is not None and obs.exceptional:
                flags.append("⭐ exceptional")
            suffix = f" · {', '.join(flags)}" if flags else ""
            lines.append(f"{pos} — **{name}**{suffix}")
            if obs is not None:
                shown = ", ".join(
                    f"{code} {value:+.3f}"
                    for code, value in sorted(obs.factor_values.items())
                    if value
                )
                if shown:
                    lines.append(f"    ↳ {shown}")
        if len(ordered) > _MAX_LISTED_RESULTS:
            lines.append(f"…and {len(ordered) - _MAX_LISTED_RESULTS} more.")
        await interaction.followup.send("\n".join(lines)[:_DISCORD_MSG_LIMIT], ephemeral=True)

    # ── /market-admin budget ─────────────────────────────────────────────
    # The SPENDING CAP (`/market-admin config`) is the league ceiling and
    # is the same for every team. The BUDGET is each team's own money:
    # opening balance + prize money + race earnings − penalties, rolled
    # over between seasons when enabled. A team may hold more budget than
    # the cap; it may never commit payroll above either.

    @budget.command(name="show", description="Show a team's budget next to its cap")
    @app_commands.describe(team="Team key")
    async def budget_show(self, interaction: discord.Interaction, team: str) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        try:
            summary = await workflow.team_budget(
                guild_id=interaction.guild_id, team_key=team, recent_limit=_BUDGET_RECENT_LIMIT,
            )
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            embed=_render_budget_summary(summary), ephemeral=True
        )

    @budget.command(
        name="award",
        description="Credit prize money to a team's budget (audit-logged)",
    )
    @app_commands.describe(
        team="Team key",
        amount_m="Amount in $M (positive)",
        note="Reason (required — audit trail), e.g. 'S7 constructors P2'",
    )
    async def budget_award(
        self, interaction: discord.Interaction, team: str, amount_m: str, note: str,
    ) -> None:
        await self._budget_write(
            interaction, team=team, amount_m=amount_m, note=note, kind="prize_money",
        )

    @budget.command(
        name="adjust",
        description="Manual budget adjustment, either sign (audit-logged)",
    )
    @app_commands.describe(
        team="Team key",
        delta_m="Adjustment in $M (positive = credit, negative = debit)",
        note="Reason (required — audit trail)",
    )
    async def budget_adjust(
        self, interaction: discord.Interaction, team: str, delta_m: str, note: str,
    ) -> None:
        await self._budget_write(
            interaction, team=team, amount_m=delta_m, note=note, kind="adjustment",
        )

    async def _budget_write(
        self,
        interaction: discord.Interaction,
        *,
        team: str,
        amount_m: str,
        note: str,
        kind: str,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        try:
            amount = Decimal(amount_m.strip().lstrip("$").rstrip("Mm"))
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse amount: {exc}", ephemeral=True
            )
            return
        try:
            balance = await workflow.award_budget(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                team_key=team,
                kind=kind,
                amount=amount,
                note=note,
            )
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
            return
        label = "Prize money" if kind == "prize_money" else "Adjustment"
        await interaction.response.send_message(
            f"\u2705 {label} {format_pl(amount)} logged for `{team.lower()}`. "
            f"Budget is now **{format_money(balance)}**.",
            ephemeral=True,
        )

    @budget.command(
        name="rollover",
        description="Carry every team's unspent budget from a past season into the active one",
    )
    @app_commands.describe(from_season="Name of the season to roll over FROM")
    async def budget_rollover(
        self, interaction: discord.Interaction, from_season: str,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            lines = await workflow.rollover_budgets(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                from_season_name=from_season,
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(f"\u274c {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            _render_rollover(from_season, lines), ephemeral=True
        )

    @budget.command(name="config", description="Show or set budget rules for the active season")
    @app_commands.describe(
        tier="Tier code for a tier-specific override (blank = season default)",
        enforce="Enforce budgets on signings and trades",
        rollover="Carry unspent budget into the next season",
        opening_m="Opening budget per team in $M",
        per_point_m="Race earnings per championship point in $M (e.g. 0.05)",
        dnf_m="Penalty per DNF in $M",
        dns_m="Penalty per no-show (DNS) in $M",
        per_incident_pt_m="Penalty per incident point in $M",
    )
    async def budget_config(
        self,
        interaction: discord.Interaction,
        tier: str | None = None,
        enforce: bool | None = None,
        rollover: bool | None = None,
        opening_m: str | None = None,
        per_point_m: str | None = None,
        dnf_m: str | None = None,
        dns_m: str | None = None,
        per_incident_pt_m: str | None = None,
    ) -> None:
        if not await _admin_or_deny(interaction):
            return
        assert interaction.guild_id is not None
        provided = {
            "enforce": enforce, "rollover": rollover, "opening_m": opening_m,
            "per_point_m": per_point_m, "dnf_m": dnf_m, "dns_m": dns_m,
            "per_incident_pt_m": per_incident_pt_m,
        }
        try:
            current = await workflow.get_budget_config(
                guild_id=interaction.guild_id, tier=tier,
            )
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
            return

        if all(v is None for v in provided.values()):
            if current is None:
                await interaction.response.send_message(
                    "Budgets are not configured for this season. Pass at least "
                    "`opening_m` to create the config (other values default to "
                    "the F1 preset when omitted).",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=_render_budget_config(current, tier), ephemeral=True
            )
            return

        def money(raw: str | None, fallback: Decimal | None) -> Decimal:
            if raw is None:
                if fallback is None:
                    raise InvalidOperation("value required when no config exists yet")
                return fallback
            return Decimal(raw.strip().lstrip("$").rstrip("Mm"))

        base = current or await workflow.get_budget_config(
            guild_id=interaction.guild_id, tier=None,
        )
        try:
            new_cfg = await workflow.set_budget_config(
                guild_id=interaction.guild_id,
                tier=tier,
                enforce_budget=enforce if enforce is not None else (
                    base.enforce_budget if base else True
                ),
                rollover_enabled=rollover if rollover is not None else (
                    base.rollover_enabled if base else True
                ),
                opening_budget=money(opening_m, base.opening_budget if base else None),
                earnings_per_point=money(
                    per_point_m, base.earnings_per_point if base else None
                ),
                dnf_penalty=money(dnf_m, base.dnf_penalty if base else None),
                dns_penalty=money(dns_m, base.dns_penalty if base else None),
                penalty_per_incident_pt=money(
                    per_incident_pt_m, base.penalty_per_incident_pt if base else None
                ),
            )
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse a value: {exc}", ephemeral=True
            )
            return
        except workflow.WorkflowError as exc:
            await interaction.response.send_message(f"\u274c {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            content="\u2705 Budget config saved.",
            embed=_render_budget_config(new_cfg, tier),
            ephemeral=True,
        )



def _parse_color(raw: str | None) -> int | None:
    if raw is None:
        return None
    text = raw.strip().lstrip("#")
    if not text:
        return None
    try:
        return int(text, 16) if not text.isdigit() else int(text)
    except ValueError:
        return None


async def _resolve_tier_id(conn, season_id: int, code: str | None) -> int | None:
    if code is None:
        return None
    tier = await queries.fetch_tier(conn, season_id, code)
    return tier.id if tier else None


def _tier_role_unavailable(guild: discord.Guild, tier) -> str | None:
    """Human-facing reason a tier can't be sync'd, or None if it's usable."""
    if tier.tier_role_id is None:
        return (
            f"tier `{tier.code}` has no Discord role set — "
            "assign one via `/market-admin tier edit`"
        )
    if guild.get_role(tier.tier_role_id) is None:
        return (
            f"tier `{tier.code}`'s role id {tier.tier_role_id} "
            "isn't in this guild"
        )
    return None


def _seeds_from_role(role: discord.Role) -> list[driver_ops.DriverSeed]:
    """Non-bot members of the role, as enrolment seeds."""
    return [
        driver_ops.DriverSeed(member_id=m.id, display_name=m.display_name)
        for m in role.members
        if not m.bot
    ]


def _delta_arrow(delta: Decimal) -> str:
    if delta > Decimal("0"):
        return "▲"
    if delta < Decimal("0"):
        return "▼"
    return "•"


def _render_valuation_preview(
    *,
    run_id: int,
    tier_code: str,
    round_label: str,
    published: bool,
    outcomes,
) -> str:
    """
    Two-line-per-driver Discord-safe layout (see CLAUDE.md §5). Mirrors
    what the public `/market view` will render in Phase 3.
    """
    header = _preview_header(run_id, tier_code, round_label, published)
    body_lines: list[str] = []
    for v in outcomes:
        body_lines.extend(_preview_body_lines(
            rank=v.rank_in_tier,
            name=v.display_name,
            market_value=v.market_value,
            delta=v.delta,
            capped=v.capped,
        ))
    return _join_preview(header, body_lines, run_id, published, len(outcomes))


def _render_valuation_preview_from_rows(
    *,
    run_id: int,
    tier_code: str,
    round_label: str,
    published: bool,
    rows,
) -> str:
    header = _preview_header(run_id, tier_code, round_label, published)
    body_lines: list[str] = []
    for row in rows:
        body_lines.extend(_preview_body_lines(
            rank=row["rank_in_tier"],
            name=row["display_name"],
            market_value=row["market_value"],
            delta=row["delta"],
            capped=row["capped"],
        ))
    return _join_preview(header, body_lines, run_id, published, len(rows))


def _preview_header(run_id: int, tier_code: str, round_label: str, published: bool) -> str:
    status = "🟢 PUBLISHED" if published else "⚪ DRY-RUN"
    return (
        f"**Valuation run `{run_id}` — {tier_code} · {round_label}**  {status}"
    )


def _preview_body_lines(
    *,
    rank: int,
    name: str,
    market_value: Decimal,
    delta: Decimal,
    capped: bool,
) -> list[str]:
    cap_flag = "  ⚠ capped" if capped else ""
    return [
        f"{rank}. {name}",
        f"   Market: {format_money(market_value)}  |  "
        f"Week: {_delta_arrow(delta)} {format_pl(delta)}{cap_flag}",
    ]


def _join_preview(
    header: str,
    body_lines: list[str],
    run_id: int,
    published: bool,
    total_drivers: int,
) -> str:
    # Discord content limit is 2000 chars; leave headroom for the tail.
    max_body_lines = 40  # ~20 drivers at 2 lines each
    truncated = False
    if len(body_lines) > max_body_lines:
        body_lines = body_lines[:max_body_lines]
        truncated = True
    tail_bits: list[str] = []
    if truncated:
        tail_bits.append(f"…truncated. {total_drivers} drivers total.")
    if not published:
        tail_bits.append(
            f"Preview only. To lock in: `/market-admin valuation publish run_id: {run_id}`."
        )
    tail = ("\n" + "\n".join(tail_bits)) if tail_bits else ""
    return header + "\n" + "\n".join(body_lines) + tail


def _render_budget_outcome(outcome: workflow.ImportOutcome) -> list[str]:
    """Budget lines for the results-import receipt; empty when not enforced."""
    b = outcome.budget
    if b is None:
        return []
    lines = [
        f"\U0001f4b0 Budgets: {b.entries_written} entr(ies) written \u2014 "
        f"+{format_money(b.total_credited)} earned, "
        f"\u2212{format_money(b.total_debited)} in penalties"
        + (f", {b.corrections_written} correction(s)" if b.corrections_written else "")
        + "."
    ]
    if outcome.budget_unattributed:
        names = outcome.budget_unattributed
        lines.append(
            f"\u26a0 {len(names)} driver(s) had no active contract, so no team was "
            "charged or credited: "
            + ", ".join(names[:_MAX_LISTED_NAMES])
            + ("\u2026" if len(names) > _MAX_LISTED_NAMES else "")
            + "."
        )
    return lines


def _render_budget_summary(s: workflow.TeamBudgetSummary) -> discord.Embed:
    embed = discord.Embed(
        title=f"Budget \u2014 {s.team_name}",
        description=f"Season **{s.season_name}**",
        colour=discord.Colour.green() if s.available >= 0 else discord.Colour.red(),
    )
    binding = "budget" if s.available < s.cap_space else "cap"
    embed.add_field(
        name="Money",
        value="\n".join([
            f"Budget: {format_money(s.balance)}",
            f"Effective payroll: {format_money(s.effective_payroll)}",
            f"Available to spend: {format_money(s.available)}",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Spending cap (league rule)",
        value="\n".join([
            f"Cap: {format_money(s.salary_cap)}",
            f"Cap space: {format_money(s.cap_space)}",
            f"Binding limit right now: **{binding}**",
        ]),
        inline=False,
    )
    if s.totals_by_kind:
        embed.add_field(
            name="Where the money came from",
            value="\n".join(
                f"{kind}: {format_pl(total)}"
                for kind, total in sorted(s.totals_by_kind.items())
            ),
            inline=False,
        )
    if s.recent:
        embed.add_field(
            name=f"Last {len(s.recent)} entr(ies)",
            value="\n".join(
                f"{e.created_at:%m-%d} {format_pl(e.amount)} {e.kind}"
                + (" (correction)" if e.is_correction else "")
                + (f" \u2014 {e.note}" if e.note else "")
                for e in s.recent
            ),
            inline=False,
        )
    embed.set_footer(
        text=(
            f"rollover {'on' if s.config.rollover_enabled else 'off'} \u00b7 "
            f"DNF \u2212{format_money(s.config.dnf_penalty)} \u00b7 "
            f"DNS \u2212{format_money(s.config.dns_penalty)} \u00b7 "
            f"{format_money(s.config.penalty_per_incident_pt)}/incident pt \u00b7 "
            f"{format_money(s.config.earnings_per_point)}/point"
        )
    )
    return embed


def _render_budget_config(cfg, tier: str | None) -> discord.Embed:
    scope = f"tier `{tier}` override" if cfg.tier_id is not None else "season default"
    if tier and cfg.tier_id is None:
        scope = f"season default (no `{tier}` override)"
    embed = discord.Embed(title=f"Budget config \u2014 {scope}", colour=discord.Colour.blurple())
    embed.add_field(
        name="Rules",
        value="\n".join([
            f"Enforce on signings/trades: {'yes' if cfg.enforce_budget else 'no'}",
            f"Rollover between seasons: {'yes' if cfg.rollover_enabled else 'no'}",
            f"Opening budget per team: {format_money(cfg.opening_budget)}",
        ]),
        inline=False,
    )
    embed.add_field(
        name="Earnings",
        value=f"Per championship point: {format_money(cfg.earnings_per_point)}",
        inline=False,
    )
    embed.add_field(
        name="Penalties (debited from the team budget)",
        value="\n".join([
            f"Per DNF: {format_money(cfg.dnf_penalty)}",
            f"Per no-show (DNS): {format_money(cfg.dns_penalty)}",
            f"Per incident point: {format_money(cfg.penalty_per_incident_pt)}",
        ]),
        inline=False,
    )
    embed.set_footer(text="The spending cap is separate: /market-admin config show")
    return embed


def _render_rollover(from_season: str, lines) -> str:
    out = [f"\u2705 Rollover from **{from_season}** into the active season:"]
    for line in lines:
        if line.skipped_reason:
            out.append(f"\u2022 {line.team_name}: skipped ({line.skipped_reason})")
        else:
            out.append(
                f"\u2022 {line.team_name}: carried {format_pl(line.carried)} "
                f"(had {format_money(line.from_balance)}, "
                f"payroll {format_money(line.from_payroll)})"
            )
    out.append(
        "Each team also received its opening balance for the new season if it "
        "didn't have one. Award prize money with `/market-admin budget award`."
    )
    return "\n".join(out)


def _render_carry_over(result: approvals.CarryOverResult) -> discord.Embed:
    report = result.report
    outcome = report.outcome
    embed = discord.Embed(
        title=f"Contract carry-over: {report.from_season_name} \u2192 {report.to_season_name}",
        description=(
            f"**{outcome.carried}** carried \u2022 **{outcome.expired}** expired \u2022 "
            f"**{outcome.skipped}** need attention \u2022 "
            f"{outcome.drivers_created} driver rows created"
        ),
        colour=discord.Colour.orange() if outcome.skipped or outcome.over_cap
        else discord.Colour.green(),
    )
    if not outcome.lines:
        embed.add_field(
            name="Nothing to do",
            value=(
                f"No active contracts remain in **{report.from_season_name}**. "
                "Either carry-over already ran or nothing was signed."
            ),
            inline=False,
        )
        return embed

    def team(line) -> str:
        return report.team_names.get(line.team_id, f"team {line.team_id}")

    carried = [
        f"\u2022 {ln.display_name} \u2014 {team(ln)} {format_money(ln.contract_value)}, "
        f"season {ln.season_index_from + 1}/{ln.term_seasons}"
        + (f" (now `{ln.new_tier_code}`)" if ln.new_tier_code != ln.tier_code else "")
        for ln in outcome.lines if ln.outcome == "carried"
    ]
    expired = [
        f"\u2022 {ln.display_name} \u2014 {team(ln)} {format_money(ln.contract_value)}, "
        f"{ln.term_seasons}-season deal complete \u2192 free agent"
        for ln in outcome.lines if ln.outcome == "expired"
    ]
    skipped = [
        f"\u2022 {ln.display_name} \u2014 {team(ln)}: {ln.reason} (contract `{ln.contract_id}` "
        "left active)"
        for ln in outcome.lines if ln.outcome == "skipped"
    ]
    for name, rows in (("Carried", carried), ("Expired", expired), ("Needs attention", skipped)):
        if rows:
            shown = rows[:_MAX_LISTED_RESULTS]
            if len(rows) > len(shown):
                shown.append(f"\u2026 and {len(rows) - len(shown)} more")
            embed.add_field(name=name, value="\n".join(shown), inline=False)
    if outcome.over_cap:
        embed.add_field(
            name="\u26a0 Over the spending cap after carry-over",
            value="\n".join(
                f"\u2022 {report.team_names.get(oc.team_id, oc.team_id)}: payroll "
                f"{format_money(oc.payroll)} vs cap {format_money(oc.salary_cap)} "
                f"(over by {format_money(oc.over_by)})"
                for oc in outcome.over_cap
            ) + "\nCarried deals are binding, so nothing was blocked. Resolve with a "
            "release, buyout, or trade before that team signs anyone.",
            inline=False,
        )
    if result.role_warnings:
        embed.add_field(
            name="Role warnings",
            value="\n".join(f"\u2022 {w}" for w in result.role_warnings[:_MAX_LISTED_ERRORS]),
            inline=False,
        )
    embed.set_footer(
        text="Re-running is safe: only rows still active in the old season are touched. "
        "Run `/market-admin budget rollover` for the money side."
    )
    return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminMarketCog(bot))
