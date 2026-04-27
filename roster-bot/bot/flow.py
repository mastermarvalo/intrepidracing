"""
Guided create/edit flow for /roster create and /roster edit.

The flow is nine steps:
  1. Modal  — team name, tagline, logo URL
  2. View   — pick team color (presets or custom hex)
  3. View   — pick the "team role" (who counts as on this team)
  4. View   — pick the "principal role" that can sign/drop players (optional)
  5. View   — build staff slots (Add/Done loop)
  6. View   — build driver slots (same loop)
  7. View   — pick the channel to post in
  8. View   — optional info box (custom labeled section)
  9. View   — preview and confirm

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
"Add slot" sub-flow (steps 5/6) sends a SECOND ephemeral message for the role select
instead of editing the builder in place.

The custom hex path in step 2 follows the same pattern: the _CustomHexModal sends a
new ephemeral message for step 3, leaving the color-picker message stale.
"""

import logging
from dataclasses import dataclass, field
from typing import Optional

import discord

from bot import db, queries
from bot.models import Team, TeamSlot
from bot.render import build_embed, build_flair_embed, build_roster_embeds, roster_flair_file

log = logging.getLogger(__name__)

# ── session storage ───────────────────────────────────────────────────────────

# Keyed by (guild_id, user_id).  One in-flight flow per user per server.
_sessions: dict[tuple[int, int], "FlowState"] = {}

# ── color presets ─────────────────────────────────────────────────────────────

# (label, rgb_int)
_PRESET_COLORS: list[tuple[str, int]] = [
    ("🔴 Red", 0xE74C3C),
    ("🟠 Orange", 0xE67E22),
    ("🟡 Gold", 0xF1C40F),
    ("🟢 Green", 0x27AE60),
    ("🩵 Teal", 0x1ABC9C),
    ("🔵 Blue", 0x3498DB),
    ("🌑 Navy", 0x2C3E50),
    ("🟣 Purple", 0x9B59B6),
    ("🩷 Pink", 0xE91E8C),
    ("⬜ White", 0xECF0F1),
    ("🩶 Silver", 0x95A5A6),
    ("⬛ Black", 0x2C2F33),
]


def _parse_hex_color(value: str) -> int | None:
    value = value.strip().lstrip("#")
    if len(value) == 6 and all(c in "0123456789abcdefABCDEF" for c in value):
        return int(value, 16)
    return None


@dataclass
class SlotDraft:
    """One slot as entered by the admin — not yet written to the DB."""

    label: str
    quantity: int
    slot_role_id: int
    slot_type: str  # "staff" or "driver"


@dataclass
class FlowState:
    """All data collected across the eight steps for a single create/edit session."""

    guild_id: int
    user_id: int
    team_key: str  # the short identifier, e.g. "redbull"

    # Populated when editing an existing team; None when creating a new one.
    existing_team: Optional[Team] = None

    # Step 1
    display_name: str = ""
    tagline: str = ""
    logo_url: str = ""
    banner_url: str = ""

    # Step 2
    color: Optional[int] = None
    dark_mode: bool = False

    # Step 3
    team_role_id: int = 0

    # Step 4 — role allowed to sign/drop players for this team (optional)
    principal_role_id: Optional[int] = None

    # Steps 5 & 6 — filled incrementally via the slot-builder loop
    staff_slots: list[SlotDraft] = field(default_factory=list)
    driver_slots: list[SlotDraft] = field(default_factory=list)

    # Step 7
    channel_id: int = 0

    # Step 8 — optional info box
    info_label: str = ""
    info_body: str = ""

    # When True (edit flow), each section returns to the edit menu instead of the
    # next step.  False for create and relink flows (linear progression).
    menu_mode: bool = False

    # Set during relink flow — the existing Discord message to take over.
    # When present, step 7 (channel picker) is skipped and _commit_and_post
    # edits this message in place instead of posting a new one.
    relink_message_id: Optional[int] = None

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


