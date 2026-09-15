"""
`/league` — the guided front door.

The bot has 84 slash commands. That is the right number of commands for
what it does, but it is the wrong number of things to ask a commissioner
to remember at 9pm on a race night. Nothing here removes or renames any
of them; this is a navigation layer that sits on top and drives the same
code, so a server admin can run a whole race night without typing a
command, while power users keep every command they already know.

The panel is organised as twelve screens, each reachable from this home
view: Setup, Race Night, Drivers, Approvals, Boards, Money, Off-season,
Market, Contracts, Trades, History and All commands. Setup, Money and
Off-season are commissioner-only; the three desks — Market, Contracts
and Trades — are open to everyone, because a Team Principal should not
need a commissioner to read the market or work their own roster.

Three principles:

  * **Never a dead end.** Every screen says what to do next, and the
    button for it is on that screen.
  * **State-aware.** The panel reads the league's actual state and only
    offers actions that are currently valid — you cannot publish a run
    that does not exist.
  * **Same code as the commands.** Actions call `bot/workflow.py`, which
    the slash commands also call. There is no second implementation to
    drift out of sync. Receipts are shared too: the import receipt is
    rendered by `bot/ui/receipts.py` for both surfaces, so the panel
    cannot quietly omit money lines the command reports.

Everything is ephemeral. A panel is private to whoever opened it, so two
commissioners can work at once without stepping on each other.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, queries, workflow
from bot.panel_help import COMMAND_CATALOG, build_help_embed, help_category_options
from bot.ui import base, receipts
from bot.ui.approvals_screen import open_approvals
from bot.ui.boards_screen import open_boards
from bot.ui.contracts_screen import open_contracts
from bot.ui.drivers_screen import open_drivers
from bot.ui.history_screen import open_history
from bot.ui.market_screen import open_market
from bot.ui.money_screen import open_money
from bot.ui.offseason_screen import open_offseason
from bot.ui.setup_screen import open_setup
from bot.ui.trades_screen import open_trades

log = logging.getLogger(__name__)

# Discord platform limits, not business rules.
_EMBED_FIELD_LIMIT = 1024
_MAX_TIER_BUTTONS = 4
_PANEL_TIMEOUT_SECONDS = 600

_COLOR_OK = discord.Color.from_str("#2e7d32")
_COLOR_INFO = discord.Color.from_str("#1a3d6d")
_COLOR_WARN = discord.Color.from_str("#c0392b")


def _is_admin(interaction: discord.Interaction) -> bool:
    """Manage Server, mirroring cogs/roster.py and cogs/admin_market.py."""
    perms = getattr(interaction.user, "guild_permissions", None)
    return bool(perms and perms.manage_guild)


def _truncate_field(text: str) -> str:
    if len(text) <= _EMBED_FIELD_LIMIT:
        return text
    return text[: _EMBED_FIELD_LIMIT - 1] + "…"


# ── Status rendering ─────────────────────────────────────────────────


def build_status_embed(status: workflow.LeagueStatus, *, is_admin: bool) -> discord.Embed:
    """
    The home screen. Answers 'what is set up' and 'what should I do next'
    before offering any buttons.
    """
    if not status.has_season:
        embed = discord.Embed(
            title="🏁 League Control",
            description=(
                "No season is active yet, so the market is not running.\n\n"
                "**Start here:** press **Setup** below and the panel will walk "
                "you through it."
            ),
            color=_COLOR_WARN,
        )
        embed.set_footer(text="Nothing is configured yet")
        return embed

    embed = discord.Embed(
        title="🏁 League Control",
        description=f"Active season: **{status.season_name}**",
        color=_COLOR_OK if status.setup_complete else _COLOR_INFO,
    )

    if status.tiers:
        lines = []
        for t in status.tiers:
            bits = [f"**{t.code}** · {t.driver_count} driver(s)"]
            if t.latest_round_label:
                bits.append(f"last import: {t.latest_round_label}")
            else:
                bits.append("no results imported")
            if t.unpublished_run_id is not None:
                bits.append(f"⏳ run #{t.unpublished_run_id} unpublished")
            elif not t.has_published_valuation:
                bits.append("no published market yet")
            lines.append(" · ".join(bits))
        embed.add_field(name="Tiers", value=_truncate_field("\n".join(lines)), inline=False)
    else:
        embed.add_field(
            name="Tiers",
            value="None yet — add at least one before importing results.",
            inline=False,
        )

    if is_admin and (status.pending_offers or status.pending_trades):
        embed.add_field(
            name="⚠ Waiting on you",
            value=(
                f"{status.pending_offers} contract offer(s) and "
                f"{status.pending_trades} trade(s) awaiting approval."
            ),
            inline=False,
        )

    embed.add_field(name="Next step", value=_next_step_text(status), inline=False)
    embed.set_footer(text="Every action here has an equivalent slash command · /help")
    return embed


def _next_step_text(status: workflow.LeagueStatus) -> str:
    """
    One concrete instruction, never a menu of possibilities.

    Ordered by what actually blocks progress, so a half-built league is
    always told the single thing standing between it and a live market.
    """
    if not status.has_season:
        return "Create and activate a season → **Setup**"
    if not status.has_tiers:
        return "Add at least one tier → **Setup**"
    if not status.has_config:
        return "Set the cap, minimum salary and movement caps → **Setup**"
    if not status.has_drivers:
        return "Enrol drivers → **Drivers**"

    unpublished = [t for t in status.tiers if t.unpublished_run_id is not None]
    if unpublished:
        t = unpublished[0]
        return (
            f"Run #{t.unpublished_run_id} for **{t.code}** is priced but not published. "
            "Review and publish it → **Race Night**"
        )

    no_market = [t for t in status.tiers if not t.has_published_valuation]
    if no_market:
        codes = ", ".join(t.code for t in no_market)
        return (
            f"No published market yet for: {codes}. Run a baseline valuation "
            "→ **Race Night**"
        )

    if status.pending_offers or status.pending_trades:
        return "Clear the approvals queue → **Approvals**"

    if status.board_count == 0:
        return "Post a self-updating market board in a public channel → **Boards**"

    return "You are up to date. After the next race → **Race Night**"


# ── Race night ───────────────────────────────────────────────────────


class ImportModal(base.PanelModal, title="Import race results"):
    """
    The four things an import needs, in one dialog.

    The sheet URL is remembered per tier between rounds, so week two is
    three fields and week three is usually two.
    """

    def __init__(
        self,
        *,
        tier: str,
        parent: RaceNightView,
        remembered_sheet: str | None,
        remembered_tab: str | None = None,
    ) -> None:
        super().__init__()
        self.tier = tier
        self.parent = parent

        self.round_label = discord.ui.TextInput(
            label="Round label",
            placeholder="R14 Abu Dhabi",
            max_length=100,
        )
        self.sheet = discord.ui.TextInput(
            label="Google Sheet URL",
            placeholder="https://docs.google.com/spreadsheets/d/...",
            default=remembered_sheet or None,
        )
        self.tab = discord.ui.TextInput(
            label="Tab / range",
            placeholder="R14 Abu Dhabi!A1:I30",
            default=remembered_tab or None,
            required=False,
        )
        self.held_on = discord.ui.TextInput(
            label="Race date (optional)",
            placeholder="YYYY-MM-DD",
            required=False,
            max_length=10,
        )
        for item in (self.round_label, self.sheet, self.tab, self.held_on):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        sheet_value = str(self.sheet.value).strip()
        tab_value = str(self.tab.value).strip() or self.parent.default_range

        try:
            outcome = await workflow.import_round(
                guild_id=interaction.guild_id,
                user_id=interaction.user.id,
                tier=self.tier,
                round_label=str(self.round_label.value).strip(),
                sheet=sheet_value,
                sheet_range=tab_value,
                held_on=str(self.held_on.value).strip() or None,
            )
        except workflow.ImportAborted as exc:
            await interaction.followup.send(
                embed=_import_error_embed(exc), ephemeral=True
            )
            return
        except workflow.WorkflowError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return

        # G23: remember the sheet in the database, not just on this
        # view. The view dies after 600s and on every bot restart, so
        # the commissioner used to re-paste the same long Sheets URL
        # before every single race.
        self.parent.remember_sheet(self.tier, sheet_value, tab_value)
        if self.parent.status.season_id is not None:
            try:
                async with db.connect() as conn:
                    await queries.remember_results_sheet(
                        conn,
                        season_id=self.parent.status.season_id,
                        tier_code=self.tier,
                        sheet_url=sheet_value,
                        sheet_range=tab_value,
                    )
            except Exception:  # pragma: no cover - convenience only
                # The import already succeeded and is committed. Losing
                # the prefill is a small annoyance; turning it into a
                # failure message would imply the results were lost.
                log.warning("could not remember results sheet", exc_info=True)

        embed = discord.Embed(
            title="✅ Results imported",
            description=(
                f"**{outcome.written}** result(s) for `{outcome.tier_code}` — "
                f"**{outcome.round_label}** (round {outcome.round_order})."
            ),
            color=_COLOR_OK,
        )
        if outcome.missing_drivers:
            embed.add_field(
                name=f"⚠ No row for {len(outcome.missing_drivers)} roster driver(s)",
                value=_truncate_field(
                    ", ".join(outcome.missing_drivers)
                    + "\nThey will score nothing for this round."
                ),
                inline=False,
            )
        # G9: the panel receipt used to stop at the result count, so an
        # admin importing here never learned that budgets had been
        # credited or that salary had just left every team's cash. Same
        # renderer as the slash-command receipt, so the two cannot drift.
        money_lines = receipts.render_money_outcome(outcome)
        if money_lines:
            embed.add_field(
                name="Money",
                value=_truncate_field("\n".join(money_lines)),
                inline=False,
            )
        embed.add_field(
            name="Next step",
            value="Press **Price this round** to see what it does to the market. Nothing is "
            "published until you say so.",
            inline=False,
        )
        view = PricePromptView(
            tier=self.tier,
            round_label=outcome.round_label,
            opener_id=interaction.user.id,
        )
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


def _import_error_embed(exc: workflow.ImportAborted) -> discord.Embed:
    embed = discord.Embed(
        title="❌ Import aborted — nothing was written",
        description=(
            "Every problem is listed below with its spreadsheet row. Fix them and "
            "import again; re-importing the same round label is safe."
        ),
        color=_COLOR_WARN,
    )
    numbered = "\n".join(f"{i}. {e}" for i, e in enumerate(exc.errors, start=1))
    embed.add_field(name="Problems", value=_truncate_field(numbered), inline=False)
    return embed


class _OwnedView(base.OwnedView):
    """
    A view only its opener may press.

    Panels are ephemeral, but Discord still delivers component clicks
    from anyone who can somehow reach them; this keeps a second admin's
    stale panel from acting on the first one's session.

    Kept as a named subclass of the shared `base.OwnedView` so the home
    and help screens — which non-admins are meant to reach — inherit the
    timeout and error handling without also inheriting the Manage Server
    gate. Admin race-night views extend `base.AdminOwnedView` instead, so
    a permission removed mid-session takes effect on the next click
    rather than at the end of the 10-minute window.
    """


class PricePromptView(base.AdminOwnedView):
    """Shown straight after an import: the obvious next action, pre-filled."""

    def __init__(self, *, tier: str, round_label: str, opener_id: int) -> None:
        super().__init__(opener_id=opener_id)
        self.tier = tier
        self.round_label = round_label

    @discord.ui.button(label="Price this round", style=discord.ButtonStyle.primary, emoji="💰")
    async def price(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await _do_run_valuation(
            interaction,
            tier=self.tier,
            round_label=self.round_label,
            opener_id=self.opener_id,
        )


async def _do_run_valuation(
    interaction: discord.Interaction,
    *,
    tier: str,
    round_label: str,
    opener_id: int,
) -> None:
    try:
        result = await workflow.run_valuation(
            guild_id=interaction.guild_id,
            user_id=interaction.user.id,
            tier=tier,
            round_label=round_label,
        )
    except workflow.WorkflowError as exc:
        await interaction.followup.send(f"❌ {exc}", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"💰 Dry run #{result.run_id} — {result.tier_code} · {result.round_label}",
        description=(
            "**Nothing has been published.** These are the values that *would* go live."
            if result.priced_round
            else "⚠ No imported results under that round label, so this is a **baseline** "
            "run — no driver moves. That is expected before a season's first race."
        ),
        color=_COLOR_INFO,
    )

    lines = []
    for v in result.outcomes:
        arrow = "▲" if v.delta > 0 else ("▼" if v.delta < 0 else "—")
        flag = " ⚠capped" if v.capped else ""
        lines.append(
            f"`{v.rank_in_tier:>2}` **{v.display_name}** "
            f"{v.previous_value:.2f} {arrow} {v.market_value:.2f} "
            f"({v.delta:+.2f}){flag}"
        )
    embed.add_field(
        name="Proposed values", value=_truncate_field("\n".join(lines) or "—"), inline=False
    )
    embed.set_footer(text="Publishing writes these values and moves the market.")

    view = PublishView(
        run_id=result.run_id,
        tier=result.tier_code,
        round_label=result.round_label,
        opener_id=opener_id,
    )
    await interaction.followup.send(embed=embed, view=view, ephemeral=True)


class PublishView(base.AdminOwnedView):
    """
    Publish is the only irreversible step in a race night, so it gets a
    confirm rather than firing on the first click.
    """

    def __init__(self, *, run_id: int, tier: str, round_label: str, opener_id: int) -> None:
        super().__init__(opener_id=opener_id)
        self.run_id = run_id
        self.tier = tier
        self.round_label = round_label
        self._armed = False

    @discord.ui.button(label="Publish", style=discord.ButtonStyle.success, emoji="📣")
    async def publish(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        if not self._armed:
            self._armed = True
            button.label = "Confirm publish"
            button.style = discord.ButtonStyle.danger
            await interaction.response.edit_message(view=self)
            await interaction.followup.send(
                f"Press again to publish run #{self.run_id}. This updates every driver "
                f"value in `{self.tier}` and posts to your market channels.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            outcome = await workflow.publish_valuation(
                interaction.client, guild_id=interaction.guild_id, run_id=self.run_id
            )
        except workflow.WorkflowError as exc:
            await interaction.followup.send(f"\u274c {exc}", ephemeral=True)
            return

        if outcome.already_published:
            await interaction.followup.send(
                f"Run #{self.run_id} was already published \u2014 nothing changed.",
                ephemeral=True,
            )
        else:
            note = (
                ""
                if outcome.boards_refreshed
                else "\n\u26a0 Board refresh failed; boards will catch up on the next poll."
            )
            await interaction.followup.send(
                f"\u2705 Published run #{self.run_id} \u2014 `{self.tier}` values for "
                f"**{self.round_label}** are live." + note,
                ephemeral=True,
            )

        # Disable the view either way: the run is no longer publishable,
        # so leaving a live button invites a confusing second click.
        for child in self.children:
            child.disabled = True
        self.stop()
        try:
            await interaction.edit_original_response(view=self)
        except discord.HTTPException:
            pass

    @discord.ui.button(label="Discard", style=discord.ButtonStyle.secondary)
    async def discard(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(
            content=(
                f"Left run #{self.run_id} unpublished. It stays available — "
                f"`/market-admin valuation preview run_id: {self.run_id}`."
            ),
            embed=None,
            view=None,
        )


class RaceNightView(base.AdminOwnedView):
    """
    The weekly loop, in the order it actually happens.

    Sheet URLs are remembered per tier for the lifetime of the panel so
    repeat imports get shorter.
    """

    default_range = "A1:Z100"

    def __init__(
        self,
        *,
        status: workflow.LeagueStatus,
        opener_id: int,
        remembered: dict[str, tuple[str, str]] | None = None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.status = status
        self._sheets: dict[str, tuple[str, str]] = dict(remembered or {})

        for tier in status.tiers[:_MAX_TIER_BUTTONS]:
            self.add_item(_ImportTierButton(tier.code))
        self.add_item(_BrowseHistoryButton())
        self.add_item(_BackHomeButton())

    def remember_sheet(self, tier: str, sheet: str, tab: str) -> None:
        self._sheets[tier] = (sheet, tab)

    def remembered(self, tier: str) -> tuple[str | None, str | None]:
        return self._sheets.get(tier, (None, None))

    @classmethod
    async def load(
        cls, *, status: workflow.LeagueStatus, opener_id: int
    ) -> RaceNightView:
        """
        Build the view with each tier's last-used sheet already filled in.

        A classmethod because the lookup is a database read and a Discord
        view constructor cannot await.
        """
        remembered: dict[str, tuple[str, str]] = {}
        if status.season_id is not None:
            try:
                async with db.connect() as conn:
                    for tier in status.tiers[:_MAX_TIER_BUTTONS]:
                        url, rng = await queries.fetch_remembered_results_sheet(
                            conn,
                            season_id=status.season_id,
                            tier_code=tier.code,
                        )
                        if url:
                            remembered[tier.code] = (url, rng or "")
            except Exception:  # pragma: no cover - convenience only
                log.warning(
                    "could not load remembered sheets", exc_info=True
                )
        return cls(
            status=status, opener_id=opener_id, remembered=remembered
        )


class _ImportTierButton(discord.ui.Button):
    def __init__(self, tier_code: str) -> None:
        super().__init__(
            label=f"Import {tier_code}",
            style=discord.ButtonStyle.primary,
            emoji="📥",
        )
        self.tier_code = tier_code

    async def callback(self, interaction: discord.Interaction) -> None:
        view: RaceNightView = self.view  # type: ignore[assignment]
        sheet, tab = view.remembered(self.tier_code)
        await interaction.response.send_modal(
            ImportModal(
                tier=self.tier_code,
                parent=view,
                remembered_sheet=sheet,
                remembered_tab=tab,
            )
        )


class _BackHomeButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Back", style=discord.ButtonStyle.secondary, emoji="◀")

    async def callback(self, interaction: discord.Interaction) -> None:
        status = await workflow.fetch_league_status(interaction.guild_id)
        is_admin = _is_admin(interaction)
        await interaction.response.edit_message(
            embed=build_status_embed(status, is_admin=is_admin),
            view=HomeView(
                status=status, opener_id=interaction.user.id, is_admin=is_admin
            ),
        )


class _BrowseHistoryButton(discord.ui.Button):
    """Race-Night-adjacent sub-panel for inspecting past runs and rounds."""

    def __init__(self) -> None:
        super().__init__(
            label="Browse history",
            style=discord.ButtonStyle.secondary,
            emoji="📜",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_history(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


async def _back_to_home(interaction: discord.Interaction) -> None:
    """
    Re-render the home panel from a fresh status read.

    Passed into every screen as its "go back" behaviour so the screens
    never import this module, which would be circular.
    """
    status = await workflow.fetch_league_status(interaction.guild_id)
    is_admin = _is_admin(interaction)
    embed = build_status_embed(status, is_admin=is_admin)
    view = HomeView(status=status, opener_id=interaction.user.id, is_admin=is_admin)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


class HomeView(_OwnedView):
    """Role-aware routing. A driver never sees a commissioner's buttons."""

    def __init__(
        self, *, status: workflow.LeagueStatus, opener_id: int, is_admin: bool
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.status = status
        self.is_admin = is_admin

        if is_admin:
            self.add_item(_SetupButton())
            if status.has_tiers:
                self.add_item(_RaceNightButton())
            if status.has_tiers:
                self.add_item(_DriversButton(status))
            # Shown even when empty: "nothing is waiting on you" is a
            # useful answer, and a button that appears and disappears is
            # harder to learn than one that is always in the same place.
            if status.has_season:
                self.add_item(_ApprovalsButton(status))
            if status.has_tiers:
                self.add_item(_BoardsButton(status))
            if status.has_season:
                self.add_item(_MoneyButton())
                self.add_item(_OffseasonButton())

        # The three desks are for everyone. A Team Principal needs to
        # read the market, manage their own contracts and propose trades
        # without a commissioner opening a screen for them, and a driver
        # needs to see their own deal. Each screen gates its own actions
        # by role, so showing the door to everybody is safe.
        if status.has_tiers:
            self.add_item(_MarketButton())
            self.add_item(_ContractsButton())
            self.add_item(_TradesButton())

        self.add_item(_HelpButton())


class _SetupButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Setup", style=discord.ButtonStyle.secondary, emoji="⚙")

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_setup(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _RaceNightButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Race Night", style=discord.ButtonStyle.primary, emoji="🏁")

    async def callback(self, interaction: discord.Interaction) -> None:
        status = await workflow.fetch_league_status(interaction.guild_id)
        embed = discord.Embed(
            title="🏁 Race night",
            description=(
                "**1.** Import results from your sheet\n"
                "**2.** Review the priced dry run\n"
                "**3.** Publish when it looks right\n\n"
                "Nothing moves the market until step 3."
            ),
            color=_COLOR_INFO,
        )
        for t in status.tiers[:_MAX_TIER_BUTTONS]:
            last = t.latest_round_label or "nothing imported yet"
            pending = (
                f" · ⏳ run #{t.unpublished_run_id} awaiting publish"
                if t.unpublished_run_id is not None
                else ""
            )
            embed.add_field(name=t.code, value=f"Last: {last}{pending}", inline=True)
        if len(status.tiers) > _MAX_TIER_BUTTONS:
            embed.set_footer(
                text=f"Showing {_MAX_TIER_BUTTONS} of {len(status.tiers)} tiers · "
                "use /market-admin results import for the rest"
            )
        await interaction.response.edit_message(
            embed=embed,
            view=await RaceNightView.load(
                status=status, opener_id=interaction.user.id
            ),
        )


class _DriversButton(discord.ui.Button):
    """
    Reachable from home once tiers exist.

    Label carries the enrolled-driver count so an admin can see whether
    a sync is due without opening anything, matching the pattern used by
    Approvals and Boards.
    """

    def __init__(self, status: workflow.LeagueStatus) -> None:
        total = sum(t.driver_count for t in status.tiers)
        label = f"Drivers ({total})" if total else "Drivers (none yet)"
        super().__init__(
            label=label,
            style=(
                discord.ButtonStyle.secondary
                if total
                else discord.ButtonStyle.primary
            ),
            emoji="👥",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_drivers(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _ApprovalsButton(discord.ui.Button):
    """
    Opens the live queue. The count is baked into the label so an admin
    can see there is work waiting without opening anything.
    """

    def __init__(self, status: workflow.LeagueStatus) -> None:
        total = status.pending_offers + status.pending_trades
        super().__init__(
            label=f"Approvals ({total})",
            style=discord.ButtonStyle.danger,
            emoji="📋",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_approvals(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _BoardsButton(discord.ui.Button):
    """
    Boards are reachable from home as well as from Setup.

    The home panel's "next step" line can point here once a league is
    otherwise configured but has no public market board, and a button it
    names has to exist.
    """

    def __init__(self, status: workflow.LeagueStatus) -> None:
        super().__init__(
            label=(
                "Boards" if status.board_count else "Boards (none yet)"
            ),
            style=(
                discord.ButtonStyle.secondary
                if status.board_count
                else discord.ButtonStyle.primary
            ),
            emoji="📊",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_boards(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _MoneyButton(discord.ui.Button):
    """Budgets, cash, escrow and the ledger — the commissioner's books."""

    def __init__(self) -> None:
        super().__init__(
            label="Money",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4b0",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_money(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _OffseasonButton(discord.ui.Button):
    """Carry-over, rollover, promotion and relegation."""

    def __init__(self) -> None:
        super().__init__(
            label="Off-season",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f504",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_offseason(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _MarketButton(discord.ui.Button):
    """Driver values and the tier market — readable by anyone."""

    def __init__(self) -> None:
        super().__init__(
            label="Market",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4c8",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_market(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _ContractsButton(discord.ui.Button):
    """Offers, signings, releases and buyouts."""

    def __init__(self) -> None:
        super().__init__(
            label="Contracts",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f4dd",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_contracts(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _TradesButton(discord.ui.Button):
    """Propose, review and approve driver trades."""

    def __init__(self) -> None:
        super().__init__(
            label="Trades",
            style=discord.ButtonStyle.secondary,
            emoji="\U0001f501",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await open_trades(
            interaction, opener_id=interaction.user.id, on_back=_back_to_home
        )


class _HelpButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="All commands", style=discord.ButtonStyle.secondary, emoji="📖")

    async def callback(self, interaction: discord.Interaction) -> None:
        tree = getattr(interaction.client, "tree", None)
        view = HelpView(opener_id=interaction.user.id, tree=tree, with_back=True)
        await interaction.response.edit_message(embed=build_help_embed(None, tree), view=view)


# ── Help browser ─────────────────────────────────────────────────────


class _HelpSelect(discord.ui.Select):
    def __init__(self, tree: app_commands.CommandTree | None) -> None:
        super().__init__(
            placeholder="Pick a command group…",
            options=help_category_options(tree),
        )
        self.tree = tree

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=build_help_embed(self.values[0], self.tree), view=self.view
        )


class HelpView(_OwnedView):
    def __init__(
        self,
        *,
        opener_id: int,
        tree: app_commands.CommandTree | None = None,
        with_back: bool = False,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.add_item(_HelpSelect(tree))
        if with_back:
            self.add_item(_BackHomeButton())


# ── Cog ──────────────────────────────────────────────────────────────


class PanelCog(commands.Cog):
    """Adds `/league` and `/help`. Adds no new capability of its own."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(
        name="league",
        description="Guided control panel — setup, race night, approvals",
    )
    async def league(self, interaction: discord.Interaction) -> None:
        if interaction.guild_id is None:
            await interaction.response.send_message(
                "Run this in a server, not a DM.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        status = await workflow.fetch_league_status(interaction.guild_id)
        is_admin = _is_admin(interaction)
        await interaction.followup.send(
            embed=build_status_embed(status, is_admin=is_admin),
            view=HomeView(status=status, opener_id=interaction.user.id, is_admin=is_admin),
            ephemeral=True,
        )

    @app_commands.command(name="help", description="Browse every command by group")
    @app_commands.describe(group="Jump straight to one group")
    async def help_command(
        self, interaction: discord.Interaction, group: str | None = None
    ) -> None:
        key = group.strip().lstrip("/").lower() if group else None
        if key is not None and key not in COMMAND_CATALOG:
            key = None
        tree = getattr(interaction.client, "tree", None)
        await interaction.response.send_message(
            embed=build_help_embed(key, tree),
            view=HelpView(opener_id=interaction.user.id, tree=tree),
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelCog(bot))
