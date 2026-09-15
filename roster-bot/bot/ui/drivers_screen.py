"""
Driver enrolment + per-driver admin actions.

`/market-admin driver add|sync|sync-all` back the enrolment surface;
`/market-admin void|set-status|promote|relegate` back the per-driver
actions. This screen is the guided front for both.

The list surface offers:
  * **Enrol member** — a three-step wizard (tier → member → status) so a
    single sign-up never means typing a slash command with an @mention
    and remembering the tier code.
  * **Sync tier** — enrol every non-bot member of a tier's Discord role
    as `active`; the workflow helper is idempotent, so re-running is a
    no-op for already-registered members.
  * **Sync all** — same, walked across every tier that has a role set.

The picker at the top opens a driver-detail view whose four buttons
mirror the /market-admin per-driver commands (void / set-status /
promote / relegate). Cap-adjust is team-scoped, not driver-scoped, so
it isn't here — Stage 4 gives it a proper home.

All persistence goes through `bot/workflow.py`. This module walks Discord
role members to build the enrolment seeds, because the workflow layer
must stay Discord-free — but it never touches queries directly.
"""

from __future__ import annotations

import discord

from bot import workflow
from bot.market import driver_ops
from bot.market.money import format_money, format_pl
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
        *,
        row: int | None = None,
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
            row=row,
        )
        self._owner = parent
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
        await self._owner.reload(
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
    """List, enrol, sync, and drill into a driver for per-driver actions."""

    def __init__(
        self,
        *,
        summaries: list[workflow.TierDriverSummary],
        drivers: list[workflow.DriverForPanel] | None = None,
        opener_id: int,
        on_back: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.summaries = summaries
        self.drivers = drivers or []

        # Rows stack top-down: picker (if any), sync select (if any), buttons.
        # Discord caps a view at 5 rows; two selects + one button row = 3.
        row = 0
        if self.drivers:
            self.add_item(_DriverPickerSelect(self, self.drivers, row=row))
            row += 1

        eligible = [s for s in summaries if s.tier_role_id is not None]
        if eligible:
            self.add_item(_SyncTierSelect(self, eligible, row=row))
            row += 1

        if summaries:
            self.add_item(_EnrolMemberButton(row=row))
        if eligible:
            self.add_item(_SyncAllButton(row=row))
        self.add_item(BackButton(on_back, row=row))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        summaries = await workflow.list_driver_summary(interaction.guild_id)
        drivers = await workflow.list_drivers_in_season(interaction.guild_id)
        view = DriversView(
            summaries=summaries,
            drivers=drivers,
            opener_id=self.opener_id,
            on_back=self._on_back,
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


# ── Driver picker + detail view ──────────────────────────────────────


def build_driver_detail_embed(detail: workflow.DriverForPanel) -> discord.Embed:
    """
    One embed summarising the driver: team, status, money, P/L.

    Degrades cleanly when the driver has no active contract — the money
    lines drop rather than showing zeros that look like real values.
    """
    team = detail.active_team_name or "free agent"
    embed = discord.Embed(
        title=f"👤 {detail.display_name}",
        description=(
            f"Tier `{detail.tier_code}` — {detail.tier_label} · "
            f"team **{team}** · status `{detail.status}`"
        ),
        color=COLOR_INFO,
    )
    if detail.market_value is not None:
        embed.add_field(
            name="Market value", value=format_money(detail.market_value), inline=True
        )
    else:
        embed.add_field(
            name="Market value",
            value="— (no published run yet)",
            inline=True,
        )
    if detail.contract_value is not None:
        embed.add_field(
            name="Contract value",
            value=format_money(detail.contract_value),
            inline=True,
        )
        pl = detail.pl
        assert pl is not None  # both market and contract are populated
        embed.add_field(name="P/L", value=format_pl(pl), inline=True)
    else:
        embed.add_field(
            name="Contract",
            value="— (no active contract)",
            inline=True,
        )
    embed.set_footer(
        text=(
            "Void / Set status / Promote / Relegate go through the same "
            "workflow the /market-admin commands use."
        )
    )
    return embed


def _picker_label(d: workflow.DriverForPanel) -> str:
    team = d.active_team_name or "FA"
    return f"{d.display_name} · {d.tier_code} · {team}"[:100]


def _picker_description(d: workflow.DriverForPanel) -> str:
    value = format_money(d.market_value) if d.market_value is not None else "—"
    return f"{d.status} · market {value}"[:100]


class _DriverPickerSelect(discord.ui.Select):
    """
    Pick a driver to act on. Capped at Discord's 25-option limit; the
    workflow layer already sorts by (tier, contract value desc), so the
    highest-visibility drivers are the top 25.
    """

    def __init__(
        self,
        parent: DriversView,
        drivers: list[workflow.DriverForPanel],
        *,
        row: int | None = None,
    ) -> None:
        shown = drivers[:SELECT_MAX_OPTIONS]
        overflow = len(drivers) - len(shown)
        placeholder = "Pick a driver…"
        if overflow > 0:
            placeholder = (
                f"Pick a driver… (showing top {len(shown)} of {len(drivers)})"
            )
        super().__init__(
            placeholder=placeholder,
            options=[
                discord.SelectOption(
                    label=_picker_label(d),
                    value=str(d.driver_id),
                    description=_picker_description(d),
                )
                for d in shown
            ],
            disabled=not shown,
            row=row,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        driver_id = int(self.values[0])
        try:
            detail = await workflow.fetch_driver_detail(
                interaction.guild_id, driver_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        view = _DriverDetailView(
            detail=detail,
            opener_id=self._owner.opener_id,
            parent=self._owner,
        )
        await interaction.edit_original_response(
            embed=build_driver_detail_embed(detail), view=view
        )


class _DriverDetailView(AdminOwnedView):
    """One-driver detail with the four /market-admin actions + Back."""

    def __init__(
        self,
        *,
        detail: workflow.DriverForPanel,
        opener_id: int,
        parent: DriversView,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.detail = detail
        self.parent = parent

        self.add_item(_VoidButton(disabled=detail.active_contract_id is None))
        self.add_item(_SetStatusButton())
        self.add_item(_MoveTierButton(label="Promote", direction="promote"))
        self.add_item(_MoveTierButton(label="Relegate", direction="relegate"))
        self.add_item(BackButton(self._back, row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def refresh(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        """Re-fetch the driver and redraw the detail view in place."""
        try:
            detail = await workflow.fetch_driver_detail(
                interaction.guild_id, self.detail.driver_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        view = _DriverDetailView(
            detail=detail, opener_id=self.opener_id, parent=self.parent
        )
        embed = build_driver_detail_embed(detail)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)


class _VoidButton(discord.ui.Button):
    def __init__(self, *, disabled: bool) -> None:
        super().__init__(
            label="Void contract",
            style=discord.ButtonStyle.danger,
            disabled=disabled,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _DriverDetailView)
        await interaction.response.send_modal(_VoidNoteModal(view))


class _VoidNoteModal(discord.ui.Modal, title="Void contract"):
    def __init__(self, parent: _DriverDetailView) -> None:
        super().__init__()
        self.parent = parent
        self.note = discord.ui.TextInput(
            label="Reason (goes to the audit ledger)",
            placeholder="e.g. driver inactive, contract dispute…",
            required=False,
            max_length=200,
        )
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        note = str(self.note.value).strip() or None
        try:
            await workflow.void_active_contract(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                driver_id=self.parent.detail.driver_id,
                note=note,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self.parent.refresh(
            interaction,
            note=f"✅ Voided **{self.parent.detail.display_name}**'s contract.",
        )


class _SetStatusButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Set status", style=discord.ButtonStyle.secondary, row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _DriverDetailView)
        # Swap the button row for a status select; back stays on row 1.
        picker_view = _StatusPickerView(parent=view)
        await interaction.response.edit_message(view=picker_view)


class _StatusPickerView(AdminOwnedView):
    """Ephemeral pick-a-status swap of the detail view's button row."""

    def __init__(self, *, parent: _DriverDetailView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.add_item(_StatusSelect(parent))
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.refresh(interaction)


class _StatusSelect(discord.ui.Select):
    def __init__(self, parent: _DriverDetailView) -> None:
        super().__init__(
            placeholder=f"New status (currently `{parent.detail.status}`)",
            options=[
                discord.SelectOption(
                    label=label,
                    value=code,
                    default=(code == parent.detail.status),
                )
                for code, label in workflow.DRIVER_STATUS_CHOICES
            ],
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        chosen = self.values[0]
        try:
            await workflow.set_driver_status(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                driver_id=self._owner.detail.driver_id,
                status=chosen,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        note = (
            f"✅ **{self._owner.detail.display_name}**: "
            f"`{self._owner.detail.status}` → `{chosen}`."
            if chosen != self._owner.detail.status
            else f"ℹ️ Status was already `{chosen}` — no change."
        )
        await self._owner.refresh(interaction, note=note)


class _MoveTierButton(discord.ui.Button):
    """
    Promote or Relegate. Both open the same tier picker; the workflow
    layer figures out the direction from the target tier's rank.
    """

    def __init__(self, *, label: str, direction: str) -> None:
        super().__init__(
            label=label,
            style=discord.ButtonStyle.primary,
            row=0,
        )
        self.direction = direction

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _DriverDetailView)
        tiers = await workflow.list_tier_choices(interaction.guild_id)
        tiers = [(c, label) for c, label in tiers if c != view.detail.tier_code]
        if not tiers:
            await report_error(
                interaction,
                "No other tier to move this driver to.",
            )
            return
        picker_view = _MoveTierPickerView(
            parent=view, direction=self.direction, tiers=tiers
        )
        await interaction.response.edit_message(view=picker_view)


class _MoveTierPickerView(AdminOwnedView):
    def __init__(
        self,
        *,
        parent: _DriverDetailView,
        direction: str,
        tiers: list[tuple[str, str]],
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.add_item(_MoveTierSelect(parent, direction, tiers))
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.refresh(interaction)


class _MoveTierSelect(discord.ui.Select):
    def __init__(
        self,
        parent: _DriverDetailView,
        direction: str,
        tiers: list[tuple[str, str]],
    ) -> None:
        super().__init__(
            placeholder=f"{direction.capitalize()} to which tier?",
            options=[
                discord.SelectOption(label=label[:100], value=code)
                for code, label in tiers[:SELECT_MAX_OPTIONS]
            ],
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        target = self.values[0]
        try:
            await workflow.move_driver_to_tier(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                driver_id=self._owner.detail.driver_id,
                new_tier_code=target,
                note=None,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._owner.refresh(
            interaction,
            note=(
                f"✅ Moved **{self._owner.detail.display_name}** to tier "
                f"`{target}`. Their active contract (if any) moved with them."
            ),
        )


async def open_drivers(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the /league home screen's Drivers button."""
    summaries = await workflow.list_driver_summary(interaction.guild_id)
    drivers = await workflow.list_drivers_in_season(interaction.guild_id)
    view = DriversView(
        summaries=summaries,
        drivers=drivers,
        opener_id=opener_id,
        on_back=on_back,
    )
    await interaction.response.edit_message(
        embed=build_drivers_embed(summaries), view=view
    )
