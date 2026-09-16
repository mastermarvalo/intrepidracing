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

The picker at the top is tier-filtered and paged — a league with more
drivers than a select can hold must still be able to reach all of them
— and opens a driver-detail view whose four buttons mirror the
/market-admin per-driver commands (void / set-status / promote /
relegate). Promote and Relegate are the same underlying move, so each
only offers tiers on its own side of the driver's current rank and
names the direction in its confirm step. Cap-adjust is team-scoped, not
driver-scoped, so it isn't here — Stage 4 gives it a proper home.

All persistence goes through `bot/workflow.py`. This module walks Discord
role members to build the enrolment seeds, because the workflow layer
must stay Discord-free — but it never touches queries directly.
"""

from __future__ import annotations

import discord

from bot import workflow
from bot.market import driver_ops
from bot.market.money import format_money, format_pl
from bot.ui import base
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

# One page of the driver picker is one full select. Paging exists because
# a three-tier league passes 25 enrolled drivers almost immediately, and
# every per-driver admin action in the panel sits behind this picker — a
# silent "top 25 of N" made the rest of the league unreachable (G16).
_DRIVER_PAGE_SIZE = SELECT_MAX_OPTIONS
_ALL_TIERS = "__all__"

# Named the way the race-night screen names `/market-admin results
# import` when it runs out of tier buttons: a panel that cannot show
# everything must still say where the rest lives.
_DRIVER_TYPED_FALLBACK = (
    "Typed fallback: `/market-admin admin set-status` · `admin void` · "
    "`admin promote` · `admin relegate`."
)

_PROMOTE = "promote"
_RELEGATE = "relegate"

# (button label, past tense, ladder direction) per move direction.
_DIRECTION_WORDS = {
    _PROMOTE: ("Promote", "Promoted", "up"),
    _RELEGATE: ("Relegate", "Relegated", "down"),
}

# Sentinel for "leave this filter / page where it was" on reload.
_KEEP = object()


def filter_drivers(
    drivers: list[workflow.DriverForPanel], tier_filter: str | None
) -> list[workflow.DriverForPanel]:
    """The drivers a tier filter admits; `None` means every tier."""
    if tier_filter is None:
        return list(drivers)
    return [d for d in drivers if d.tier_code == tier_filter]


def page_count(total: int, page_size: int = _DRIVER_PAGE_SIZE) -> int:
    """How many pages `total` rows need — always at least one."""
    if total <= 0:
        return 1
    return (total + page_size - 1) // page_size


def page_slice(
    rows: list, page: int, page_size: int = _DRIVER_PAGE_SIZE
) -> list:
    """One page of `rows`, clamped so an out-of-range page is empty, not an error."""
    start = max(page, 0) * page_size
    return rows[start : start + page_size]


def build_drivers_embed(
    summaries: list[workflow.TierDriverSummary],
    *,
    picker_total: int | None = None,
    tier_filter: str | None = None,
    page: int = 0,
    pages: int = 1,
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
    if picker_total is not None:
        embed.add_field(
            name="Driver picker",
            value=truncate_field(
                _picker_state_text(
                    picker_total, tier_filter=tier_filter, page=page, pages=pages
                )
            ),
            inline=False,
        )
    embed.set_footer(
        text="Equivalent commands: /market-admin driver add · sync · sync-all"
    )
    return embed


def _picker_state_text(
    total: int, *, tier_filter: str | None, page: int, pages: int
) -> str:
    """
    What the picker below is currently showing, stated rather than implied.

    The old picker said "showing top 25 of N" and offered no way to see
    the rest; this says which slice is on screen, how to move, and which
    typed command reaches a driver directly.
    """
    scope = f"tier `{tier_filter}`" if tier_filter else "all tiers"
    if total == 0:
        return (
            f"No enrolled drivers in {scope}. Enrol one below, or clear the "
            f"tier filter.\n{_DRIVER_TYPED_FALLBACK}"
        )
    head = f"Page **{page + 1}** of **{pages}** · {total} driver(s) in {scope}."
    if pages > 1:
        head += " Use the tier filter and ◀ ▶ to reach the rest."
    return f"{head}\n{_DRIVER_TYPED_FALLBACK}"


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
        tier_filter: str | None = None,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.summaries = summaries
        self.drivers = drivers or []

        # A filter naming a tier that no longer exists (deleted, or the
        # season changed under the panel) falls back to "all tiers"
        # rather than rendering an empty screen with no way out.
        if tier_filter is not None and all(
            s.code != tier_filter for s in summaries
        ):
            tier_filter = None
        self.tier_filter = tier_filter
        self.filtered = filter_drivers(self.drivers, tier_filter)
        self.pages = page_count(len(self.filtered))
        self.page = min(max(page, 0), self.pages - 1)
        shown = page_slice(self.filtered, self.page)

        # Rows stack top-down: tier filter, picker, paging, sync select,
        # buttons. Discord caps a view at 5 rows, which is exactly what
        # the fullest case uses.
        row = 0
        if self.drivers and len(summaries) > 1:
            self.add_item(_DriverTierFilterSelect(self, summaries, row=row))
            row += 1
        if self.drivers:
            self.add_item(
                _DriverPickerSelect(
                    self,
                    shown,
                    row=row,
                    page=self.page,
                    pages=self.pages,
                    total=len(self.filtered),
                )
            )
            row += 1
        if self.pages > 1:
            self.add_item(_DriverPageButton(back=True, row=row))
            self.add_item(_DriverPageButton(back=False, row=row))
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
        self,
        interaction: discord.Interaction,
        *,
        note: str | None = None,
        tier_filter: object = _KEEP,
        page: object = _KEEP,
    ) -> None:
        """
        Re-read the league and redraw, keeping the filter and page unless
        told otherwise.

        Always a fresh read: an enrolment or a tier move changes which
        drivers belong on which page, so paging off the list captured
        when the screen opened would show a stale roster.
        """
        new_filter = self.tier_filter if tier_filter is _KEEP else tier_filter
        new_page = self.page if page is _KEEP else page
        summaries = await workflow.list_driver_summary(interaction.guild_id)
        drivers = await workflow.list_drivers_in_season(interaction.guild_id)
        view = DriversView(
            summaries=summaries,
            drivers=drivers,
            opener_id=self.opener_id,
            on_back=self._on_back,
            tier_filter=new_filter if isinstance(new_filter, str) else None,
            page=new_page if isinstance(new_page, int) else 0,
        )
        embed = build_drivers_embed(
            summaries,
            picker_total=len(view.filtered) if drivers else None,
            tier_filter=view.tier_filter,
            page=view.page,
            pages=view.pages,
        )
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
        # P/L needs BOTH sides. A driver can hold a contract before any
        # valuation run has been published for their tier (a league that
        # signed its rosters before opening the market), so the absence
        # of a market value is a normal state, not a broken one.
        pl = detail.pl
        embed.add_field(
            name="P/L",
            value=format_pl(pl) if pl is not None else "— (needs a published run)",
            inline=True,
        )
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


class _DriverTierFilterSelect(discord.ui.Select):
    """
    Narrow the picker to one tier, or `all` for every tier.

    Mirrors the history browser's tier filter: the filter is state on the
    view, and picking one re-reads the league rather than slicing a
    snapshot.
    """

    def __init__(
        self,
        parent: DriversView,
        summaries: list[workflow.TierDriverSummary],
        *,
        row: int | None = None,
    ) -> None:
        current = parent.tier_filter
        options = [
            discord.SelectOption(
                label="All tiers", value=_ALL_TIERS, default=current is None
            )
        ]
        for s in summaries[: SELECT_MAX_OPTIONS - 1]:
            options.append(
                discord.SelectOption(
                    label=f"{s.code} — {s.label}"[:100],
                    value=s.code,
                    description=f"{s.driver_count} enrolled"[:100],
                    default=s.code == current,
                )
            )
        super().__init__(
            placeholder=(
                f"Filter tier ({'all' if current is None else current})"
            ),
            options=options,
            row=row,
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        await self._owner.reload(
            interaction,
            tier_filter=None if picked == _ALL_TIERS else picked,
            page=0,
        )


class _DriverPageButton(discord.ui.Button):
    """Prev / next page of the driver picker."""

    def __init__(self, *, back: bool, row: int) -> None:
        super().__init__(
            label="Prev drivers" if back else "Next drivers",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if back else "▶",
            row=row,
        )
        self._back = back

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, DriversView)
        step = -1 if self._back else 1
        # Wrap rather than disable at the ends: the page count can change
        # under the panel (an enrolment, a tier move), and a wrap is
        # always in range where a remembered "last page" may not be.
        target = (view.page + step) % view.pages
        await view.reload(interaction, page=target)


class _DriverPickerSelect(discord.ui.Select):
    """
    Pick a driver to act on — one page of the (optionally tier-filtered)
    league.

    Discord caps a select at 25 options. This used to be the whole story:
    the list was truncated to the top 25 with a "showing top 25 of N"
    placeholder, which made every per-driver admin action unreachable in
    a league bigger than that (G16). The list is now filtered and paged,
    and the placeholder names the page rather than a dead end.
    """

    def __init__(
        self,
        parent: DriversView,
        drivers: list[workflow.DriverForPanel],
        *,
        row: int | None = None,
        page: int = 0,
        pages: int = 1,
        total: int | None = None,
    ) -> None:
        shown = drivers[:SELECT_MAX_OPTIONS]
        total = len(shown) if total is None else total
        if total == 0:
            placeholder = "No drivers in this filter"
        elif pages > 1:
            placeholder = (
                f"Pick a driver… (page {page + 1}/{pages} of {total})"
            )
        else:
            placeholder = "Pick a driver…"
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
        self.add_item(_MoveTierButton(label="Promote", direction=_PROMOTE))
        self.add_item(_MoveTierButton(label="Relegate", direction=_RELEGATE))
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


class _VoidNoteModal(base.PanelModal, title="Void contract"):
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
            outcome = await workflow.void_active_contract(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                driver_id=self.parent.detail.driver_id,
                note=note,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        # A void frees the driver, so the team role has to come off —
        # release and buyout already do this. Report it either way: a
        # driver left wearing the role still looks signed to the server.
        from bot import roster_ops
        failure = await roster_ops.drop_from_team_best_effort(
            guild=interaction.guild,
            member_id=outcome.member_id,
            team=outcome.team,
            actor=interaction.user,
            reason=f"Contract {outcome.contract_id} voided",
        )
        where = f" and removed the {outcome.team_name} role" if (
            failure is None and outcome.team_name is not None
        ) else ""
        tail = f"\n⚠ Team role not removed: {failure}" if failure else ""
        await self.parent.refresh(
            interaction,
            note=(
                f"✅ Voided **{outcome.display_name}**'s contract"
                f"{where}.{tail}"
            ),
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


def move_targets(tiers: list, *, current_rank: int, direction: str) -> list:
    """
    The tiers a Promote / Relegate may legally target.

    A promotion moves *up* the ladder, which is a **lower** `rank_order`
    (rank 1 is the top tier); a relegation is the reverse. Offering every
    other tier to both buttons is what let Promote relegate a driver
    (G14).

    Tiers sharing the driver's rank are excluded from both directions.
    `rank_order` is not unique (G25), and a move between two equal-ranked
    tiers is neither a promotion nor a relegation — there is no honest
    direction word for it, so the panel declines to claim one instead of
    guessing or crashing.
    """
    if direction == _PROMOTE:
        # Nearest tier first: the rank just above is the likely intent.
        return sorted(
            (t for t in tiers if t.rank_order < current_rank),
            key=lambda t: (-t.rank_order, t.code),
        )
    return sorted(
        (t for t in tiers if t.rank_order > current_rank),
        key=lambda t: (t.rank_order, t.code),
    )


def no_move_target_note(tiers: list, *, current, direction: str) -> str:
    """Why a direction has nowhere to go, including the equal-rank case."""
    side = "above" if direction == _PROMOTE else "below"
    note = (
        f"No tier ranked {side} `{current.code}` (rank "
        f"{current.rank_order}), so there is nothing to {direction} "
        "this driver into."
    )
    ties = [
        t.code
        for t in tiers
        if t.rank_order == current.rank_order and t.code != current.code
    ]
    if ties:
        codes = ", ".join(f"`{c}`" for c in ties)
        note += (
            f" {codes} share rank {current.rank_order} with it, and a move "
            "between equal-ranked tiers is neither a promotion nor a "
            "relegation — give them distinct ranks in Setup → Tiers first."
        )
    return note


def build_move_confirm_embed(
    detail: workflow.DriverForPanel, *, direction: str, current, target
) -> discord.Embed:
    """
    The confirm step for a tier move, with the direction stated outright.

    Promote and Relegate are the same underlying move, so the screen —
    not the admin's memory of which button they pressed — has to say
    which one is about to happen.
    """
    label, _past, ladder = _DIRECTION_WORDS[direction]
    arrow = "⬆" if direction == _PROMOTE else "⬇"
    return discord.Embed(
        title=f"{arrow} {label} {detail.display_name}?",
        description=(
            f"This is a **{direction}** — {ladder} the ladder.\n\n"
            f"From `{current.code}` — {current.label} (rank "
            f"{current.rank_order})\n"
            f"To `{target.code}` — {target.label} (rank "
            f"{target.rank_order})\n\n"
            "Their active contract, if any, moves with them. Confirm to "
            "apply it."
        ),
        color=COLOR_WARN,
    )


class _MoveTierButton(discord.ui.Button):
    """
    Promote or Relegate — direction enforced here, not left to the pick.

    The tier picker this opens is filtered to tiers strictly above (or
    strictly below) the driver's current rank, so the button an admin
    pressed is the only move they can make.
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
        # Re-read the tiers: ranks are commissioner-editable, so the set
        # of legal targets may have changed since the panel opened.
        tiers = await workflow.list_tiers(interaction.guild_id)
        current = next(
            (t for t in tiers if t.code == view.detail.tier_code), None
        )
        if current is None:
            await report_error(
                interaction,
                f"Tier `{view.detail.tier_code}` is no longer in the active "
                "season, so there is no rank to move from.",
            )
            return
        targets = move_targets(
            tiers, current_rank=current.rank_order, direction=self.direction
        )
        if not targets:
            await report_error(
                interaction,
                no_move_target_note(
                    tiers, current=current, direction=self.direction
                ),
            )
            return
        picker_view = _MoveTierPickerView(
            parent=view,
            direction=self.direction,
            tiers=targets,
            current=current,
        )
        await interaction.response.edit_message(view=picker_view)


