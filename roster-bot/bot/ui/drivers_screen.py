"""
Driver enrolment as an interactive screen.

`/market-admin driver add|sync|sync-all` are the underlying commands.
This screen is the guided front for them: it lists tiers with their
current driver counts, then offers three actions:

  * **Enrol member** — a three-step wizard (tier → member → status) so a
    single sign-up never means typing a slash command with an @mention
    and remembering the tier code.
  * **Sync tier** — enrol every non-bot member of a tier's Discord role
    as `active`; the workflow helper is idempotent, so re-running is a
    no-op for already-registered members.
  * **Sync all** — same, walked across every tier that has a role set.

All persistence goes through `bot/workflow.py`. This module walks Discord
role members to build the enrolment seeds, because the workflow layer
must stay Discord-free — but it never touches queries directly.
"""

from __future__ import annotations

import discord

from bot import workflow
from bot.market import driver_ops
from bot.ui.base import (
    COLOR_INFO,
    COLOR_OK,
    COLOR_WARN,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)


def build_drivers_embed(
    summaries: list[workflow.TierDriverSummary],
) -> discord.Embed:
    if not summaries:
        return discord.Embed(
            title="👥 Drivers",
            description=(
                "No tiers in the active season yet.\n\n"
                "Add a tier in **Setup** first — enrolment writes the "
                "market-side driver record for a member in a specific tier."
            ),
            color=COLOR_INFO,
        )

    lines: list[str] = []
    tiers_without_role: list[str] = []
    for s in summaries:
        role_note = "" if s.tier_role_id is not None else " · ⚠ no role set"
        lines.append(
            f"**{s.code}** — {s.label} · {s.driver_count} driver(s){role_note}"
        )
        if s.tier_role_id is None:
            tiers_without_role.append(s.code)

    embed = discord.Embed(
        title="👥 Drivers",
        description=truncate_field("\n".join(lines)),
        color=COLOR_WARN if tiers_without_role else COLOR_OK,
    )
    if tiers_without_role:
        codes = ", ".join(f"`{c}`" for c in tiers_without_role)
        embed.add_field(
            name="⚠ Tiers with no Discord role",
            value=(
                f"{codes} can't be sync'd until a role is attached. "
                "Set one via Setup → Tiers, or `/market-admin tier edit`."
            ),
            inline=False,
        )
    embed.set_footer(
        text="Equivalent commands: /market-admin driver add · sync · sync-all"
    )
    return embed


# ── Enrol member: tier → member → status ─────────────────────────────


