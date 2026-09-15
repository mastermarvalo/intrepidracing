"""
League setup as an interactive screen.

Standing up a league by command means running `season create`, then
`season activate`, then `tier add` once per tier, then `config edit`,
then `config role`, then `config channel` three times, then `board add`
per board — nine-plus invocations with exact argument names, in an order
nothing enforces.

This screen presents the same operations as a checklist that shows what
is already done, and uses Discord's native role and channel pickers
instead of asking an admin to paste ids. It adds no capability the
commands lack; it routes to the identical `bot.workflow` functions.
"""

from __future__ import annotations

import discord

from bot import workflow
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
from bot.ui.boards_screen import open_boards
from bot.ui.config_modal import ConfigSectionView, build_config_embed

_PRESET_F1 = "f1"
_PRESET_NONE = "none"

# Rank order is a plain sort key, so the first tier an admin adds is
# simply first. Offered as the default so the field is never blank.
_DEFAULT_RANK_ORDER = "1"


def build_setup_embed(status: workflow.LeagueStatus) -> discord.Embed:
    """A checklist, not a status dump — each line is an action or a tick."""
    done = "✅"
    todo = "⬜"

    lines = [
        f"{done if status.has_season else todo} **Season** — "
        + (
            f"**{status.season_name}** is active"
            if status.has_season
            else "none yet, start here"
        ),
        f"{done if status.has_tiers else todo} **Tiers** — "
        + (
            f"{len(status.tiers)} configured "
            f"({', '.join(t.code for t in status.tiers)})"
            if status.has_tiers
            else "none yet"
        ),
        f"{done if status.has_drivers else todo} **Drivers** — "
        + (
            f"{sum(t.driver_count for t in status.tiers)} across all tiers"
            if status.has_drivers
            else "none yet, add them with `/roster add`"
        ),
    ]

    embed = discord.Embed(
        title="⚙️ League setup",
        description="\n".join(lines),
        color=COLOR_OK if status.setup_complete else COLOR_WARN,
    )

    if status.has_tiers:
        embed.add_field(
            name="Tiers",
            value=truncate_field(
                "\n".join(
                    f"`{t.code}` **{t.label}** · {t.driver_count} driver(s)"
                    + (" · 🏷 role linked" if t.has_role else "")
                    for t in status.tiers
                )
            ),
            inline=False,
        )

    if not status.has_season:
        embed.add_field(
            name="Next step",
            value=(
                "Press **Season** and give it a name. Choosing the F1 preset "
                "creates three tiers, the scoring table, the valuation "
                "factors and the default $145.00M cap in one go."
            ),
            inline=False,
        )
    elif not status.has_tiers:
        embed.add_field(
            name="Next step",
            value="Press **Tier** to add your first tier.",
            inline=False,
        )
    elif not status.has_drivers:
        embed.add_field(
            name="Next step",
            value=(
                "Add drivers with `/roster add`, then come back and press "
                "**Boards** to pin the market tables."
            ),
            inline=False,
        )

    embed.set_footer(
        text="Equivalent commands: /market-admin season · tier · config · board"
    )
    return embed


# ── season ───────────────────────────────────────────────────────────