def _parse_message_link(raw: str) -> tuple[int, int] | None:
    """Parse a Discord message URL into (channel_id, message_id), or None on failure.

    Accepts:
      https://discord.com/channels/{guild}/{channel}/{message}
      https://ptb.discord.com/channels/...
      {channel_id}/{message_id}
    """
    raw = raw.strip()
    if "discord.com/channels/" in raw:
        tail = raw.split("/channels/")[-1].rstrip("/")
        parts = tail.split("/")
        # tail is guild/channel/message — we want the last two
        if len(parts) >= 2 and all(p.isdigit() for p in parts[-2:]):
            return int(parts[-2]), int(parts[-1])
    # bare channel_id/message_id
    parts = raw.split("/")
    if len(parts) == 2 and all(p.isdigit() for p in parts):
        return int(parts[0]), int(parts[1])
    return None


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
        banner_url=existing.banner_url or "",
        color=existing.color,
        dark_mode=existing.dark_mode,
        team_role_id=existing.team_role_id,
        principal_role_id=existing.principal_role_id,
        channel_id=existing.channel_id,
        info_label=existing.info_label or "",
        info_body=existing.info_body or "",
    )
    for s in existing.slots:
        draft = SlotDraft(
            label=s.label, quantity=s.quantity, slot_role_id=s.slot_role_id, slot_type=s.slot_type
        )
        target = state.staff_slots if s.slot_type == "staff" else state.driver_slots
        target.append(draft)

    state.menu_mode = True
    _save(state)
    await _show_edit_menu(interaction, state, new_message=True)


async def start_relink(interaction: discord.Interaction, team_key: str) -> None:
    """Kick off the relink flow. Opens a modal that collects team info + the existing message URL."""
    assert interaction.guild_id is not None

    async with db.connect() as conn:
        existing = await queries.fetch_team(conn, interaction.guild_id, team_key)
    if existing is not None:
        await interaction.response.send_message(
            f"Team `{team_key}` already exists in the database. "
            f"Use `/roster edit {team_key}` to modify it.",
            ephemeral=True,
        )
        return

    state = FlowState(guild_id=interaction.guild_id, user_id=interaction.user.id, team_key=team_key)
    _save(state)
    await interaction.response.send_modal(_RelinkStep1Modal(state))


# ── edit menu (used instead of linear steps when editing) ────────────────────


def _edit_menu_content(state: FlowState) -> str:
    color_hex = f"`#{state.color:06X}`" if state.color else "`default`"
    dm = " 🌑" if state.dark_mode else ""
    staff = f"{len(state.staff_slots)} slot(s)" if state.staff_slots else "none"
    drivers = f"{len(state.driver_slots)} slot(s)" if state.driver_slots else "none"
    info = f"`{state.info_label}`" if state.info_label else "none"
    return (
        f"**Editing: {state.display_name}**\n"
        f"Color: {color_hex}{dm} | Staff: {staff} | Drivers: {drivers} | Info box: {info}\n\n"
        "Select a section to edit, then click **Finish →** when done."
    )


async def _show_edit_menu(
    interaction: discord.Interaction, state: FlowState, *, new_message: bool = False
) -> None:
    view = _EditMenuView(state)
    content = _edit_menu_content(state)
    if new_message:
        await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=content, view=view, embed=None, attachments=[])