class _EnrolTierSelect(discord.ui.Select):
    def __init__(
        self, flow: _EnrolMemberFlow, tiers: list[tuple[str, str]]
    ) -> None:
        super().__init__(
            placeholder="Which tier?",
            options=[
                discord.SelectOption(label=label[:100], value=code)
                for code, label in tiers[:SELECT_MAX_OPTIONS]
            ],
            disabled=not tiers,
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.tier_code = self.values[0]
        await self._flow.advance(interaction)


class _EnrolMemberSelect(discord.ui.UserSelect):
    def __init__(self, flow: _EnrolMemberFlow) -> None:
        super().__init__(placeholder="Which member?", max_values=1)
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        user = self.values[0]
        if getattr(user, "bot", False):
            await report_error(
                interaction,
                f"{user.display_name} is a bot; bots cannot be drivers.",
            )
            return
        self._flow.member_id = user.id
        self._flow.member_display_name = user.display_name
        await self._flow.advance(interaction)


class _EnrolStatusSelect(discord.ui.Select):
    def __init__(self, flow: _EnrolMemberFlow) -> None:
        super().__init__(
            placeholder="Initial status?",
            options=[
                discord.SelectOption(label=label, value=code)
                for code, label in workflow.DRIVER_STATUS_CHOICES
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self._flow.tier_code is not None
        assert self._flow.member_id is not None
        assert self._flow.member_display_name is not None
        try:
            report = await workflow.enrol_driver(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                tier_code=self._flow.tier_code,
                member_id=self._flow.member_id,
                display_name=self._flow.member_display_name,
                status=self.values[0],
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        if report.created:
            note = (
                f"✅ Enrolled **{report.display_name}** in tier "
                f"`{report.tier_code}` as `{self.values[0]}`."
            )
        else:
            note = (
                f"ℹ️ **{report.display_name}** is already enrolled in tier "
                f"`{report.tier_code}`."
            )
        await self._flow.parent.reload(interaction, note=note)


class _EnrolMemberFlow(AdminOwnedView):
    def __init__(self, *, opener_id: int, parent: DriversView) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_code: str | None = None
        self.member_id: int | None = None
        self.member_display_name: str | None = None

    async def start(
        self,
        interaction: discord.Interaction,
        tiers: list[tuple[str, str]],
    ) -> None:
        self.clear_items()
        self.add_item(_EnrolTierSelect(self, tiers))
        self.add_item(BackButton(self._back, label="Cancel", row=1))
        embed = discord.Embed(
            title="👥 Enrol a member",
            description="Step 1 of 3 — pick the tier.",
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def advance(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        if self.member_id is None:
            self.add_item(_EnrolMemberSelect(self))
            step = f"Step 2 of 3 — pick the member for tier `{self.tier_code}`."
        else:
            self.add_item(_EnrolStatusSelect(self))
            step = (
                f"Step 3 of 3 — pick the initial status for "
                f"**{self.member_display_name}** in tier `{self.tier_code}`."
            )
        self.add_item(BackButton(self._back, label="Cancel", row=1))
        embed = discord.Embed(
            title="👥 Enrol a member",
            description=step,
            color=COLOR_INFO,
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


# ── Sync one tier ────────────────────────────────────────────────────


class _SyncTierSelect(discord.ui.Select):
    """Pick a tier to sync; only tiers with a Discord role attached appear."""

    def __init__(
        self,
        parent: DriversView,
        eligible: list[workflow.TierDriverSummary],
    ) -> None:
        super().__init__(
            placeholder="Sync which tier?",
            options=[
                discord.SelectOption(
                    label=f"{s.code} — {s.label}"[:100],
                    value=s.code,
                    description=f"{s.driver_count} currently enrolled"[:100],
                )
                for s in eligible[:SELECT_MAX_OPTIONS]
            ],
            disabled=not eligible,
        )
        self._parent = parent
        self._by_code = {s.code: s for s in eligible}

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        tier_code = self.values[0]
        summary = self._by_code[tier_code]
        assert summary.tier_role_id is not None
        assert interaction.guild is not None
        role = interaction.guild.get_role(summary.tier_role_id)
        if role is None:
            await report_error(
                interaction,
                f"Tier `{tier_code}`'s role id {summary.tier_role_id} "
                "isn't in this guild.",
            )
            return
        seeds = _seeds_from_role(role)
        try:
            report = await workflow.sync_drivers_in_tier(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                tier_code=tier_code,
                seeds=seeds,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._parent.reload(
            interaction,
            note=(
                f"✅ Tier `{report.tier_code}`: enrolled **{report.created}**, "
                f"already-registered **{report.already_registered}**."
            ),
        )


# ── Sync all tiers at once ───────────────────────────────────────────


class _SyncAllButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Sync all tiers",
            style=discord.ButtonStyle.primary,
            emoji="🔄",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = self.view
        assert isinstance(view, DriversView)
        assert interaction.guild is not None

        seeds_by_tier: dict[str, list[driver_ops.DriverSeed]] = {}
        skipped_by_tier: dict[str, str] = {}
        for s in view.summaries:
            if s.tier_role_id is None:
                skipped_by_tier[s.code] = "no Discord role set"
                continue
            role = interaction.guild.get_role(s.tier_role_id)
            if role is None:
                skipped_by_tier[s.code] = (
                    f"role id {s.tier_role_id} not in guild"
                )
                continue
            seeds_by_tier[s.code] = _seeds_from_role(role)

        try:
            reports = await workflow.sync_drivers_all_tiers(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                seeds_by_tier_code=seeds_by_tier,
                skipped_by_tier_code=skipped_by_tier,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        lines = [_format_sync_line(r) for r in reports]
        await view.reload(
            interaction,
            note="✅ Sync-all complete:\n" + "\n".join(lines),
        )


def _format_sync_line(report: workflow.TierSyncReport) -> str:
    if report.skipped_reason is not None:
        return f"• `{report.tier_code}`: skipped ({report.skipped_reason})"
    return (
        f"• `{report.tier_code}`: enrolled **{report.created}**, "
        f"already-registered **{report.already_registered}**"
    )


# ── Main view ────────────────────────────────────────────────────────


class DriversView(AdminOwnedView):
    """List, enrol, sync one tier, or sync every tier."""

    def __init__(
        self,
        *,
        summaries: list[workflow.TierDriverSummary],
        opener_id: int,
        on_back: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.summaries = summaries

        eligible = [s for s in summaries if s.tier_role_id is not None]
        if eligible:
            self.add_item(_SyncTierSelect(self, eligible))

        if summaries:
            self.add_item(_EnrolMemberButton(row=1))
        if eligible:
            self.add_item(_SyncAllButton(row=1))
        self.add_item(BackButton(on_back, row=1))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        summaries = await workflow.list_driver_summary(interaction.guild_id)
        view = DriversView(
            summaries=summaries, opener_id=self.opener_id, on_back=self._on_back
        )
        embed = build_drivers_embed(summaries)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _EnrolMemberButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Enrol member",
            style=discord.ButtonStyle.success,
            emoji="➕",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, DriversView)
        tiers = await workflow.list_tier_choices(interaction.guild_id)
        if not tiers:
            await report_error(
                interaction,
                "No tiers in the active season yet. Add one in Setup first.",
            )
            return
        flow = _EnrolMemberFlow(opener_id=view.opener_id, parent=view)
        await flow.start(interaction, tiers)


def _seeds_from_role(role: discord.Role) -> list[driver_ops.DriverSeed]:
    """Non-bot members of the role, as enrolment seeds."""
    return [
        driver_ops.DriverSeed(member_id=m.id, display_name=m.display_name)
        for m in role.members
        if not m.bot
    ]


async def open_drivers(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the /league home screen's Drivers button."""
    summaries = await workflow.list_driver_summary(interaction.guild_id)
    view = DriversView(summaries=summaries, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_drivers_embed(summaries), view=view
    )
