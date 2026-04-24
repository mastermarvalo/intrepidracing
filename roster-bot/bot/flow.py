"""
Guided create/edit flow for /roster create and /roster edit.

The flow is six steps:
  1. Modal  — team name, tagline, logo URL
  2. View   — pick the "team role" (who counts as on this team)
  3. View   — build staff slots (Add/Done loop)
  4. View   — build driver slots (same loop)
  5. View   — pick the channel to post in
  6. View   — preview and confirm

State is collected in a FlowState object and stored in `_sessions` while the
admin works through the steps.  It's discarded when the flow finishes or times out.

--- Discord interaction model (read this if something looks weird) ---

Every Discord interaction (slash command, button click, modal submit, select) must
be "responded to" exactly once.  The response type matters:

  - send_message  → creates a new (ephemeral) message
  - edit_message  → edits the message the button/select was on
  - send_modal    → opens a popup form; the underlying message is unchanged

After a MODAL submit, the only valid responses are send_message or defer+followup.
You cannot edit an existing message from a modal's on_submit.  This is why the
"Add slot" sub-flow (steps 3/4) sends a SECOND ephemeral message for the role select
instead of editing the builder in place.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import discord

from bot import db, queries
from bot.models import Team, TeamSlot
from bot.render import build_embed

log = logging.getLogger(__name__)

# ── session storage ───────────────────────────────────────────────────────────

# Keyed by (guild_id, user_id).  One in-flight flow per user per server.
_sessions: dict[tuple[int, int], "FlowState"] = {}


@dataclass
class SlotDraft:
    """One slot as entered by the admin — not yet written to the DB."""

    label: str
    quantity: int
    slot_role_id: int
    slot_type: str  # "staff" or "driver"


@dataclass
class FlowState:
    """All data collected across the six steps for a single create/edit session."""

    guild_id: int
    user_id: int
    team_key: str  # the short identifier, e.g. "redbull"

    # Populated when editing an existing team; None when creating a new one.
    existing_team: Optional[Team] = None

    # Step 1
    display_name: str = ""
    tagline: str = ""
    logo_url: str = ""

    # Step 2
    team_role_id: int = 0

    # Steps 3 & 4 — filled incrementally via the slot-builder loop
    staff_slots: list[SlotDraft] = field(default_factory=list)
    driver_slots: list[SlotDraft] = field(default_factory=list)

    # Step 5
    channel_id: int = 0

    # Tracks the active builder view so we can stop it when a new builder message
    # appears.  The "Add slot" sub-flow (modal → role select) always creates a new
    # ephemeral message, leaving the old builder message stale.  Stopping the old
    # view makes its buttons fail loudly rather than silently acting on old data.
    _current_builder: Optional[discord.ui.View] = field(default=None, repr=False)

    def session_key(self) -> tuple[int, int]:
        return (self.guild_id, self.user_id)


def _save(state: FlowState) -> None:
    _sessions[state.session_key()] = state


def _discard(state: FlowState) -> None:
    _sessions.pop(state.session_key(), None)


# ── entry points (called by cog) ──────────────────────────────────────────────


async def start_create(interaction: discord.Interaction, team_key: str) -> None:
    """Kick off the create flow.  Opens Step 1 modal."""
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

    state = FlowState(guild_id=interaction.guild_id, user_id=interaction.user.id, team_key=team_key)
    _save(state)
    await interaction.response.send_modal(_Step1Modal(state))


async def start_edit(interaction: discord.Interaction, team_key: str) -> None:
    """Kick off the edit flow pre-filled from the DB.  Opens Step 1 modal."""
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
        existing_team=existing,
        display_name=existing.name,
        tagline=existing.tagline or "",
        logo_url=existing.logo_url or "",
        team_role_id=existing.team_role_id,
        channel_id=existing.channel_id,
    )
    for s in existing.slots:
        draft = SlotDraft(
            label=s.label, quantity=s.quantity, slot_role_id=s.slot_role_id, slot_type=s.slot_type
        )
        target = state.staff_slots if s.slot_type == "staff" else state.driver_slots
        target.append(draft)

    _save(state)
    await interaction.response.send_modal(_Step1Modal(state))


# ── step 1: text fields ───────────────────────────────────────────────────────
# A Modal is a popup form with up to 5 TextInput fields.
# on_submit is called when the admin clicks Submit.


class _Step1Modal(discord.ui.Modal, title="Team Setup (1/6)"):
    team_name = discord.ui.TextInput(
        label="Display name", placeholder="Red Bull Racing", max_length=64
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
        # Pre-fill when editing
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
        # Modal submit → must send a NEW message (can't edit anything)
        await _show_step2(interaction, self._state)


# ── step 2: team role ─────────────────────────────────────────────────────────
# A View is a set of interactive components attached to a message.
# RoleSelect lets the admin pick from the server's roles.


async def _show_step2(interaction: discord.Interaction, state: FlowState) -> None:
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
        # children[0] is the RoleSelect we added above
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        self._state.team_role_id = sel.values[0].id
        self.stop()
        # Component interaction → edit the same message in place
        await _show_builder(interaction, self._state, slot_type="staff", step_num=3)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── steps 3 & 4: slot builder loop ───────────────────────────────────────────
# The admin can add as many slots as they want.  Each "Add slot" click opens a
# modal (label + quantity), then a role-select message.  "Done →" advances.
#
# Because modal submit must send a NEW message, each "Add slot" produces a
# second ephemeral message.  The role-select edits THAT message into a fresh
# builder.  The old builder message becomes stale; its view is stopped so its
# buttons don't cause confusion.


def _slot_summary(slots: list[SlotDraft]) -> str:
    if not slots:
        return "*(none yet)*"
    return "\n".join(f"• **{s.label}** × {s.quantity} — <@&{s.slot_role_id}>" for s in slots)


async def _show_builder(
    interaction: discord.Interaction,
    state: FlowState,
    slot_type: str,
    step_num: int,
) -> None:
    if state._current_builder is not None:
        state._current_builder.stop()

    slots = state.staff_slots if slot_type == "staff" else state.driver_slots
    label = slot_type.capitalize()
    content = (
        f"**Step {step_num} of 6 — {label} slots**\n"
        f"Add as many {label.lower()} slots as you need, then click **Done →**.\n\n"
        f"**Current {label.lower()} slots:**\n{_slot_summary(slots)}"
    )
    view = _BuilderView(state=state, slot_type=slot_type, step_num=step_num)
    state._current_builder = view
    await interaction.response.edit_message(content=content, view=view)


class _BuilderView(discord.ui.View):
    def __init__(self, *, state: FlowState, slot_type: str, step_num: int) -> None:
        super().__init__(timeout=600)
        self._state = state
        self._slot_type = slot_type
        self._step_num = step_num

    @discord.ui.button(label="Add slot", style=discord.ButtonStyle.primary)
    async def add_slot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Opening a modal is the response to this button click.
        # The builder message stays visible; the modal appears on top.
        await interaction.response.send_modal(
            _SlotTextModal(state=self._state, slot_type=self._slot_type, step_num=self._step_num)
        )

    @discord.ui.button(label="Done →", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._step_num == 3:
            await _show_builder(interaction, self._state, slot_type="driver", step_num=4)
        else:
            await _show_step5(interaction, self._state)

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

        label = self.label_input.value.strip()
        qty = int(raw)

        # Can't edit the builder message here (modal submit → must send_message).
        # We send a new ephemeral message with just the role select.
        # When the role is picked, THAT message gets edited into the next builder.
        view = _SlotRoleView(
            state=self._state,
            label=label,
            qty=qty,
            slot_type=self._slot_type,
            step_num=self._step_num,
        )
        await interaction.response.send_message(
            f'**Pick the role for "{label}":**', view=view, ephemeral=True
        )


class _SlotRoleView(discord.ui.View):
    def __init__(
        self, *, state: FlowState, label: str, qty: int, slot_type: str, step_num: int
    ) -> None:
        super().__init__(timeout=120)
        self._state = state
        self._label = label
        self._qty = qty
        self._slot_type = slot_type
        self._step_num = step_num
        sel = discord.ui.RoleSelect(
            placeholder="Select role for this slot…", min_values=1, max_values=1
        )
        sel.callback = self._on_select
        self.add_item(sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        draft = SlotDraft(
            label=self._label,
            quantity=self._qty,
            slot_role_id=sel.values[0].id,
            slot_type=self._slot_type,
        )
        target = self._state.staff_slots if draft.slot_type == "staff" else self._state.driver_slots
        target.append(draft)

        self.stop()
        # edit_message turns this role-select message into the refreshed builder.
        await _show_builder(
            interaction, self._state, slot_type=self._slot_type, step_num=self._step_num
        )

    async def on_timeout(self) -> None:
        pass  # The pending slot is just dropped; the admin can click "Add slot" again


# ── step 5: channel ───────────────────────────────────────────────────────────


async def _show_step5(interaction: discord.Interaction, state: FlowState) -> None:
    view = _Step5View(state)
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
        await _show_step6(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── step 6: preview & confirm ─────────────────────────────────────────────────


def _preview_team(state: FlowState) -> Team:
    """Build a temporary Team from current flow state, used only for the embed preview."""
    slots = [
        TeamSlot(
            id=i, team_id=0,
            slot_role_id=s.slot_role_id, label=s.label, quantity=s.quantity,
            slot_type=s.slot_type,  # type: ignore[arg-type]
            sort_order=i,
        )
        for i, s in enumerate(state.staff_slots + state.driver_slots)
    ]
    return Team(
        id=0, guild_id=state.guild_id, key=state.team_key,
        name=state.display_name, team_role_id=state.team_role_id,
        channel_id=state.channel_id,
        tagline=state.tagline or None, logo_url=state.logo_url or None,
        slots=slots,
    )


async def _show_step6(interaction: discord.Interaction, state: FlowState) -> None:
    assert interaction.guild is not None
    embed = build_embed(_preview_team(state), list(interaction.guild.members))
    view = _Step6View(state)
    await interaction.response.edit_message(
        content=(
            "**Step 6 of 6 — Confirm**\n"
            "Here's how the roster will look. Member mentions are live.\n"
            f"Posting to <#{state.channel_id}>."
        ),
        embed=embed,
        view=view,
    )


class _Step6View(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state

    @discord.ui.button(label="Post roster", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        # Defer immediately so we have time to write to the DB and post the message.
        # After deferring, use edit_original_response to update the ephemeral message.
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
            content=f"✅ Roster posted! (message ID `{message_id}`)", embed=None, view=None
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        _discard(self._state)
        self.stop()
        await interaction.response.edit_message(content="Cancelled.", embed=None, view=None)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── write to DB and post the embed ────────────────────────────────────────────


async def _commit_and_post(interaction: discord.Interaction, state: FlowState) -> int:
    """Save the team to the DB and post (or update) the roster embed. Returns the message ID."""
    assert interaction.guild is not None

    channel = interaction.guild.get_channel(state.channel_id)
    if not isinstance(channel, discord.TextChannel):
        raise ValueError(f"Channel {state.channel_id} is not available as a text channel")

    # Each slot is stored as (role_id, label, quantity, type, order)
    all_slots = [
        (s.slot_role_id, s.label, s.quantity, s.slot_type, i)
        for i, s in enumerate(state.staff_slots + state.driver_slots)
    ]

    async with db.connect() as conn:
        if state.existing_team is None:
            team_id = await queries.insert_team(
                conn,
                guild_id=state.guild_id, key=state.team_key, name=state.display_name,
                team_role_id=state.team_role_id, channel_id=state.channel_id,
                tagline=state.tagline or None, logo_url=state.logo_url or None,
            )
        else:
            team_id = state.existing_team.id
            await queries.update_team(
                conn, team_id=team_id, name=state.display_name,
                team_role_id=state.team_role_id, channel_id=state.channel_id,
                tagline=state.tagline or None, logo_url=state.logo_url or None,
            )

        await queries.replace_slots(conn, team_id, all_slots)
        await conn.commit()
        team = await queries.fetch_team_by_id(conn, team_id)
        assert team is not None

    embed = build_embed(team, list(interaction.guild.members))

    # If editing and the old message still exists, update it in place
    if state.existing_team and state.existing_team.message_id:
        try:
            old_msg = await channel.fetch_message(state.existing_team.message_id)
            await old_msg.edit(embed=embed)
            return old_msg.id
        except discord.NotFound:
            pass  # Message was deleted; fall through and send a new one

    msg = await channel.send(embed=embed)
    async with db.connect() as conn:
        await queries.set_message_id(conn, team_id, msg.id)
        await conn.commit()
    return msg.id
