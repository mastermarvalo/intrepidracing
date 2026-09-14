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

Later phases add board, approval, void, adjust-cap, and free-agency
window subcommands. This module is `/market-admin` only; public /market
and /contract cogs land in Phase 3 and 4.

Authority: Manage Server (mirrors `_is_admin` in cogs/roster.py). A
commissioner role assigned via `/market-admin config role commissioner`
is stored for later phases to consult but does not yet grant access —
Phase 1 stays with Manage Server so we do not diverge from the existing
permission model until the contract flow needs the split.
"""

from decimal import Decimal, InvalidOperation

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, queries
from bot.market import valuation as valuation_engine
from bot.market.money import format_money, format_pl
from bot.presets import f1 as f1_preset


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
        async with db.connect() as conn:
            existing = await queries.fetch_season_by_name(conn, interaction.guild_id, name)
            if existing is not None:
                await interaction.followup.send(
                    f"A season named **{name}** already exists in this server.",
                    ephemeral=True,
                )
                return
            season_id = await queries.insert_season(conn, interaction.guild_id, name)
            preset_note = ""
            if preset is not None and preset.value == "f1":
                await f1_preset.seed_season(conn, season_id)
                preset_note = (
                    "\nSeeded F1 preset: 3 tiers (t1/t2/t3), lookups, "
                    "valuation factors, default league config."
                )

        await interaction.followup.send(
            f"Created season **{name}** (id `{season_id}`)."
            f"{preset_note}\nUse `/market-admin season activate {name}` to make it active.",
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

        async with db.connect() as conn:
            target = await queries.fetch_season_by_name(conn, interaction.guild_id, name)
            if target is None:
                await interaction.response.send_message(
                    f"No season named **{name}** in this server.", ephemeral=True
                )
                return
            await queries.activate_season(conn, target.id)

        await interaction.response.send_message(
            f"✅ **{name}** is now the active season.", ephemeral=True
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

        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season. Create one with `/market-admin season create` "
                    "and activate it first.",
                    ephemeral=True,
                )
                return
            existing = await queries.fetch_tier(conn, season.id, code)
            if existing is not None:
                await interaction.response.send_message(
                    f"Tier `{code}` already exists in **{season.name}**. "
                    f"Use `/market-admin tier edit`.",
                    ephemeral=True,
                )
                return
            await queries.insert_tier(
                conn,
                season.id,
                code=code,
                label=label,
                rank_order=rank_order,
                tier_role_id=role.id if role else None,
                accent_color=color_int,
            )

        await interaction.response.send_message(
            f"✅ Added tier `{code}` — **{label}** to season **{season.name}**.",
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
            f"Max contract term: {cfg.max_term_seasons} season(s)",
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

    @config.command(name="edit", description="Edit numeric league config (opens modal)")
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

        await interaction.response.send_modal(
            _ConfigModal(season_id=season.id, tier_id=tier_id, current=cfg)
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
                commissioner_role_id=role.id,
            )

        await interaction.response.send_message(
            f"✅ Commissioner role set to {role.mention}.", ephemeral=True
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
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.followup.send(
                    "No active season.", ephemeral=True
                )
                return
            tier_row = await queries.fetch_tier(conn, season.id, tier)
            if tier_row is None:
                await interaction.followup.send(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            cfg = await queries.fetch_league_config_row(conn, season.id, tier_row.id)
            if cfg is None:
                cfg = await queries.fetch_league_config_row(conn, season.id, None)
            if cfg is None:
                await interaction.followup.send(
                    "No league_config for that scope. Seed one with the F1 preset first.",
                    ephemeral=True,
                )
                return

            factor_rows = await queries.fetch_valuation_factors(conn, season.id)
            drivers = await queries.fetch_drivers_in_tier(conn, tier_row.id)
            if not drivers:
                await interaction.followup.send(
                    f"No drivers in tier `{tier}` yet. Add drivers before running a valuation.",
                    ephemeral=True,
                )
                return

            # Build previous-value map from the latest published run per driver.
            prev_values: dict[int, Decimal] = {}
            for d in drivers:
                latest = await queries.fetch_latest_published_valuation(conn, d.id)
                if latest is not None:
                    prev_values[d.id] = latest

            factors = [
                valuation_engine.FactorWeight(
                    code=row["code"],
                    weight=row["weight"],
                    max_contribution=row["max_contribution"],
                )
                for row in factor_rows
            ]
            engine_inputs = [
                valuation_engine.DriverInput(
                    driver_id=d.id,
                    display_name=d.display_name,
                    previous_value=prev_values.get(d.id, cfg.min_salary),
                    factor_values={},
                )
                for d in drivers
            ]
            caps = valuation_engine.MovementCaps(
                weekly=cfg.weekly_move_cap,
                exceptional=cfg.exceptional_move_cap,
            )

            outcomes = valuation_engine.compute_run(factors, engine_inputs, caps)
            run_id = await queries.insert_valuation_run(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                round_label=round_label,
                created_by=interaction.user.id,
                published=False,
            )
            rows = [
                {
                    "driver_id": v.driver_id,
                    "market_value": v.market_value,
                    "previous_value": v.previous_value,
                    "delta": v.delta,
                    "rank_in_tier": v.rank_in_tier,
                    "capped": v.capped,
                    "breakdown": valuation_engine.breakdown_to_json(v.breakdown),
                }
                for v in outcomes
            ]
            await queries.insert_driver_valuations(conn, run_id, rows)

        await interaction.followup.send(
            _render_valuation_preview(
                run_id=run_id,
                tier_code=tier,
                round_label=round_label,
                published=False,
                outcomes=outcomes,
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

        async with db.connect() as conn:
            run = await queries.fetch_valuation_run(conn, run_id)
            if run is None:
                await interaction.response.send_message(
                    f"No valuation run with id `{run_id}`.", ephemeral=True
                )
                return
            if run["published"]:
                await interaction.response.send_message(
                    f"Run `{run_id}` was already published.", ephemeral=True
                )
                return
            await queries.publish_valuation_run(conn, run_id)

        await interaction.response.send_message(
            f"✅ Published run `{run_id}` — market values are now live.",
            ephemeral=True,
        )

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


# ── helpers ──────────────────────────────────────────────────────────────


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


class _ConfigModal(discord.ui.Modal):
    """
    5-field modal for the numeric config values. Additional flags
    (channels, roles, free agency) are set via /market-admin config
    channel|role|free-agency. Splitting them this way keeps every input
    inside Discord's 5-input modal limit without cramming toggles into
    text fields.
    """

    def __init__(
        self, *, season_id: int, tier_id: int | None, current
    ) -> None:
        super().__init__(title="Edit league config")
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current
        self._salary_cap = discord.ui.TextInput(
            label="Salary cap ($M)", default=str(current.salary_cap)
        )
        self._min_salary = discord.ui.TextInput(
            label="Minimum salary ($M)", default=str(current.min_salary)
        )
        self._weekly_cap = discord.ui.TextInput(
            label="Weekly move cap ($M)", default=str(current.weekly_move_cap)
        )
        self._exceptional_cap = discord.ui.TextInput(
            label="Exceptional move cap ($M)",
            default=str(current.exceptional_move_cap),
        )
        self._max_term = discord.ui.TextInput(
            label="Max contract term (seasons)",
            default=str(current.max_term_seasons),
        )
        for widget in (
            self._salary_cap, self._min_salary, self._weekly_cap,
            self._exceptional_cap, self._max_term,
        ):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            salary_cap = Decimal(self._salary_cap.value.strip())
            min_salary = Decimal(self._min_salary.value.strip())
            weekly_cap = Decimal(self._weekly_cap.value.strip())
            exceptional_cap = Decimal(self._exceptional_cap.value.strip())
            max_term = int(self._max_term.value.strip())
        except (InvalidOperation, ValueError) as exc:
            await interaction.response.send_message(
                f"Could not parse a number: {exc}", ephemeral=True
            )
            return

        cur = self._current
        async with db.connect() as conn:
            await queries.upsert_league_config(
                conn,
                season_id=self._season_id,
                tier_id=self._tier_id,
                salary_cap=salary_cap,
                min_salary=min_salary,
                max_salary=cur.max_salary,
                active_driver_slots=cur.active_driver_slots,
                weekly_move_cap=weekly_cap,
                exceptional_move_cap=exceptional_cap,
                max_term_seasons=max_term,
                max_incentive_pct=cur.max_incentive_pct,
                offer_ttl_hours=cur.offer_ttl_hours,
            )

        await interaction.response.send_message(
            "✅ League config saved.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(AdminMarketCog(bot))