class _MoveTierPickerView(AdminOwnedView):
    """One page of legal target tiers for a single direction."""

    def __init__(
        self,
        *,
        parent: _DriverDetailView,
        direction: str,
        tiers: list,
        current,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.direction = direction
        self.tiers = tiers
        self.current = current
        self.pages = page_count(len(tiers))
        self.page = min(max(page, 0), self.pages - 1)
        self.add_item(
            _MoveTierSelect(
                parent,
                direction,
                page_slice(tiers, self.page),
                page=self.page,
                pages=self.pages,
                current=current,
            )
        )
        if self.pages > 1:
            self.add_item(_MoveTierPageButton(back=True, row=1))
            self.add_item(_MoveTierPageButton(back=False, row=1))
        self.add_item(BackButton(self._back, label="Cancel", row=2))

    async def show_page(
        self, interaction: discord.Interaction, page: int
    ) -> None:
        view = _MoveTierPickerView(
            parent=self.parent,
            direction=self.direction,
            tiers=self.tiers,
            current=self.current,
            page=page,
        )
        await interaction.response.edit_message(view=view)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.refresh(interaction)


class _MoveTierPageButton(discord.ui.Button):
    """Prev / next page of target tiers — a league may have over 25."""

    def __init__(self, *, back: bool, row: int) -> None:
        super().__init__(
            label="Prev tiers" if back else "Next tiers",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if back else "▶",
            row=row,
        )
        self._back = back

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _MoveTierPickerView)
        step = -1 if self._back else 1
        await view.show_page(interaction, (view.page + step) % view.pages)


