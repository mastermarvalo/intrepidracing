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


# ── helpers ──────────────────────────────────────────────────────────────


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


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminMarketCog(bot))