class _SeasonModal(discord.ui.Modal, title="Create a season"):
    """
    Creates and activates in one step.

    The commands keep create and activate separate, because a commissioner
    may want to build next season while this one is still running. From
    the setup checklist the intent is unambiguous — you are setting up the
    league you are about to run — so this path does both and says so.
    """

    def __init__(self, parent: SetupView) -> None:
        super().__init__()
        self._parent = parent
        self._name = discord.ui.TextInput(
            label="Season name",
            placeholder="Season 7",
            max_length=100,
        )
        self._preset = discord.ui.TextInput(
            label="Preset — type f1, or leave blank for empty",
            required=False,
            placeholder="f1",
            max_length=20,
        )
        self.add_item(self._name)
        self.add_item(self._preset)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self._preset.value.strip().lower()
        if raw in ("", _PRESET_NONE):
            preset = None
        elif raw == _PRESET_F1:
            preset = _PRESET_F1
        else:
            await report_error(
                interaction,
                f"`{raw}` is not a known preset. Type `f1` or leave it blank.",
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            created = await workflow.create_and_activate_season(
                guild_id=interaction.guild_id,
                name=self._name.value,
                preset=preset,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        note = f"✅ **{created.name}** created and activated."
        if created.preset_seeded:
            note += (
                "\nSeeded the F1 preset: tiers `t1`/`t2`/`t3`, scoring table, "
                "valuation factors, and the default $145.00M salary cap."
            )
        await self._parent.reload(interaction, note=note)


# ── tiers ────────────────────────────────────────────────────────────


class _TierModal(discord.ui.Modal, title="Add a tier"):
    def __init__(self, parent: SetupView, *, suggested_rank: int) -> None:
        super().__init__()
        self._parent = parent
        self._code = discord.ui.TextInput(
            label="Code — short, used in commands",
            placeholder="t1",
            max_length=16,
        )
        self._label = discord.ui.TextInput(
            label="Display name",
            placeholder="Tier 1",
            max_length=100,
        )
        self._rank = discord.ui.TextInput(
            label="Rank order — 1 is the top tier",
            default=str(suggested_rank) if suggested_rank else _DEFAULT_RANK_ORDER,
            max_length=3,
        )
        self._color = discord.ui.TextInput(
            label="Accent colour (optional)",
            required=False,
            placeholder="#e10600",
            max_length=7,
        )
        for widget in (self._code, self._label, self._rank, self._color):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            rank = int(self._rank.value.strip())
        except ValueError:
            await report_error(
                interaction, "Rank order must be a whole number, e.g. `1`."
            )
            return

        accent: int | None = None
        raw_color = self._color.value.strip()
        if raw_color:
            try:
                accent = discord.Color.from_str(raw_color).value
            except ValueError:
                await report_error(
                    interaction,
                    f"Could not read `{raw_color}` as a colour. Use hex, e.g. "
                    f"`#e10600`.",
                )
                return

        await interaction.response.defer(ephemeral=True)
        code = self._code.value.strip().lower()
        try:
            await workflow.add_tier(
                guild_id=interaction.guild_id,
                code=code,
                label=self._label.value,
                rank_order=rank,
                accent_color=accent,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        await self._parent.reload(
            interaction,
            note=(
                f"✅ Tier `{code}` added. To link a Discord role to it, press "
                f"**Tier role**."
            ),
        )


class _TierRoleFlow(AdminOwnedView):
    """Pick a tier, then pick the role that identifies its drivers."""

    def __init__(self, *, opener_id: int, parent: SetupView) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_code: str | None = None
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def start(self, interaction: discord.Interaction) -> None:
        tiers = await workflow.list_tier_choices(interaction.guild_id)
        if not tiers:
            await report_error(interaction, "Add a tier first.")
            return
        self.clear_items()
        self.add_item(_TierPickSelect(self, tiers))
        self.add_item(BackButton(self._back, label="Cancel", row=1))
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🏷 Link a role to a tier",
                description="Step 1 of 2 — which tier?",
                color=COLOR_INFO,
            ),
            view=self,
        )

    async def ask_for_role(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        self.add_item(_TierRoleSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🏷 Link a role to a tier",
                description=(
                    f"Tier `{self.tier_code}` — step 2 of 2, pick the role."
                ),
                color=COLOR_INFO,
            ),
            view=self,
        )


class _TierPickSelect(discord.ui.Select):
    def __init__(self, flow: _TierRoleFlow, tiers: list[tuple[str, str]]) -> None:
        super().__init__(
            placeholder="Which tier?",
            options=[
                discord.SelectOption(label=label, value=code)
                for code, label in tiers[:SELECT_MAX_OPTIONS]
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.tier_code = self.values[0]
        await self._flow.ask_for_role(interaction)


class _TierRoleSelect(discord.ui.RoleSelect):
    def __init__(self, flow: _TierRoleFlow) -> None:
        super().__init__(placeholder="Which role?", max_values=1)
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        role = self.values[0]
        assert self._flow.tier_code is not None
        try:
            await workflow.set_tier_role(
                guild_id=interaction.guild_id,
                tier_code=self._flow.tier_code,
                role_id=role.id,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._flow.parent.reload(
            interaction,
            note=f"✅ Tier `{self._flow.tier_code}` linked to {role.mention}.",
        )


# ── commissioner role ────────────────────────────────────────────────


class _CommissionerRoleFlow(AdminOwnedView):
    """
    Sets the season-default commissioner role.

    Per-tier overrides stay on `/market-admin config role tier:<code>`;
    a single league-wide commissioner role is what almost every server
    wants, and offering the override here would make the common case
    two clicks longer.
    """

    def __init__(self, *, opener_id: int, parent: SetupView) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.add_item(_CommissionerRoleSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _CommissionerRoleSelect(discord.ui.RoleSelect):
    def __init__(self, flow: _CommissionerRoleFlow) -> None:
        super().__init__(placeholder="Which role approves contracts?", max_values=1)
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        role = self.values[0]
        try:
            await workflow.set_commissioner_role(
                guild_id=interaction.guild_id, role_id=role.id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._flow.parent.reload(
            interaction, note=f"✅ Commissioner role set to {role.mention}."
        )


# ── channels ─────────────────────────────────────────────────────────


class _ChannelFlow(AdminOwnedView):
    """Pick which notification channel to set, then pick the channel."""

    def __init__(self, *, opener_id: int, parent: SetupView) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.kind: str | None = None
        self.add_item(_ChannelKindSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)

    async def ask_for_channel(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        self.add_item(_TargetChannelSelect(self))
        self.add_item(BackButton(self._back, label="Cancel", row=1))
        assert self.kind is not None
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📣 Set a channel",
                description=(
                    f"**{workflow.CHANNEL_KIND_LABELS[self.kind]}** — "
                    f"pick the channel."
                ),
                color=COLOR_INFO,
            ),
            view=self,
        )


_CHANNEL_KIND_HELP = {
    "market": "Where published valuations are posted.",
    "transactions": "Public log of signings, drops and trades.",
    "approvals": "Where pending offers ping the commissioners.",
}


class _ChannelKindSelect(discord.ui.Select):
    def __init__(self, flow: _ChannelFlow) -> None:
        super().__init__(
            placeholder="Which channel do you want to set?",
            options=[
                discord.SelectOption(
                    label=workflow.CHANNEL_KIND_LABELS[kind],
                    value=kind,
                    description=_CHANNEL_KIND_HELP[kind],
                )
                for kind in workflow.CHANNEL_KINDS
            ],
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        self._flow.kind = self.values[0]
        await self._flow.ask_for_channel(interaction)


class _TargetChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, flow: _ChannelFlow) -> None:
        super().__init__(
            placeholder="Which channel?",
            channel_types=[discord.ChannelType.text],
            max_values=1,
        )
        self._flow = flow

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        assert self._flow.kind is not None
        channel = self.values[0]
        try:
            await workflow.set_channel(
                guild_id=interaction.guild_id,
                kind=self._flow.kind,
                channel_id=channel.id,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        label = workflow.CHANNEL_KIND_LABELS[self._flow.kind]
        await self._flow.parent.reload(
            interaction, note=f"✅ {label} channel set to <#{channel.id}>."
        )


# ── the screen ───────────────────────────────────────────────────────


class SetupView(AdminOwnedView):
    """
    The setup checklist.

    Buttons are enabled based on what exists: adding a tier before there
    is a season is not a thing an admin should be able to try and be told
    off for.
    """

    def __init__(
        self,
        *,
        status: workflow.LeagueStatus,
        opener_id: int,
        on_back: BackCallback,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.status = status

        has_season = status.has_season
        self.add_item(_SeasonButton(row=0))
        self.add_item(_TierButton(row=0, enabled=has_season))
        self.add_item(_ConfigButton(row=0, enabled=has_season))
        self.add_item(_TierRoleButton(row=1, enabled=status.has_tiers))
        self.add_item(_CommissionerButton(row=1, enabled=has_season))
        self.add_item(_ChannelsButton(row=1, enabled=has_season))
        self.add_item(_BoardsButton(row=2, enabled=status.has_tiers))
        self.add_item(_FreeAgencyButton(row=2, enabled=has_season))
        self.add_item(BackButton(on_back, label="Back to home", row=2))

    async def reload(
        self, interaction: discord.Interaction, *, note: str | None = None
    ) -> None:
        status = await workflow.fetch_league_status(interaction.guild_id)
        view = SetupView(
            status=status, opener_id=self.opener_id, on_back=self._on_back
        )
        embed = build_setup_embed(status)

        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        if note:
            await interaction.followup.send(note, ephemeral=True)

    async def open_boards_screen(self, interaction: discord.Interaction) -> None:
        await open_boards(
            interaction, opener_id=self.opener_id, on_back=self._back_to_setup
        )

    async def _back_to_setup(self, interaction: discord.Interaction) -> None:
        await self.reload(interaction)

    async def open_season_menu(self, interaction: discord.Interaction) -> None:
        menu = _SeasonMenuView(parent=self)
        await menu.render(interaction)

    async def open_tier_menu(self, interaction: discord.Interaction) -> None:
        menu = _TierMenuView(parent=self)
        await menu.render(interaction)

    async def open_free_agency(self, interaction: discord.Interaction) -> None:
        view = _FreeAgencyView(parent=self)
        await view.render(interaction)


def _style(enabled: bool) -> discord.ButtonStyle:
    return discord.ButtonStyle.primary if enabled else discord.ButtonStyle.secondary


class _SeasonButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Season", style=discord.ButtonStyle.primary, emoji="📅", row=row
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        await view.open_season_menu(interaction)


class _TierButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Tier", style=_style(enabled), emoji="🧱", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        await view.open_tier_menu(interaction)


class _ConfigButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Cap & rules", style=_style(enabled), emoji="💰", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            season_id, tier_id, cfg = await workflow.fetch_config_for_edit(
                guild_id=interaction.guild_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        # Two modals, not one: ten numeric tunables do not fit in
        # Discord's five-input modal, so the button opens a chooser.
        view = self.view
        assert isinstance(view, SetupView)
        await interaction.response.edit_message(
            embed=build_config_embed(cfg, scope_label="the season default"),
            view=ConfigSectionView(
                season_id=season_id,
                tier_id=tier_id,
                current=cfg,
                opener_id=view.opener_id,
                on_back=view.reload,
            ),
        )


class _TierRoleButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Tier role", style=_style(enabled), emoji="🏷", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        flow = _TierRoleFlow(opener_id=view.opener_id, parent=view)
        await flow.start(interaction)


class _CommissionerButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Commissioner role", style=_style(enabled), emoji="🧑‍⚖️", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        flow = _CommissionerRoleFlow(opener_id=view.opener_id, parent=view)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="🧑‍⚖️ Commissioner role",
                description=(
                    "Members with this role can approve contracts and trades.\n"
                    "For a per-tier override use "
                    "`/market-admin config role tier:<code>`."
                ),
                color=COLOR_INFO,
            ),
            view=flow,
        )


class _ChannelsButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Channels", style=_style(enabled), emoji="📣", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        flow = _ChannelFlow(opener_id=view.opener_id, parent=view)
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="📣 Set a channel",
                description="Which channel do you want to set?",
                color=COLOR_INFO,
            ),
            view=flow,
        )


class _BoardsButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Boards", style=_style(enabled), emoji="📊", row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        await view.open_boards_screen(interaction)


async def open_setup(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    """Entry point used by the panel's Setup button."""
    status = await workflow.fetch_league_status(interaction.guild_id)
    view = SetupView(status=status, opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_setup_embed(status), view=view
    )


# ── seasons menu (list + activate + create-new) ──────────────────────


class _SeasonMenuView(AdminOwnedView):
    """
    List every season, offer per-season Activate + a Create-new button.

    Wraps `/market-admin season list` + `season activate` + `season create`
    behind one entry point so an admin isn't juggling three commands
    just to switch which season is active.
    """

    def __init__(self, *, parent: SetupView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent

    async def render(self, interaction: discord.Interaction) -> None:
        seasons = await workflow.list_seasons(interaction.guild_id)
        self.clear_items()
        if seasons:
            self.add_item(_ActivateSeasonSelect(self, seasons))
        self.add_item(_CreateSeasonButton(row=1))
        self.add_item(BackButton(self._back, row=1))
        embed = _build_seasons_embed(seasons)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


def _build_seasons_embed(seasons) -> discord.Embed:
    if not seasons:
        return discord.Embed(
            title="📅 Seasons",
            description=(
                "No seasons yet.\n\n"
                "Press **Create season** to add one — you can seed the F1 "
                "preset from the same modal."
            ),
            color=COLOR_INFO,
        )
    lines = []
    for s in seasons:
        marker = "🟢 active" if s.is_active else "⚪ inactive"
        lines.append(f"**{s.name}** · {marker}")
    return discord.Embed(
        title="📅 Seasons",
        description=truncate_field("\n".join(lines)),
        color=COLOR_INFO,
    )


class _ActivateSeasonSelect(discord.ui.Select):
    def __init__(self, menu: _SeasonMenuView, seasons) -> None:
        super().__init__(
            placeholder="Activate a season…",
            options=[
                discord.SelectOption(
                    label=s.name[:100],
                    value=s.name[:100],
                    description=("Currently active" if s.is_active else "Inactive"),
                    default=s.is_active,
                )
                for s in seasons[:SELECT_MAX_OPTIONS]
            ],
        )
        self._menu = menu

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        name = self.values[0]
        try:
            await workflow.activate_season(guild_id=interaction.guild_id, name=name)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._menu.render(interaction)
        await interaction.followup.send(
            f"✅ **{name}** is now the active season.", ephemeral=True
        )


class _CreateSeasonButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Create season",
            style=discord.ButtonStyle.success,
            emoji="➕",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _SeasonMenuView)
        await interaction.response.send_modal(_SeasonModal(view.parent))


# ── tiers menu (list + edit-per-tier + add-new) ──────────────────────


class _TierMenuView(AdminOwnedView):
    """List existing tiers with per-tier Edit, plus an Add-new button."""

    def __init__(self, *, parent: SetupView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent

    async def render(self, interaction: discord.Interaction) -> None:
        tiers = await workflow.list_tiers(interaction.guild_id)
        self.clear_items()
        if tiers:
            self.add_item(_EditTierSelect(self, tiers))
        self.add_item(_AddTierButton(row=1))
        self.add_item(BackButton(self._back, row=1))
        embed = _build_tiers_embed(tiers)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


def _build_tiers_embed(tiers) -> discord.Embed:
    if not tiers:
        return discord.Embed(
            title="🧱 Tiers",
            description=(
                "No tiers yet.\n\n"
                "Press **Add tier** to create one. You'll be prompted for a "
                "short code, a display label, a rank, and an optional colour."
            ),
            color=COLOR_INFO,
        )
    lines = []
    for t in tiers:
        role = "🏷 role linked" if t.tier_role_id else "no role"
        lines.append(
            f"`{t.code}` **{t.label}** · rank {t.rank_order} · {role}"
        )
    return discord.Embed(
        title="🧱 Tiers",
        description=truncate_field("\n".join(lines)),
        color=COLOR_INFO,
    )


class _EditTierSelect(discord.ui.Select):
    def __init__(self, menu: _TierMenuView, tiers) -> None:
        super().__init__(
            placeholder="Edit a tier…",
            options=[
                discord.SelectOption(
                    label=f"{t.code} — {t.label}"[:100],
                    value=t.code,
                )
                for t in tiers[:SELECT_MAX_OPTIONS]
            ],
        )
        self._menu = menu
        self._by_code = {t.code: t for t in tiers}

    async def callback(self, interaction: discord.Interaction) -> None:
        tier = self._by_code[self.values[0]]
        await interaction.response.send_modal(_TierEditModal(self._menu, tier))


class _AddTierButton(discord.ui.Button):
    def __init__(self, *, row: int) -> None:
        super().__init__(
            label="Add tier",
            style=discord.ButtonStyle.success,
            emoji="➕",
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _TierMenuView)
        # Suggest the next rank so consecutive adds don't collide.
        suggested = len(view.parent.status.tiers) + 1
        await interaction.response.send_modal(
            _TierModal(view.parent, suggested_rank=suggested)
        )


class _TierEditModal(discord.ui.Modal, title="Edit tier"):
    """
    Rewrite a tier's label, rank, or accent colour.

    Role is kept intact — that's what **Tier role** is for. Code is
    immutable (it's the join key everything else references), so it
    isn't editable here either.
    """

    def __init__(self, menu: _TierMenuView, tier) -> None:
        super().__init__()
        self._menu = menu
        self._tier = tier
        self._label = discord.ui.TextInput(
            label="Display name",
            default=tier.label,
            max_length=100,
        )
        self._rank = discord.ui.TextInput(
            label="Rank order — 1 is the top tier",
            default=str(tier.rank_order),
            max_length=3,
        )
        self._color = discord.ui.TextInput(
            label="Accent colour (blank to clear)",
            required=False,
            default=(
                f"#{tier.accent_color:06x}"
                if tier.accent_color is not None
                else ""
            ),
            max_length=7,
        )
        for widget in (self._label, self._rank, self._color):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            rank = int(self._rank.value.strip())
        except ValueError:
            await report_error(
                interaction, "Rank order must be a whole number, e.g. `1`."
            )
            return
        raw_color = self._color.value.strip()
        accent: int | None = None
        if raw_color:
            try:
                accent = discord.Color.from_str(raw_color).value
            except ValueError:
                await report_error(
                    interaction,
                    f"Could not read `{raw_color}` as a colour.",
                )
                return

        await interaction.response.defer(ephemeral=True)
        try:
            await workflow.edit_tier(
                guild_id=interaction.guild_id,
                tier_code=self._tier.code,
                label=self._label.value,
                rank_order=rank,
                accent_color=accent,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._menu.render(interaction)
        await interaction.followup.send(
            f"✅ Tier `{self._tier.code}` updated.", ephemeral=True
        )


# ── free agency toggle ───────────────────────────────────────────────


class _FreeAgencyButton(discord.ui.Button):
    def __init__(self, *, row: int, enabled: bool) -> None:
        super().__init__(
            label="Free agency",
            style=_style(enabled),
            emoji="🕊",
            row=row,
            disabled=not enabled,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, SetupView)
        await view.open_free_agency(interaction)


class _FreeAgencyView(AdminOwnedView):
    """
    Show the current free-agency window and offer a one-click toggle.

    Season default only; per-tier overrides remain a slash command
    (`/market-admin config free-agency tier:<code>`) — the panel keeps
    the common case one click away.
    """

    def __init__(self, *, parent: SetupView) -> None:
        super().__init__(opener_id=parent.opener_id)
        self.parent = parent

    async def render(self, interaction: discord.Interaction) -> None:
        try:
            cfg = await workflow.show_config(interaction.guild_id)
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        self.clear_items()
        self.add_item(_FreeAgencyToggle(self, is_open=cfg.free_agency_open))
        self.add_item(BackButton(self._back))
        state = "🟢 OPEN" if cfg.free_agency_open else "⚪ CLOSED"
        embed = discord.Embed(
            title="🕊 Free agency window",
            description=(
                f"Current state: **{state}** (season default).\n\n"
                "While closed, only extension offers to a driver's own team "
                "are accepted. Open it during the offseason to let teams bid "
                "on drivers whose contracts have ended."
            ),
            color=COLOR_OK if cfg.free_agency_open else COLOR_INFO,
        )
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _FreeAgencyToggle(discord.ui.Button):
    def __init__(self, view: _FreeAgencyView, *, is_open: bool) -> None:
        super().__init__(
            label="Close" if is_open else "Open",
            style=(
                discord.ButtonStyle.danger if is_open
                else discord.ButtonStyle.success
            ),
        )
        self._parent = view
        self._current = is_open

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            await workflow.set_free_agency(
                guild_id=interaction.guild_id, is_open=not self._current
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        await self._parent.render(interaction)
        await interaction.followup.send(
            "✅ Free agency " + ("opened." if not self._current else "closed."),
            ephemeral=True,
        )