class _MoveTierSelect(discord.ui.Select):
    def __init__(
        self,
        parent: _DriverDetailView,
        direction: str,
        tiers: list,
        *,
        page: int = 0,
        pages: int = 1,
        current=None,
    ) -> None:
        label, _past, ladder = _DIRECTION_WORDS[direction]
        placeholder = f"{label} — move {ladder} to which tier?"
        if pages > 1:
            placeholder = f"{placeholder} (page {page + 1}/{pages})"
        super().__init__(
            placeholder=placeholder[:150],
            options=[
                discord.SelectOption(
                    label=f"{t.code} — {t.label}"[:100],
                    value=t.code,
                    description=f"rank {t.rank_order}"[:100],
                )
                for t in tiers[:SELECT_MAX_OPTIONS]
            ],
            disabled=not tiers,
        )
        self._owner = parent
        self._direction = direction
        self._current = current
        self._by_code = {t.code: t for t in tiers}

    async def callback(self, interaction: discord.Interaction) -> None:
        target = self._by_code[self.values[0]]
        confirm = _MoveTierConfirmView(
            parent=self._owner,
            direction=self._direction,
            current=self._current,
            target=target,
        )
        await interaction.response.edit_message(
            embed=build_move_confirm_embed(
                self._owner.detail,
                direction=self._direction,
                current=self._current,
                target=target,
            ),
            view=confirm,
        )