class _EditMenuView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=600)
        self._state = state
        self._sel = discord.ui.Select(
            placeholder="Select section to edit…",
            options=[
                discord.SelectOption(label="Team info", value="info",
                    description="Name, tagline, logo, accent image"),
                discord.SelectOption(label="Team color", value="color"),
                discord.SelectOption(label="Team role", value="team_role"),
                discord.SelectOption(label="Principal role", value="principal"),
                discord.SelectOption(label="Staff slots", value="staff"),
                discord.SelectOption(label="Driver slots", value="drivers"),
                discord.SelectOption(label="Channel", value="channel"),
                discord.SelectOption(label="Info box", value="info_box",
                    description="Custom labeled section"),
            ],
        )
        self._sel.callback = self._on_select
        self.add_item(self._sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = self._sel.values[0]
        self.stop()
        if value == "info":
            await interaction.response.send_modal(_Step1Modal(self._state))
        elif value == "color":
            await _show_step2_color(interaction, self._state, from_modal=False)
        elif value == "team_role":
            await _show_step3_team_role(interaction, self._state)
        elif value == "principal":
            await _show_step4_principal_role(interaction, self._state)
        elif value == "staff":
            await _show_builder(interaction, self._state, slot_type="staff", step_num=5)
        elif value == "drivers":
            await _show_builder(interaction, self._state, slot_type="driver", step_num=6)
        elif value == "channel":
            await _show_step7_channel(interaction, self._state)
        elif value == "info_box":
            await _show_step8_info(interaction, self._state)

    @discord.ui.button(label="Finish →", style=discord.ButtonStyle.success, row=1)
    async def finish(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── step 1: text fields ───────────────────────────────────────────────────────


class _Step1Modal(discord.ui.Modal, title="Team Setup (1/9)"):
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
        label="Logo image (optional)",
        placeholder="https://example.com/logo.png",
        required=False,
        max_length=256,
    )
    banner_url = discord.ui.TextInput(
        label="Accent image (optional)",
        placeholder="https://example.com/banner.png",
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
        if state.banner_url:
            self.banner_url.default = state.banner_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._state.display_name = self.team_name.value.strip()
        self._state.tagline = self.tagline.value.strip()
        self._state.logo_url = self.logo_url.value.strip()
        self._state.banner_url = self.banner_url.value.strip()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state, new_message=True)
        else:
            await _show_step2_color(interaction, self._state)


class _RelinkStep1Modal(discord.ui.Modal, title="Team Relink — Setup"):
    """Step 1 for the relink flow. Same fields as create, plus the existing message URL."""

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
        label="Logo image (optional)",
        placeholder="https://example.com/logo.png",
        required=False,
        max_length=256,
    )
    banner_url = discord.ui.TextInput(
        label="Accent image (optional)",
        placeholder="https://example.com/banner.png",
        required=False,
        max_length=256,
    )
    message_link = discord.ui.TextInput(
        label="Existing roster message URL",
        placeholder="https://discord.com/channels/.../.../..",
        max_length=200,
    )

    def __init__(self, state: FlowState) -> None:
        super().__init__()
        self._state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        parsed = _parse_message_link(self.message_link.value)
        if parsed is None:
            await interaction.response.send_message(
                "Could not parse the message URL. "
                "Please paste the full Discord message link "
                "(right-click the message → **Copy Message Link**).",
                ephemeral=True,
            )
            _discard(self._state)
            return

        channel_id, message_id = parsed
        self._state.display_name = self.team_name.value.strip()
        self._state.tagline = self.tagline.value.strip()
        self._state.logo_url = self.logo_url.value.strip()
        self._state.banner_url = self.banner_url.value.strip()
        self._state.channel_id = channel_id
        self._state.relink_message_id = message_id
        await _show_step2_color(interaction, self._state)


# ── step 2: team color ────────────────────────────────────────────────────────


async def _show_step2_color(
    interaction: discord.Interaction, state: FlowState, *, from_modal: bool = True
) -> None:
    view = _Step2ColorView(state)
    dm_note = "\n**Clarity mode: ON** — flair background darkened so white text shows." if state.dark_mode else ""
    content = (
        "**Step 2 of 9 — Team color**\n"
        "Pick a color for this team's embeds, or enter a custom hex code.\n"
        f"This color will appear on all roster and transaction messages.{dm_note}"
    )
    if from_modal:
        await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=content, view=view)


