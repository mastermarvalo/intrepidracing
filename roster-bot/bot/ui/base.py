"""
Shared building blocks for the control panel's screens.

These live outside `cogs/panel.py` so the individual screens can import
them without importing the panel itself, which would be circular. The
screens receive their "go back" behaviour as a callback for the same
reason — a screen never needs to know what is above it.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

import discord

log = logging.getLogger(__name__)

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
        # Set by a screen that has work in flight worth naming when the
        # panel expires — e.g. a priced-but-unpublished valuation run.
        self.expiry_hint: str | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message(
                "This panel belongs to whoever opened it. Run `/league` to get "
                "your own.",
                ephemeral=True,
            )
            return False
        return True

    async def bind_message(self, interaction: discord.Interaction) -> None:
        """
        Remember the message this view is attached to, so `on_timeout` can
        edit it.

        discord.py only populates `View.message` when the view is sent with
        `channel.send(view=...)`. Panels are ephemeral interaction
        responses, so every screen has to fetch it once. Failures are
        swallowed: not being able to bind costs a nicer expiry message,
        which is never worth breaking the click that just succeeded.

        Read through `getattr`, because on discord.py 2.7.1 `View.message`
        does not merely hold None on a fresh view — the attribute does not
        exist at all until something assigns it, so touching it directly
        raises AttributeError and takes down the click it was meant to
        improve.
        """
        if getattr(self, "message", None) is not None:
            return
        try:
            self.message = await interaction.original_response()
        except discord.HTTPException as exc:
            log.debug("Could not bind panel message: %s", exc)

    async def on_timeout(self) -> None:
        """
        Expire visibly rather than leaving live-looking buttons behind.

        Without this, discord.py simply stops dispatching after the
        timeout: the buttons stay enabled and a click returns Discord's
        generic "This interaction failed", which is indistinguishable from
        a bug. Anything the owner had in flight is named by
        `expiry_hint` so the work is not orphaned.
        """
        for child in self.children:
            if isinstance(child, discord.ui.Button | discord.ui.Select):
                child.disabled = True

        note = "⏳ This panel expired — run `/league` to open a fresh one."
        if self.expiry_hint:
            note = f"{note}\n{self.expiry_hint}"

        if getattr(self, "message", None) is None:
            return
        try:
            await self.message.edit(content=note, view=self)
        except discord.HTTPException as exc:
            log.debug("Could not render panel expiry: %s", exc)

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        """
        Always say something, even for a failure we did not anticipate.

        Handlers defer before doing work, and after a deferral an uncaught
        exception produces no follow-up at all — the button looks like it
        did nothing and the owner cannot tell whether the write landed.
        Domain errors are handled at their call sites; this is the net
        under everything else.
        """
        log.exception(
            "Unhandled error in %s (%s)", type(self).__name__, type(item).__name__,
            exc_info=error,
        )
        await report_unexpected(interaction, error)


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


async def report_unexpected(
    interaction: discord.Interaction, error: Exception
) -> None:
    """
    The honest message for a failure with no domain meaning.

    It deliberately does not claim the write was rolled back. Every
    `db.connect()` is wrapped in a transaction, so a failure inside one
    cannot half-apply — but a failure *after* the commit (posting to a
    channel, assigning a role) leaves real state behind, and this handler
    cannot tell which happened. Saying "check" is true in both cases;
    saying "nothing was written" would not be.
    """
    await report_error(
        interaction,
        f"Something went wrong: `{type(error).__name__}`.\n"
        "This was not an expected failure, so the league may be in a "
        "partly-changed state — check the relevant panel or `/league` "
        "before retrying. The details are in the bot log.",
    )


class PanelModal(discord.ui.Modal):
    """
    A modal that always reports an unexpected failure.

    `discord.ui.Modal.on_error` defaults to logging to stderr and telling
    the user nothing, which is the same silent-failure problem the views
    had. Field-level validation still raises its own domain errors and is
    handled in `on_submit`.
    """

    async def on_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        log.exception(
            "Unhandled error in modal %s", type(self).__name__, exc_info=error
        )
        await report_unexpected(interaction, error)