class _MoveTierConfirmView(AdminOwnedView):
    """Explicit confirm step, so a mis-picked tier is not a fait accompli."""

    def __init__(
        self, *, parent: _DriverDetailView, direction: str, current, target
    ) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent
        self.direction = direction
        self.current = current
        self.target = target
        label, _past, _ladder = _DIRECTION_WORDS[direction]
        self.add_item(
            _MoveTierConfirmButton(
                label=f"Confirm {label.lower()} to {target.code}"
            )
        )
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.refresh(interaction)


class _MoveTierConfirmButton(discord.ui.Button):
    def __init__(self, *, label: str) -> None:
        super().__init__(
            label=label[:80], style=discord.ButtonStyle.danger, row=0
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        view = self.view
        assert isinstance(view, _MoveTierConfirmView)
        detail = view.parent.detail
        _label, past, ladder = _DIRECTION_WORDS[view.direction]
        try:
            await workflow.move_driver_to_tier(
                guild_id=interaction.guild_id,
                actor_id=interaction.user.id,
                driver_id=detail.driver_id,
                new_tier_code=view.target.code,
                note=f"{view.direction} via panel",
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await view.parent.refresh(
            interaction,
            note=(
                f"✅ **{past}** {detail.display_name} — moved {ladder} from "
                f"`{view.current.code}` (rank {view.current.rank_order}) to "
                f"`{view.target.code}` (rank {view.target.rank_order}). "
                "Their active contract (if any) moved with them."
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
        embed=build_drivers_embed(
            summaries,
            picker_total=len(view.filtered) if drivers else None,
            tier_filter=view.tier_filter,
            page=view.page,
            pages=view.pages,
        ),
        view=view,
    )