class _Step2ColorView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state

        options = [
            discord.SelectOption(label=label, value=str(rgb))
            for label, rgb in _PRESET_COLORS
        ]
        options.append(discord.SelectOption(label="🎨 Custom hex code…", value="custom"))
        if state.color is not None:
            options.append(discord.SelectOption(label="⏩ Keep current color", value="keep"))
        options.append(discord.SelectOption(label="⏭️ Skip (use default)", value="skip"))

        self._sel = discord.ui.Select(placeholder="Choose a color…", options=options)
        self._sel.callback = self._on_select
        self.add_item(self._sel)

        dm_label = "🌑 Clarity: ON" if state.dark_mode else "☀️ Clarity: OFF"
        dm_style = discord.ButtonStyle.primary if state.dark_mode else discord.ButtonStyle.secondary
        dm_btn = discord.ui.Button(label=dm_label, style=dm_style, row=1)
        dm_btn.callback = self._toggle_dark_mode
        self.add_item(dm_btn)

        self.finish_editing.disabled = state.existing_team is None

    async def _on_select(self, interaction: discord.Interaction) -> None:
        value = self._sel.values[0]
        if value == "custom":
            await interaction.response.send_modal(_CustomHexModal(self._state))
            return
        if value == "keep":
            pass  # retain existing color
        elif value == "skip":
            self._state.color = None
        else:
            self._state.color = int(value)
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step3_team_role(interaction, self._state)

    async def _toggle_dark_mode(self, interaction: discord.Interaction) -> None:
        self._state.dark_mode = not self._state.dark_mode
        self.stop()
        await _show_step2_color(interaction, self._state, from_modal=False)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary, row=1)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


class _CustomHexModal(discord.ui.Modal, title="Custom Team Color"):
    hex_input = discord.ui.TextInput(
        label="Hex color code",
        placeholder="#FF0000 or FF0000",
        min_length=6,
        max_length=7,
    )

    def __init__(self, state: FlowState) -> None:
        super().__init__()
        self._state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        color = _parse_hex_color(self.hex_input.value)
        if color is None:
            await interaction.response.send_message(
                "Invalid hex code. Use a format like `#FF0000` or `FF0000`.",
                ephemeral=True,
            )
            return
        self._state.color = color
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state, new_message=True)
        else:
            await _show_step3_team_role(interaction, self._state, from_modal=True)


# ── step 3: team role ─────────────────────────────────────────────────────────


async def _show_step3_team_role(
    interaction: discord.Interaction, state: FlowState, *, from_modal: bool = False
) -> None:
    view = _Step3TeamRoleView(state)
    content = (
        "**Step 3 of 9 — Team role**\n"
        "Pick the Discord role that marks someone as being on this team."
    )
    if from_modal:
        await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=content, view=view)


class _Step3TeamRoleView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        sel = discord.ui.RoleSelect(placeholder="Select team role…", min_values=1, max_values=1)
        sel.callback = self._on_select
        self.add_item(sel)
        self.keep_current.disabled = not bool(state.team_role_id)
        self.finish_editing.disabled = state.existing_team is None

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.RoleSelect = self.children[0]  # type: ignore[assignment]
        self._state.team_role_id = sel.values[0].id
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step4_principal_role(interaction, self._state)

    @discord.ui.button(label="Keep current →", style=discord.ButtonStyle.secondary)
    async def keep_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step4_principal_role(interaction, self._state)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── step 4: principal role ────────────────────────────────────────────────────


async def _show_step4_principal_role(interaction: discord.Interaction, state: FlowState) -> None:
    editing = state.existing_team is not None
    if editing and state.principal_role_id is not None:
        hint = "Click **Keep current →** to leave it unchanged, or **Clear →** to remove it."
    elif editing:
        hint = "Click **Skip →** to leave it admin-only."
    else:
        hint = "Click **Skip →** to leave it admin-only."
    view = _Step4PrincipalRoleView(state)
    await interaction.response.edit_message(
        content=(
            "**Step 4 of 9 — Principal role (optional)**\n"
            "Pick a role whose members can use `/roster sign` and `/roster drop` for this team.\n"
            + hint
        ),
        view=view,
    )


