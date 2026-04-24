"""
Guided create/edit flow for /roster create and /roster edit.

Each step is a discord.ui.View or Modal that mutates a FlowState and sends
the next step as an edited ephemeral message.  Steps that need text input use
Modals; steps that need role/channel pickers use component Views.

Session state lives in the module-level _sessions dict, keyed by
(guild_id, user_id), and is discarded when the flow completes or times out.

UX note: the "Add slot" sub-flow (modal → role select) sends a new ephemeral
message for the role select, because Discord modals can only respond with a
fresh message — there is no way to edit an existing ephemeral message from a
modal submit.  The old builder message becomes stale; we stop its view so its
buttons fail gracefully rather than silently acting on outdated state.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import discord
from discord.ext import commands

from bot import db, queries
from bot.models import Team, TeamSlot
from bot.render import build_embed

log = logging.getLogger(__name__)

# ── session state ─────────────────────────────────────────────────────────────

_sessions: dict[tuple[int, int], "FlowState"] = {}


@dataclass
class SlotDraft:
    label: str
    quantity: int
    slot_role_id: int
    slot_type: str  # 'staff' | 'driver'


@dataclass
class FlowState:
    guild_id: int
    user_id: int
    team_key: str
    bot: commands.Bot

    existing_team: Optional[Team] = None

    # Step 1
    display_name: str = ""
    tagline: str = ""
    logo_url: str = ""

    # Step 2
    team_role_id: int = 0

    # Steps 3 & 4
    staff_slots: list[SlotDraft] = field(default_factory=list)
    driver_slots: list[SlotDraft] = field(default_factory=list)

    # Step 5
    channel_id: int = 0

    # Pending slot — set during modal submit, consumed by role select
    pending_slot_label: str = ""
    pending_slot_qty: int = 1
    pending_slot_type: str = ""
    pending_step_num: int = 0

    # The most recently active builder view; stopped when a new one takes over
    active_builder_view: Optional[discord.ui.View] = field(default=None, repr=False)

    def session_key(self) -> tuple[int, int]:
        return (self.guild_id, self.user_id)


def _put(state: FlowState) -> None:
    _sessions[state.session_key()] = state


def _discard(state: FlowState) -> None:
    _sessions.pop(state.session_key(), None)


# ── entry points ──────────────────────────────────────────────────────────────


async def start_create(
    interaction: discord.Interaction, team_key: str, bot: commands.Bot
) -> None:
    assert interaction.guild_id is not None

    async with db.connect() as conn:
        existing = await queries.fetch_team(conn, interaction.guild_id, team_key)
    if existing is not None:
        await interaction.response.send_message(
            f"A team with key `{team_key}` already exists. "
            f"Use `/roster edit {team_key}` to modify it.",
            ephemeral=True,
        )
        return

    state = FlowState(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        team_key=team_key,
        bot=bot,
    )
    _put(state)
    await interaction.response.send_modal(_Step1Modal(state))


async def start_edit(
    interaction: discord.Interaction, team_key: str, bot: commands.Bot
) -> None:
    assert interaction.guild_id is not None

    async with db.connect() as conn:
        existing = await queries.fetch_team(conn, interaction.guild_id, team_key)
    if existing is None:
        await interaction.response.send_message(
            f"No team named `{team_key}`. Use `/roster create {team_key}` to create it.",
            ephemeral=True,
        )
        return

    state = FlowState(
        guild_id=interaction.guild_id,
        user_id=interaction.user.id,
        team_key=team_key,
        bot=bot,
        existing_team=existing,
        display_name=existing.name,
        tagline=existing.tagline or "",
        logo_url=existing.logo_url or "",
        team_role_id=existing.team_role_id,
        channel_id=existing.channel_id,
    )
    for s in existing.slots:
        draft = SlotDraft(
            label=s.label,
            quantity=s.quantity,
            slot_role_id=s.slot_role_id,
            slot_type=s.slot_type,
        )
        if s.slot_type == "staff":
            state.staff_slots.append(draft)
        else:
            state.driver_slots.append(draft)

    _put(state)
    await interaction.response.send_modal(_Step1Modal(state))


# ── step 1: text fields ───────────────────────────────────────────────────────


class _Step1Modal(discord.ui.Modal, title="Team Setup (1/6)"):
    team_name = discord.ui.TextInput(
        label="Display name",
        placeholder="Red Bull Racing",
        max_length=64,
    )
    tagline = discord.ui.TextInput(
        label="Tagline (optional)",
        placeholder="6x WCC | 3x WDC | 1x ICC",
        required=False,
        max_length=120,
    )
    logo_url = discord.ui.TextInput(
        label="Logo URL (optional)",
        placeholder="https://example.com/logo.png",
        required=False,
        max_length=256,
    )

    def __init__(self, state: FlowState) -> None:
        super().__init__()
        self._state = state
        if state.display_name:
            self.team_name.default = state.display_name
        if state.tagline:
            self.tagline.default = state.tagline
        if state.logo_url:
            self.logo_url.default = state.logo_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._state.display_name = self.team_name.value.strip()
        self._state.tagline = self.tagline.value.strip()
        self._state.logo_url = self.logo_url.value.strip()
        await _send_step2(interaction, self._state)


# ── step 2: team role ─────────────────────────────────────────────────────────


async def _send_step2(interaction: discord.Interaction, state: FlowState) -> None:
    view = _Step2View(state)
    await interaction.response.send_message(
        "**Step 2 of 6 — Team role**\n"
        "Pick the Discord role that marks someone as being on this team.",
        view=view,
        ephemeral=True,
    )


class _Step2View(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        sel = discord.ui.RoleSelect(placeholder="Select team role…", min_values=1, max_values=1)
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        self._state.team_role_id = sel.values[0].id
        self.stop()
        await _send_builder(interaction, self._state, slot_type="staff", step_num=3)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── steps 3 & 4: slot builders ────────────────────────────────────────────────


def _slot_summary(slots: list[SlotDraft]) -> str:
    if not slots:
        return "*(none yet)*"
    return "\n".join(f"• **{s.label}** × {s.quantity} — <@&{s.slot_role_id}>" for s in slots)


async def _send_builder(
    interaction: discord.Interaction,
    state: FlowState,
    slot_type: str,
    step_num: int,
) -> None:
    """Send or refresh the slot builder. Stops the previous active builder view."""
    if state.active_builder_view is not None:
        state.active_builder_view.stop()

    slots = state.staff_slots if slot_type == "staff" else state.driver_slots
    label = slot_type.capitalize()
    content = (
        f"**Step {step_num} of 6 — {label} slots**\n"
        f"Add as many {label.lower()} slots as you need, then click **Done →**.\n\n"
        f"**Current {label.lower()} slots:**\n{_slot_summary(slots)}"
    )
    view = _BuilderView(state=state, slot_type=slot_type, step_num=step_num)
    state.active_builder_view = view

    # After a modal submit the only valid response is send_message.
    # After a select (component) we can edit the existing message.
    if interaction.response.is_done():
        await interaction.edit_original_response(content=content, view=view)
    else:
        await interaction.response.edit_message(content=content, view=view)


class _BuilderView(discord.ui.View):
    def __init__(self, *, state: FlowState, slot_type: str, step_num: int) -> None:
        super().__init__(timeout=600)
        self._state = state
        self._slot_type = slot_type
        self._step_num = step_num

    @discord.ui.button(label="Add slot", style=discord.ButtonStyle.primary)
    async def add_slot(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.send_modal(
            _SlotTextModal(state=self._state, slot_type=self._slot_type, step_num=self._step_num)
        )

    @discord.ui.button(label="Done →", style=discord.ButtonStyle.success)
    async def done(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.stop()
        if self._step_num == 3:
            await _send_builder(interaction, self._state, slot_type="driver", step_num=4)
        else:
            await _send_step5(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


class _SlotTextModal(discord.ui.Modal):
    label_input = discord.ui.TextInput(
        label="Slot label", placeholder="Tier 1 Drivers", max_length=64
    )
    qty_input = discord.ui.TextInput(label="Seats (quantity)", placeholder="2", max_length=2)

    def __init__(self, *, state: FlowState, slot_type: str, step_num: int) -> None:
        super().__init__(title=f"Add {slot_type.capitalize()} Slot")
        self._state = state
        self._slot_type = slot_type
        self._step_num = step_num

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.qty_input.value.strip()
        if not raw.isdigit() or int(raw) < 1:
            await interaction.response.send_message(
                "Quantity must be a positive whole number.", ephemeral=True
            )
            return

        self._state.pending_slot_label = self.label_input.value.strip()
        self._state.pending_slot_qty = int(raw)
        self._state.pending_slot_type = self._slot_type
        self._state.pending_step_num = self._step_num

        view = _SlotRoleView(state=self._state)
        # Modal submit must respond with a fresh message; we can't edit the builder here.
        await interaction.response.send_message(
            f"**Pick the role for \"{self._state.pending_slot_label}\":**",
            view=view,
            ephemeral=True,
        )


class _SlotRoleView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=120)
        self._state = state
        sel = discord.ui.RoleSelect(
            placeholder="Select role for this slot…", min_values=1, max_values=1
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        draft = SlotDraft(
            label=self._state.pending_slot_label,
            quantity=self._state.pending_slot_qty,
            slot_role_id=sel.values[0].id,
            slot_type=self._state.pending_slot_type,
        )
        if draft.slot_type == "staff":
            self._state.staff_slots.append(draft)
        else:
            self._state.driver_slots.append(draft)

        self.stop()
        # edit_message updates the role-select message to become the new builder
        await _send_builder(
            interaction,
            self._state,
            slot_type=draft.slot_type,
            step_num=self._state.pending_step_num,
        )

    async def on_timeout(self) -> None:
        pass  # pending slot is discarded; user can try again


# ── step 5: channel ───────────────────────────────────────────────────────────


async def _send_step5(interaction: discord.Interaction, state: FlowState) -> None:
    view = _Step5View(state)
    if interaction.response.is_done():
        await interaction.edit_original_response(
            content=(
                "**Step 5 of 6 — Channel**\n"
                "Pick the text channel where the roster will be posted."
            ),
            view=view,
        )
    else:
        await interaction.response.edit_message(
            content=(
                "**Step 5 of 6 — Channel**\n"
                "Pick the text channel where the roster will be posted."
            ),
            view=view,
        )


class _Step5View(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        sel = discord.ui.ChannelSelect(
            placeholder="Select a text channel…",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.ChannelSelect = self.children[0]  # type: ignore[assignment]
        self._state.channel_id = sel.values[0].id
        self.stop()
        await _send_step6(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── step 6: confirm & post ────────────────────────────────────────────────────


def _state_to_preview_team(state: FlowState) -> Team:
    slots = [
        TeamSlot(
            id=i,
            team_id=0,
            slot_role_id=s.slot_role_id,
            label=s.label,
            quantity=s.quantity,
            slot_type=s.slot_type,  # type: ignore[arg-type]
            sort_order=i,
        )
        for i, s in enumerate(state.staff_slots + state.driver_slots)
    ]
    return Team(
        id=0,
        guild_id=state.guild_id,
        key=state.team_key,
        name=state.display_name,
        team_role_id=state.team_role_id,
        channel_id=state.channel_id,
        tagline=state.tagline or None,
        logo_url=state.logo_url or None,
        slots=slots,
    )


async def _send_step6(interaction: discord.Interaction, state: FlowState) -> None:
    assert interaction.guild is not None
    preview = _state_to_preview_team(state)
    embed = build_embed(preview, list(interaction.guild.members))
    view = _Step6View(state)

    content = (
        "**Step 6 of 6 — Confirm**\n"
        "Preview below. Member mentions are live — the real roster will look the same.\n"
        f"Posting to <#{state.channel_id}>."
    )
    await interaction.response.edit_message(content=content, embed=embed, view=view)


class _Step6View(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state

    @discord.ui.button(label="Post roster", style=discord.ButtonStyle.success)
    async def post(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            message_id = await _commit_and_post(interaction, self._state)
        except Exception as exc:
            log.exception("Failed to post roster")
            await interaction.followup.send(f"Something went wrong: {exc}", ephemeral=True)
            return

        _discard(self._state)
        self.stop()
        await interaction.edit_original_response(
            content=f"✅ Roster posted! (message ID `{message_id}`)",
            embed=None,
            view=None,
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        _discard(self._state)
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── DB write + Discord post ───────────────────────────────────────────────────


async def _commit_and_post(interaction: discord.Interaction, state: FlowState) -> int:
    assert interaction.guild is not None

    channel = interaction.guild.get_channel(state.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(f"Channel {state.channel_id} not available as a text channel")

    all_slots = [
        (s.slot_role_id, s.label, s.quantity, s.slot_type, i)
        for i, s in enumerate(state.staff_slots + state.driver_slots)
    ]

    async with db.connect() as conn:
        if state.existing_team is None:
            team_id = await queries.insert_team(
                conn,
                guild_id=state.guild_id,
                key=state.team_key,
                name=state.display_name,
                team_role_id=state.team_role_id,
                channel_id=state.channel_id,
                tagline=state.tagline or None,
                logo_url=state.logo_url or None,
            )
        else:
            team_id = state.existing_team.id
            await queries.update_team(
                conn,
                team_id=team_id,
                name=state.display_name,
                team_role_id=state.team_role_id,
                channel_id=state.channel_id,
                tagline=state.tagline or None,
                logo_url=state.logo_url or None,
            )

        await queries.replace_slots(conn, team_id, all_slots)
        await conn.commit()
        team = await queries.fetch_team_by_id(conn, team_id)
        assert team is not None

    embed = build_embed(team, list(interaction.guild.members))

    if state.existing_team and state.existing_team.message_id:
        try:
            old_msg = await channel.fetch_message(state.existing_team.message_id)
            await old_msg.edit(embed=embed)
            return old_msg.id
        except discord.NotFound:
            pass

    msg = await channel.send(embed=embed)
    async with db.connect() as conn:
        await queries.set_message_id(conn, team_id, msg.id)
        await conn.commit()
    return msg.id
