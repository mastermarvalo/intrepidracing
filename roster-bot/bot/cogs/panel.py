"""
`/league` — the guided front door.

The bot has 62 slash commands. That is the right number of commands for
what it does, but it is the wrong number of things to ask a commissioner
to remember at 9pm on a race night. Nothing here removes or renames any
of them; this is a navigation layer that sits on top and drives the same
code, so a server admin can run a whole race night without typing a
command, while power users keep every command they already know.

Three principles:

  * **Never a dead end.** Every screen says what to do next, and the
    button for it is on that screen.
  * **State-aware.** The panel reads the league's actual state and only
    offers actions that are currently valid — you cannot publish a run
    that does not exist.
  * **Same code as the commands.** Actions call `bot/workflow.py`, which
    the slash commands also call. There is no second implementation to
    drift out of sync.

Everything is ephemeral. A panel is private to whoever opened it, so two
commissioners can work at once without stepping on each other.
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from bot import workflow
from bot.panel_help import COMMAND_CATALOG, build_help_embed, help_category_options

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
        return "Seed league config (cap, min salary, movement caps) → **Setup**"
    if not status.has_drivers:
        return "Register drivers into your tiers → **Setup**"

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


class ImportModal(discord.ui.Modal, title="Import race results"):
    """
    The four things an import needs, in one dialog.

    The sheet URL is remembered per tier between rounds, so week two is
    three fields and week three is usually two.
    """

    def __init__(self, *, tier: str, parent: RaceNightView, remembered_sheet: str | None) -> None:
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

        self.parent.remember_sheet(self.tier, sheet_value)

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


class _OwnedView(discord.ui.View):
    """
    A view only its opener may press.

    Panels are ephemeral, but Discord still delivers component clicks
    from anyone who can somehow reach them; this keeps a second admin's
    stale panel from acting on the first one's session.
    """

    def __init__(self, *, opener_id: int, timeout: float | None = _PANEL_TIMEOUT_SECONDS) -> None:
        super().__init__(timeout=timeout)
        self.opener_id = opener_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.opener_id:
            await interaction.response.send_message(
                "That panel belongs to someone else. Run `/league` to open your own.",
                ephemeral=True,
            )
            return False
        return True


class PricePromptView(_OwnedView):
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


class PublishView(_OwnedView):
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


class RaceNightView(_OwnedView):
    """
    The weekly loop, in the order it actually happens.

    Sheet URLs are remembered per tier for the lifetime of the panel so
    repeat imports get shorter.
    """

    default_range = "A1:Z100"

    def __init__(self, *, status: workflow.LeagueStatus, opener_id: int) -> None:
        super().__init__(opener_id=opener_id)
        self.status = status
        self._sheets: dict[str, str] = {}

        for tier in status.tiers[:_MAX_TIER_BUTTONS]:
            self.add_item(_ImportTierButton(tier.code))
        self.add_item(_BackHomeButton())

    def remember_sheet(self, tier: str, sheet: str) -> None:
        self._sheets[tier] = sheet

    def remembered(self, tier: str) -> str | None:
        return self._sheets.get(tier)


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
        await interaction.response.send_modal(
            ImportModal(
                tier=self.tier_code,
                parent=view,
                remembered_sheet=view.remembered(self.tier_code),
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


# ── Setup ────────────────────────────────────────────────────────────


def build_setup_embed(status: workflow.LeagueStatus) -> discord.Embed:
    """
    A checklist, not a wall of commands.

    Each line is either done or is the next thing to do, with the exact
    command to run. Admins can follow it top to bottom on a fresh server.
    """
    steps = [
        (
            status.has_season,
            "Create and activate a season",
            "`/market-admin season create name:\"Season 7\" preset:f1`\n"
            "`/market-admin season activate name:\"Season 7\"`",
        ),
        (
            status.has_tiers,
            "Add your tiers",
            "`/market-admin tier add code:t1 name:\"Tier 1\" rank_order:1`",
        ),
        (
            status.has_config,
            "Check league config",
            "`/market-admin config show` · `/market-admin config edit`\n"
            "Salary cap, min salary, movement caps, offer expiry.",
        ),
        (
            status.has_drivers,
            "Register drivers into tiers",
            "Drivers enter the market when signed to a team roster.",
        ),
        (
            status.commissioner_role_id is not None,
            "Set the commissioner role",
            "`/market-admin config role`",
        ),
        (
            status.board_count > 0,
            "Post market boards",
            "`/market-admin board add kind:market channel:#market`",
        ),
    ]

    embed = discord.Embed(
        title="⚙ League setup",
        description="Work down the list. Anything already done is ticked.",
        color=_COLOR_INFO,
    )
    for done, title, detail in steps:
        mark = "✅" if done else "⬜"
        embed.add_field(name=f"{mark} {title}", value=detail, inline=False)
    embed.add_field(
        name="Google Sheets access",
        value="Results import needs a service account. See **Google Sheets access** in the "
        "bot README — it is the most common first-time blocker.",
        inline=False,
    )
    return embed


# ── Home ─────────────────────────────────────────────────────────────


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
            if status.setup_complete or status.has_tiers:
                self.add_item(_RaceNightButton())
            if status.pending_offers or status.pending_trades:
                self.add_item(_ApprovalsButton(status))
        self.add_item(_HelpButton())


class _SetupButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(label="Setup", style=discord.ButtonStyle.secondary, emoji="⚙")

    async def callback(self, interaction: discord.Interaction) -> None:
        status = await workflow.fetch_league_status(interaction.guild_id)
        view = _OwnedView(opener_id=interaction.user.id)
        view.add_item(_BackHomeButton())
        await interaction.response.edit_message(embed=build_setup_embed(status), view=view)


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
            view=RaceNightView(status=status, opener_id=interaction.user.id),
        )


class _ApprovalsButton(discord.ui.Button):
    def __init__(self, status: workflow.LeagueStatus) -> None:
        total = status.pending_offers + status.pending_trades
        super().__init__(
            label=f"Approvals ({total})",
            style=discord.ButtonStyle.danger,
            emoji="📋",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        status = await workflow.fetch_league_status(interaction.guild_id)
        embed = discord.Embed(
            title="📋 Awaiting approval",
            description=(
                f"**{status.pending_offers}** contract offer(s)\n"
                f"**{status.pending_trades}** trade(s)"
            ),
            color=_COLOR_WARN if (status.pending_offers or status.pending_trades) else _COLOR_OK,
        )
        embed.add_field(
            name="Approve or reject",
            value=(
                "`/market-admin approve offer_id:<id>`\n"
                "`/market-admin reject offer_id:<id> note:<why>`\n"
                "`/market-admin approve-trade trade_id:<id>`\n"
                "`/market-admin reject-trade trade_id:<id> note:<why>`"
            ),
            inline=False,
        )
        view = _OwnedView(opener_id=interaction.user.id)
        view.add_item(_BackHomeButton())
        await interaction.response.edit_message(embed=embed, view=view)


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
