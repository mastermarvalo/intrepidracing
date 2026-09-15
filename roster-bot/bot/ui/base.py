"""
Shared building blocks for the control panel's screens.

These live outside `cogs/panel.py` so the individual screens can import
them without importing the panel itself, which would be circular. The
screens receive their "go back" behaviour as a callback for the same
reason — a screen never needs to know what is above it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import discord

# Panels are ephemeral working surfaces, not permanent furniture. Ten
# minutes is long enough to finish a race night without leaving stale
# buttons around that would fail on click after a restart.
PANEL_TIMEOUT_SECONDS = 600

# Discord platform limits.
SELECT_MAX_OPTIONS = 25
EMBED_FIELD_LIMIT = 1024
MODAL_MAX_INPUTS = 5

COLOR_OK = discord.Color.from_str("#2e7d32")
COLOR_INFO = discord.Color.from_str("#1a3d6d")
COLOR_WARN = discord.Color.from_str("#c0392b")

BackCallback = Callable[[discord.Interaction], Awaitable[None]]


def truncate_field(text: str, limit: int = EMBED_FIELD_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def is_admin(interaction: discord.Interaction) -> bool:
    """
    Manage Server is the panel's admin gate.

    Matches `_is_admin` in the cogs exactly; the panel must not be a
    softer door into admin actions than the slash commands are.
    """
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


class OwnedView(discord.ui.View):
    """
    A view only its opener may interact with.

    Panels are sent ephemerally, but Discord still delivers component
    interactions from anyone who can see the message, and an ephemeral
    reply can be visible after a client resync. Without this check a
    second admin could click Approve on a queue someone else was mid-way
    through triaging.
    """

    def __init__(
        self, *, opener_id: int, timeout: float | None = PANEL_TIMEOUT_SECONDS
    ) -> None:
        super().__init__(timeout=timeout)
        self.opener_id = opener_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message(
                "This panel belongs to whoever opened it. Run `/league` to get "
                "your own.",
                ephemeral=True,
            )
            return False
        return True


class AdminOwnedView(OwnedView):
    """
    An `OwnedView` that additionally re-checks Manage Server on click.

    The permission is re-checked per interaction rather than trusted from
    when the panel was opened, so a permission removed mid-session takes
    effect immediately.
    """

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not await super().interaction_check(interaction):
            return False
        if not is_admin(interaction):
            await interaction.response.send_message(
                "This action needs the **Manage Server** permission.",
                ephemeral=True,
            )
            return False
        return True


class BackButton(discord.ui.Button):
    """Navigates up one screen via a caller-supplied callback."""

    def __init__(
        self, on_back: BackCallback, *, label: str = "Back", row: int | None = None
    ) -> None:
        super().__init__(
            label=label, style=discord.ButtonStyle.secondary, emoji="◀", row=row
        )
        self._on_back = on_back

    async def callback(self, interaction: discord.Interaction) -> None:
        await self._on_back(interaction)


async def report_error(interaction: discord.Interaction, message: str) -> None:
    """
    Surface a user-facing failure without destroying the current screen.

    Uses a follow-up when the interaction has already been responded to
    or deferred, which is the common case inside a button callback.
    """
    text = f"❌ {message}"
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=True)
    else:
        await interaction.response.send_message(text, ephemeral=True)