class _Step4PrincipalRoleView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        self._sel = discord.ui.RoleSelect(
            placeholder="Select principal role (optional)…", min_values=1, max_values=1
        )
        self._sel.callback = self._on_select
        self.add_item(self._sel)
        editing = state.existing_team is not None
        if editing and state.principal_role_id is not None:
            self.keep_current.disabled = False
            self.skip_or_clear.label = "Clear →"
        else:
            self.keep_current.disabled = True
        self.finish_editing.disabled = not editing

    async def _on_select(self, interaction: discord.Interaction) -> None:
        self._state.principal_role_id = self._sel.values[0].id
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_builder(interaction, self._state, slot_type="staff", step_num=5)

    @discord.ui.button(label="Keep current →", style=discord.ButtonStyle.secondary)
    async def keep_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_builder(interaction, self._state, slot_type="staff", step_num=5)

    @discord.ui.button(label="Skip →", style=discord.ButtonStyle.secondary)
    async def skip_or_clear(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._state.principal_role_id = None
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_builder(interaction, self._state, slot_type="staff", step_num=5)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── steps 5 & 6: slot builder loop ───────────────────────────────────────────
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
        f"**Step {step_num} of 9 — {label} slots**\n"
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
        slots = state.staff_slots if slot_type == "staff" else state.driver_slots
        if not slots:
            self.remove_slot.disabled = True
        self.finish_editing.disabled = state.existing_team is None

    @discord.ui.button(label="Add slot", style=discord.ButtonStyle.primary)
    async def add_slot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(
            _SlotTextModal(state=self._state, slot_type=self._slot_type, step_num=self._step_num)
        )

    @discord.ui.button(label="Remove slot", style=discord.ButtonStyle.danger)
    async def remove_slot(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        view = _SlotRemoveView(state=self._state, slot_type=self._slot_type, step_num=self._step_num)
        slots = self._state.staff_slots if self._slot_type == "staff" else self._state.driver_slots
        label = self._slot_type.capitalize()
        await interaction.response.edit_message(
            content=f"**Remove a {label.lower()} slot — select which one to delete:**\n\n"
                    f"**Current {label.lower()} slots:**\n{_slot_summary(slots)}",
            view=view,
        )

    @discord.ui.button(label="Done →", style=discord.ButtonStyle.success)
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        elif self._step_num == 5:
            await _show_builder(interaction, self._state, slot_type="driver", step_num=6)
        elif self._state.relink_message_id is not None:
            await _show_step9_confirm(interaction, self._state)
        else:
            await _show_step7_channel(interaction, self._state)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary, row=1)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

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
        await _show_builder(
            interaction, self._state, slot_type=self._slot_type, step_num=self._step_num
        )

    async def on_timeout(self) -> None:
        pass  # The pending slot is just dropped; the admin can click "Add slot" again


class _SlotRemoveView(discord.ui.View):
    def __init__(self, *, state: FlowState, slot_type: str, step_num: int) -> None:
        super().__init__(timeout=120)
        self._state = state
        self._slot_type = slot_type
        self._step_num = step_num
        slots = state.staff_slots if slot_type == "staff" else state.driver_slots
        options = [
            discord.SelectOption(
                label=f"{s.label} × {s.quantity}",
                value=str(i),
            )
            for i, s in enumerate(slots)
        ]
        self._sel = discord.ui.Select(placeholder="Select slot to remove…", options=options)
        self._sel.callback = self._on_select
        self.add_item(self._sel)

    async def _on_select(self, interaction: discord.Interaction) -> None:
        idx = int(self._sel.values[0])
        slots = self._state.staff_slots if self._slot_type == "staff" else self._state.driver_slots
        if 0 <= idx < len(slots):
            slots.pop(idx)
        self.stop()
        await _show_builder(interaction, self._state, slot_type=self._slot_type, step_num=self._step_num)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_builder(interaction, self._state, slot_type=self._slot_type, step_num=self._step_num)

    async def on_timeout(self) -> None:
        pass


# ── step 7: channel ───────────────────────────────────────────────────────────


async def _show_step7_channel(interaction: discord.Interaction, state: FlowState) -> None:
    view = _Step7ChannelView(state)
    hint = f" Click **Keep current →** to keep <#{state.channel_id}>." if state.channel_id else ""
    await interaction.response.edit_message(
        content=(
            "**Step 7 of 9 — Channel**\n"
            f"Pick the text or forum channel where the roster will be posted.{hint}"
        ),
        view=view,
    )


class _Step7ChannelView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        sel = discord.ui.ChannelSelect(
            placeholder="Select a text or forum channel…",
            channel_types=[discord.ChannelType.text, discord.ChannelType.forum],
            min_values=1,
            max_values=1,
        )
        sel.callback = self._on_select
        self.add_item(sel)
        self.keep_current.disabled = not bool(state.channel_id)
        self.finish_editing.disabled = state.existing_team is None

    async def _on_select(self, interaction: discord.Interaction) -> None:
        sel: discord.ui.ChannelSelect = self.children[0]  # type: ignore[assignment]
        self._state.channel_id = sel.values[0].id
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step8_info(interaction, self._state)

    @discord.ui.button(label="Keep current →", style=discord.ButtonStyle.secondary)
    async def keep_current(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step8_info(interaction, self._state)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


# ── step 8: additional info (optional) ───────────────────────────────────────


async def _show_step8_info(
    interaction: discord.Interaction, state: FlowState, *, from_modal: bool = False
) -> None:
    view = _Step8InfoView(state)
    current = ""
    if state.info_label and state.info_body:
        preview = state.info_body[:60] + ("…" if len(state.info_body) > 60 else "")
        current = f"\n\n**Current:** **{state.info_label}** — {preview}"
    content = (
        "**Step 8 of 9 — Additional info (optional)**\n"
        "Add a custom labeled section to the roster (e.g. accolades, achievements)."
        + current
    )
    if from_modal:
        await interaction.response.send_message(content, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=content, view=view)


class _Step8InfoView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state
        self.clear_info.disabled = not (state.info_label or state.info_body)
        self.finish_editing.disabled = state.existing_team is None

    @discord.ui.button(label="Set info box", style=discord.ButtonStyle.primary)
    async def set_info(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(_InfoBoxModal(self._state))

    @discord.ui.button(label="Clear", style=discord.ButtonStyle.danger)
    async def clear_info(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self._state.info_label = ""
        self._state.info_body = ""
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step8_info(interaction, self._state)

    @discord.ui.button(label="Next →", style=discord.ButtonStyle.success)
    async def next_step(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state)
        else:
            await _show_step9_confirm(interaction, self._state)

    @discord.ui.button(label="Finish editing →", style=discord.ButtonStyle.primary)
    async def finish_editing(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await _show_step9_confirm(interaction, self._state)

    async def on_timeout(self) -> None:
        _discard(self._state)


class _InfoBoxModal(discord.ui.Modal, title="Info Box"):
    label_input = discord.ui.TextInput(
        label="Section label",
        placeholder="Accolades",
        max_length=64,
    )
    body_input = discord.ui.TextInput(
        label="Content",
        placeholder="2x WCC | 3x WDC",
        style=discord.TextStyle.paragraph,
        max_length=1024,
    )

    def __init__(self, state: FlowState) -> None:
        super().__init__()
        self._state = state
        if state.info_label:
            self.label_input.default = state.info_label
        if state.info_body:
            self.body_input.default = state.info_body

    async def on_submit(self, interaction: discord.Interaction) -> None:
        self._state.info_label = self.label_input.value.strip()
        self._state.info_body = self.body_input.value.strip()
        if self._state.menu_mode:
            await _show_edit_menu(interaction, self._state, new_message=True)
        else:
            await _show_step8_info(interaction, self._state, from_modal=True)


# ── step 9: preview & confirm ─────────────────────────────────────────────────


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
        banner_url=state.banner_url or None,
        principal_role_id=state.principal_role_id,
        color=state.color,
        info_label=state.info_label or None,
        info_body=state.info_body or None,
        dark_mode=state.dark_mode,
        slots=slots,
    )


async def _show_step9_confirm(interaction: discord.Interaction, state: FlowState) -> None:
    assert interaction.guild is not None
    preview = _preview_team(state)
    flair = roster_flair_file(preview)
    view = _Step9ConfirmView(state)
    if state.relink_message_id is not None:
        content = (
            "**Confirm — Relink existing roster**\n"
            "Here's how the roster will look. "
            f"The existing message in <#{state.channel_id}> will be updated in place."
        )
    else:
        content = (
            "**Step 9 of 9 — Confirm**\n"
            "Here's how the roster will look. Member mentions are live.\n"
            f"Posting to <#{state.channel_id}>."
        )
    await interaction.response.edit_message(
        content=content,
        embeds=[build_flair_embed(preview.color)] + build_roster_embeds(preview, list(interaction.guild.members)),
        view=view,
        attachments=[flair],
    )


class _Step9ConfirmView(discord.ui.View):
    def __init__(self, state: FlowState) -> None:
        super().__init__(timeout=300)
        self._state = state

    @discord.ui.button(label="Post roster", style=discord.ButtonStyle.success)
    async def post(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
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

_RosterChannel = discord.TextChannel | discord.ForumChannel


async def _get_thread(client: discord.Client, thread_id: int) -> discord.Thread | None:
    """Return a Thread from cache, falling back to a fetch."""
    ch = client.get_channel(thread_id)
    if isinstance(ch, discord.Thread):
        return ch
    try:
        ch = await client.fetch_channel(thread_id)
        return ch if isinstance(ch, discord.Thread) else None
    except (discord.NotFound, discord.Forbidden):
        return None


async def _fetch_roster_msg(
    client: discord.Client,
    channel: _RosterChannel,
    message_id: int,
) -> discord.Message | None:
    """Fetch the roster message from either a text channel or a forum thread."""
    try:
        if isinstance(channel, discord.TextChannel):
            return await channel.fetch_message(message_id)
        else:  # ForumChannel — message_id == thread_id
            thread = await _get_thread(client, message_id)
            return await thread.fetch_message(message_id) if thread else None
    except (discord.NotFound, discord.Forbidden):
        return None


async def _commit_and_post(interaction: discord.Interaction, state: FlowState) -> int:
    """Save the team to the DB and post (or update) the roster embed. Returns the message ID."""
    assert interaction.guild is not None

    channel = interaction.guild.get_channel(state.channel_id)
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        raise ValueError(f"Channel {state.channel_id} is not available as a text or forum channel")

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
                banner_url=state.banner_url or None,
                principal_role_id=state.principal_role_id,
                color=state.color,
                info_label=state.info_label or None,
                info_body=state.info_body or None,
                dark_mode=state.dark_mode,
            )
        else:
            team_id = state.existing_team.id
            await queries.update_team(
                conn, team_id=team_id, name=state.display_name,
                team_role_id=state.team_role_id, channel_id=state.channel_id,
                tagline=state.tagline or None, logo_url=state.logo_url or None,
                banner_url=state.banner_url or None,
                principal_role_id=state.principal_role_id,
                color=state.color,
                info_label=state.info_label or None,
                info_body=state.info_body or None,
                dark_mode=state.dark_mode,
            )

        await queries.replace_slots(conn, team_id, all_slots)
        await conn.commit()
        team = await queries.fetch_team_by_id(conn, team_id)
        assert team is not None

    guild_members = list(interaction.guild.members)
    flair = roster_flair_file(team)
    embeds = [build_flair_embed(team.color)] + build_roster_embeds(team, guild_members)

    # Relink: take over an existing message
    if state.relink_message_id is not None:
        existing_msg = await _fetch_roster_msg(interaction.client, channel, state.relink_message_id)
        if existing_msg is not None:
            try:
                await existing_msg.edit(embeds=embeds, attachments=[flair])
                async with db.connect() as conn:
                    await queries.set_message_id(conn, team_id, existing_msg.id)
                    await conn.commit()
                return existing_msg.id
            except discord.Forbidden:
                log.warning("No permission to edit relink target message %s", state.relink_message_id)
        else:
            log.warning("Relink target message %s not found — posting new", state.relink_message_id)

    # Normal edit: update the existing posted message
    if state.existing_team and state.existing_team.message_id:
        old_msg = await _fetch_roster_msg(interaction.client, channel, state.existing_team.message_id)
        if old_msg is not None:
            await old_msg.edit(embeds=embeds, attachments=[flair])
            return old_msg.id
        # Message was deleted; fall through and send a new one

    # New post
    if isinstance(channel, discord.ForumChannel):
        thread = await channel.create_thread(name=team.name, embeds=embeds, file=flair)
        msg_id = thread.id
    else:
        msg = await channel.send(embeds=embeds, file=flair)
        msg_id = msg.id
    async with db.connect() as conn:
        await queries.set_message_id(conn, team_id, msg_id)
        await conn.commit()
    return msg_id
