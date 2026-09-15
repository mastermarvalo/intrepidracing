"""
Race Night → Browse history sub-panel.

Wraps `/market-admin valuation list|preview` and `/market-admin results
list|show`. The screen has two branches:

  * **Valuation runs** — pick a run to re-render (works for both
    dry-run and published runs; a preview is read-only).
  * **Race rounds** — pick a round to see its raw results + the
    normalized observations the valuation engine would compute for it.

Every read goes through `bot/workflow.py` (CLAUDE.md §8). The renderers
here are pure functions of the workflow's return types, so they are
testable without Discord.
"""

from __future__ import annotations

import discord

from bot import workflow
from bot.market.money import format_money, format_pl
from bot.ui.base import (
    COLOR_INFO,
    COLOR_OK,
    SELECT_MAX_OPTIONS,
    AdminOwnedView,
    BackButton,
    BackCallback,
    report_error,
    truncate_field,
)

# Discord platform limits.
_DESCRIPTION_LIMIT = 4096

# A select holds 25 options, and neither browser may drop the rest of
# the list silently: both page, and both keep their tier filter.
ITEMS_PER_PAGE = SELECT_MAX_OPTIONS



def page_count(total: int, *, per_page: int = ITEMS_PER_PAGE) -> int:
    """Number of pages needed for `total` items (at least one)."""
    if total <= 0:
        return 1
    return -(-total // per_page)


def page_slice(items: list, page: int, *, per_page: int = ITEMS_PER_PAGE):
    """`(visible, clamped_page, pages)` for a paged list."""
    pages = page_count(len(items), per_page=per_page)
    page = min(max(page, 0), pages - 1)
    start = page * per_page
    return items[start : start + per_page], page, pages


def _page_field(embed: discord.Embed, *, shown: int, page: int, pages: int,
                total: int, noun: str) -> None:
    if pages <= 1:
        return
    start = page * ITEMS_PER_PAGE + 1
    embed.add_field(
        name="Page",
        value=(
            f"Showing {noun} {start}–{start + shown - 1} of {total} "
            f"(page {page + 1} of {pages}). Narrow it with the tier filter, "
            "or use the page buttons."
        ),
        inline=False,
    )


# ── landing ──────────────────────────────────────────────────────────


def build_history_embed() -> discord.Embed:
    return discord.Embed(
        title="📜 History",
        description=(
            "Browse past valuation runs and imported race rounds.\n\n"
            "**Valuation runs** — re-render any dry-run or published run.\n"
            "**Race rounds** — inspect the raw results and the normalized "
            "observations the engine derived from them."
        ),
        color=COLOR_INFO,
    )


class HistoryView(AdminOwnedView):
    def __init__(self, *, opener_id: int, on_back: BackCallback) -> None:
        super().__init__(opener_id=opener_id)
        self._on_back = on_back
        self.add_item(_ValuationsButton())
        self.add_item(_RoundsButton())
        self.add_item(BackButton(on_back))

    async def reload(self, interaction: discord.Interaction) -> None:
        view = HistoryView(opener_id=self.opener_id, on_back=self._on_back)
        embed = build_history_embed()
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await view.bind_message(interaction)


class _ValuationsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Valuation runs",
            style=discord.ButtonStyle.primary,
            emoji="💰",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, HistoryView)
        await _open_valuations(
            interaction,
            opener_id=view.opener_id,
            parent=view,
            tier_filter=None,
        )


class _RoundsButton(discord.ui.Button):
    def __init__(self) -> None:
        super().__init__(
            label="Race rounds",
            style=discord.ButtonStyle.primary,
            emoji="🏁",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, HistoryView)
        await _open_rounds(
            interaction,
            opener_id=view.opener_id,
            parent=view,
            tier_filter=None,
        )


# ── valuation-runs browser ───────────────────────────────────────────


def build_valuation_list_embed(
    runs: list[workflow.ValuationRunSummary],
    *,
    tier_filter: str | None,
    page: int = 0,
) -> discord.Embed:
    scope = f"tier `{tier_filter}`" if tier_filter else "every tier"
    if not runs:
        return discord.Embed(
            title="💰 Valuation runs",
            description=(
                f"No runs recorded for {scope} yet.\n\n"
                "Run a dry-run with **Race Night → Price this round**, "
                "or `/market-admin valuation run`."
            ),
            color=COLOR_INFO,
        )
    visible, page, pages = page_slice(runs, page)
    lines = []
    for r in visible:
        state = "🟢 published" if r.published else "⚪ dry-run"
        lines.append(
            f"`#{r.run_id}` · `{r.tier_code}` · **{r.round_label}** · "
            f"{state} · {r.created_at:%Y-%m-%d %H:%M}"
        )
    embed = discord.Embed(
        title=f"💰 Valuation runs — {scope}",
        description=truncate_field("\n".join(lines), _DESCRIPTION_LIMIT),
        color=COLOR_INFO,
    )
    _page_field(
        embed, shown=len(visible), page=page, pages=pages,
        total=len(runs), noun="runs",
    )
    return embed


class _ValuationBrowserView(AdminOwnedView):
    def __init__(
        self,
        *,
        opener_id: int,
        parent: HistoryView,
        tier_choices: list[tuple[str, str]],
        runs: list[workflow.ValuationRunSummary],
        tier_filter: str | None,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_filter = tier_filter

        visible, self.page, pages = page_slice(runs, page)
        if tier_choices:
            self.add_item(
                _TierFilterSelect(
                    self, tier_choices, kind="valuation", current=tier_filter
                )
            )
        if visible:
            self.add_item(_ValuationRunSelect(self, visible))
        self.add_item(BackButton(self._back))
        if pages > 1:
            self.add_item(
                _HistoryPageButton(
                    kind="valuation", delta=-1, enabled=self.page > 0
                )
            )
            self.add_item(
                _HistoryPageButton(
                    kind="valuation", delta=1, enabled=self.page + 1 < pages
                )
            )

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _ValuationRunSelect(discord.ui.Select):
    def __init__(
        self, parent: _ValuationBrowserView, runs: list[workflow.ValuationRunSummary]
    ) -> None:
        super().__init__(
            placeholder="Pick a run to re-render…",
            options=[
                discord.SelectOption(
                    label=f"#{r.run_id} · {r.tier_code} · {r.round_label}"[:100],
                    value=str(r.run_id),
                    description=(
                        ("🟢 published · " if r.published else "⚪ dry-run · ")
                        + f"{r.created_at:%Y-%m-%d}"
                    )[:100],
                )
                for r in runs
            ],
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        run_id = int(self.values[0])
        try:
            preview = await workflow.preview_valuation(
                interaction.guild_id, run_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        embed = build_valuation_preview_embed(preview)
        view = _ValuationPreviewView(
            opener_id=self._owner.opener_id,
            parent=self._owner,
            preview=preview,
        )
        await interaction.edit_original_response(embed=embed, view=view)
        await view.bind_message(interaction)


class _ValuationPreviewView(AdminOwnedView):
    """
    A stored run, re-rendered.

    An unpublished run reached from here used to be a dead end: no
    button, and no mention of the one command that could finish the job
    (G19). It now carries the same two-click publish the race-night
    `PublishView` uses, and the embed names the run id and the typed
    command so an expired panel is still recoverable.
    """

    def __init__(
        self,
        *,
        opener_id: int,
        parent: _ValuationBrowserView,
        preview: workflow.ValuationRunPreview | None = None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.preview = preview
        if preview is not None and not preview.published:
            self.add_item(_PublishRunButton(preview.run_id))
            self.expiry_hint = publish_fallback_line(preview.run_id)
        self.add_item(BackButton(self._back, label="Back to runs"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await _open_valuations(
            interaction,
            opener_id=self.opener_id,
            parent=self.parent.parent,
            tier_filter=self.parent.tier_filter,
            page=self.parent.page,
        )

    async def refresh(self, interaction: discord.Interaction) -> None:
        """Re-read the run and re-render, after publishing it."""
        assert self.preview is not None
        try:
            preview = await workflow.preview_valuation(
                interaction.guild_id, self.preview.run_id
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        view = _ValuationPreviewView(
            opener_id=self.opener_id, parent=self.parent, preview=preview
        )
        embed = build_valuation_preview_embed(preview)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=view)
        else:
            await interaction.response.edit_message(embed=embed, view=view)
        await view.bind_message(interaction)


def publish_fallback_line(run_id: int) -> str:
    """The typed route to the same outcome, named wherever publish is."""
    return (
        f"Typed equivalent: `/market-admin valuation publish run_id: {run_id}`"
    )


def build_publish_confirm_note(preview: workflow.ValuationRunPreview) -> str:
    """The consequence, restated before the second click."""
    return (
        f"Press **Confirm publish** to publish run #{preview.run_id}. This "
        f"writes every driver value in `{preview.tier_code}` for "
        f"**{preview.round_label}** and refreshes the public market boards. "
        "**It cannot be undone** — a mistake has to be corrected with "
        "another run.\n"
        "It does not touch contracts, budgets or roles, and it does not "
        "publish any other run.\n"
        f"{publish_fallback_line(preview.run_id)}"
    )


def build_published_note(outcome: workflow.PublishOutcome, *, tier_code: str) -> str:
    if outcome.already_published:
        return (
            f"Run #{outcome.run_id} was already published — nothing changed."
        )
    boards = (
        "The market boards were refreshed."
        if outcome.boards_refreshed
        else (
            "⚠ The board refresh failed, so the public boards may still show "
            "the old numbers until the 15-minute poll catches up."
        )
    )
    return (
        f"✅ Published run #{outcome.run_id} — `{tier_code}` values are live. "
        f"{boards}\n"
        "No contract, budget or Discord role changed, and no other run was "
        "published."
    )


class _PublishRunButton(discord.ui.Button):
    """
    Publish an unpublished run, with the race-night confirm step.

    Publishing is irreversible, so the first click only arms the button
    and restates what the second one will do.
    """

    def __init__(self, run_id: int) -> None:
        super().__init__(
            label="Publish this run",
            style=discord.ButtonStyle.success,
            emoji="📣",
        )
        self.run_id = run_id
        self._armed = False

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ValuationPreviewView)
        assert view.preview is not None

        if not self._armed:
            self._armed = True
            self.label = "Confirm publish"
            self.style = discord.ButtonStyle.danger
            await interaction.response.edit_message(view=view)
            await interaction.followup.send(
                build_publish_confirm_note(view.preview), ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        try:
            outcome = await workflow.publish_valuation(
                interaction.client,
                guild_id=interaction.guild_id,
                run_id=self.run_id,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return

        note = build_published_note(outcome, tier_code=view.preview.tier_code)
        await view.refresh(interaction)
        await interaction.followup.send(note, ephemeral=True)


def build_valuation_preview_embed(
    preview: workflow.ValuationRunPreview,
) -> discord.Embed:
    state = "🟢 PUBLISHED" if preview.published else "⚪ DRY-RUN"
    lines: list[str] = []
    for row in preview.rows:
        arrow = _delta_arrow(row["delta"])
        cap = "  ⚠capped" if row["capped"] else ""
        lines.append(
            f"`{row['rank_in_tier']:>2}` **{row['display_name']}** "
            f"{format_money(row['market_value'])}  {arrow} "
            f"{format_pl(row['delta'])}{cap}"
        )
    body = "\n".join(lines) if lines else "_(no driver rows in this run)_"
    if preview.published:
        header = f"{state} · run `#{preview.run_id}`"
    else:
        header = (
            f"{state} · run `#{preview.run_id}` — **nothing is live yet.**\n"
            "Press **Publish this run** below to make these values the "
            "market, or use the typed route:\n"
            f"`/market-admin valuation publish run_id: {preview.run_id}`"
        )
    return discord.Embed(
        title=(
            f"💰 Run #{preview.run_id} — `{preview.tier_code}` · "
            f"{preview.round_label}"
        ),
        description=truncate_field(f"{header}\n\n{body}", _DESCRIPTION_LIMIT),
        color=COLOR_OK if preview.published else COLOR_INFO,
    )


def _delta_arrow(delta) -> str:
    if delta > 0:
        return "▲"
    if delta < 0:
        return "▼"
    return "—"


# ── race-rounds browser ──────────────────────────────────────────────


def build_rounds_list_embed(
    rounds: list[workflow.RaceRoundSummary],
    *,
    tier_filter: str | None,
    page: int = 0,
) -> discord.Embed:
    scope = f"tier `{tier_filter}`" if tier_filter else "every tier"
    if not rounds:
        return discord.Embed(
            title="🏁 Race rounds",
            description=(
                f"No rounds imported for {scope} yet.\n\n"
                "Import a round with **Race Night → Import {tier}** or "
                "`/market-admin results import`."
            ),
            color=COLOR_INFO,
        )
    visible, page, pages = page_slice(rounds, page)
    lines = []
    for r in visible:
        held = r.held_on.isoformat() if r.held_on else "date not set"
        lines.append(
            f"`{r.tier_code}` R{r.round_order} — **{r.round_label}** · "
            f"{r.result_count} result(s) · {held}"
        )
    embed = discord.Embed(
        title=f"🏁 Race rounds — {scope}",
        description=truncate_field("\n".join(lines), _DESCRIPTION_LIMIT),
        color=COLOR_INFO,
    )
    _page_field(
        embed, shown=len(visible), page=page, pages=pages,
        total=len(rounds), noun="rounds",
    )
    return embed


class _RoundsBrowserView(AdminOwnedView):
    def __init__(
        self,
        *,
        opener_id: int,
        parent: HistoryView,
        tier_choices: list[tuple[str, str]],
        rounds: list[workflow.RaceRoundSummary],
        tier_filter: str | None,
        page: int = 0,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_filter = tier_filter

        visible, self.page, pages = page_slice(rounds, page)
        if tier_choices:
            self.add_item(
                _TierFilterSelect(
                    self, tier_choices, kind="rounds", current=tier_filter
                )
            )
        if visible:
            self.add_item(_RoundSelect(self, visible))
        self.add_item(BackButton(self._back))
        if pages > 1:
            self.add_item(
                _HistoryPageButton(
                    kind="rounds", delta=-1, enabled=self.page > 0
                )
            )
            self.add_item(
                _HistoryPageButton(
                    kind="rounds", delta=1, enabled=self.page + 1 < pages
                )
            )

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.parent.reload(interaction)


class _RoundSelect(discord.ui.Select):
    def __init__(
        self, parent: _RoundsBrowserView, rounds: list[workflow.RaceRoundSummary]
    ) -> None:
        super().__init__(
            placeholder="Pick a round to inspect…",
            options=[
                discord.SelectOption(
                    label=f"{r.tier_code} R{r.round_order} — {r.round_label}"[:100],
                    value=f"{r.tier_code}::{r.round_label}",
                    description=(
                        f"{r.result_count} result(s) · "
                        f"{r.held_on.isoformat() if r.held_on else 'no date'}"
                    )[:100],
                )
                for r in rounds
            ],
        )
        self._owner = parent

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        tier_code, round_label = self.values[0].split("::", 1)
        try:
            detail = await workflow.show_round(
                interaction.guild_id,
                tier_code=tier_code,
                round_label=round_label,
            )
        except workflow.WorkflowError as exc:
            await report_error(interaction, str(exc))
            return
        embed = build_round_detail_embed(detail)
        view = _RoundDetailView(
            opener_id=self._owner.opener_id, parent=self._owner
        )
        await interaction.edit_original_response(embed=embed, view=view)


class _RoundDetailView(AdminOwnedView):
    def __init__(
        self, *, opener_id: int, parent: _RoundsBrowserView
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.add_item(BackButton(self._back, label="Back to rounds"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await _open_rounds(
            interaction,
            opener_id=self.opener_id,
            parent=self.parent.parent,
            tier_filter=self.parent.tier_filter,
            page=self.parent.page,
        )


def build_round_detail_embed(
    detail: workflow.RaceRoundDetail,
) -> discord.Embed:
    held = detail.held_on.isoformat() if detail.held_on else "date not set"
    header = (
        f"**{detail.round_label}** · `{detail.tier_code}` · "
        f"round {detail.round_order} · {held} · {len(detail.results)} result(s)"
    )
    lines: list[str] = [header, ""]
    for r in detail.results:
        if r.dns:
            pos = "DNS"
        elif r.dnf:
            pos = "DNF"
        elif r.finish_position is not None:
            pos = f"P{r.finish_position}"
        else:
            pos = "—"
        flags = []
        if r.grid_position:
            flags.append(f"grid P{r.grid_position}")
        if r.fastest_lap:
            flags.append("FL")
        if r.driver_of_day:
            flags.append("DOTD")
        if r.incident_points:
            flags.append(f"{r.incident_points} inc")
        if r.exceptional:
            flags.append("⭐ exceptional")
        suffix = f" · {', '.join(flags)}" if flags else ""
        lines.append(f"{pos} — **{r.driver_name}**{suffix}")
        if r.factor_values:
            shown = ", ".join(
                f"{code} {value:+.3f}"
                for code, value in sorted(r.factor_values.items())
                if value
            )
            if shown:
                lines.append(f"    ↳ {shown}")
    return discord.Embed(
        title=f"🏁 {detail.round_label}",
        description=truncate_field("\n".join(lines), _DESCRIPTION_LIMIT),
        color=COLOR_INFO,
    )


# ── shared paging + tier filter ──────────────────────────────────────


class _HistoryPageButton(discord.ui.Button):
    """Page a browser rather than dropping everything past option 25."""

    def __init__(self, *, kind: str, delta: int, enabled: bool) -> None:
        super().__init__(
            label="Previous page" if delta < 0 else "Next page",
            style=discord.ButtonStyle.secondary,
            emoji="◀" if delta < 0 else "▶",
            disabled=not enabled,
        )
        self._kind = kind
        self._delta = delta

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert isinstance(view, _ValuationBrowserView | _RoundsBrowserView)
        opener = view.opener_id
        page = view.page + self._delta
        if self._kind == "valuation":
            await _open_valuations(
                interaction,
                opener_id=opener,
                parent=view.parent,
                tier_filter=view.tier_filter,
                page=page,
            )
        else:
            await _open_rounds(
                interaction,
                opener_id=opener,
                parent=view.parent,
                tier_filter=view.tier_filter,
                page=page,
            )


# ── shared tier filter select ────────────────────────────────────────


class _TierFilterSelect(discord.ui.Select):
    """
    Filter the browser to a single tier, or `all` for every tier.

    `kind` decides which reload path we take on callback so the two
    browsers can share this component.
    """

    def __init__(
        self,
        parent,
        tier_choices: list[tuple[str, str]],
        *,
        kind: str,
        current: str | None,
    ) -> None:
        options = [
            discord.SelectOption(
                label="All tiers", value="__all__", default=current is None
            )
        ]
        for code, label in tier_choices[: SELECT_MAX_OPTIONS - 1]:
            options.append(
                discord.SelectOption(
                    label=label[:100], value=code, default=code == current
                )
            )
        super().__init__(
            placeholder=f"Filter tier ({'all' if current is None else current})",
            options=options,
        )
        self._owner = parent
        self._kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        new_filter = None if picked == "__all__" else picked
        opener_id = self._owner.opener_id
        history_parent = self._owner.parent
        if self._kind == "valuation":
            await _open_valuations(
                interaction,
                opener_id=opener_id,
                parent=history_parent,
                tier_filter=new_filter,
            )
        else:
            await _open_rounds(
                interaction,
                opener_id=opener_id,
                parent=history_parent,
                tier_filter=new_filter,
            )


# ── entry points ─────────────────────────────────────────────────────


async def open_history(
    interaction: discord.Interaction, *, opener_id: int, on_back: BackCallback
) -> None:
    view = HistoryView(opener_id=opener_id, on_back=on_back)
    await interaction.response.edit_message(
        embed=build_history_embed(), view=view
    )
    await view.bind_message(interaction)


async def _open_valuations(
    interaction: discord.Interaction,
    *,
    opener_id: int,
    parent: HistoryView,
    tier_filter: str | None,
    page: int = 0,
) -> None:
    try:
        runs = await workflow.list_valuations(
            interaction.guild_id, tier_code=tier_filter
        )
    except workflow.WorkflowError as exc:
        await report_error(interaction, str(exc))
        return
    tier_choices = await workflow.list_tier_choices(interaction.guild_id)
    view = _ValuationBrowserView(
        opener_id=opener_id,
        parent=parent,
        tier_choices=tier_choices,
        runs=runs,
        tier_filter=tier_filter,
        page=page,
    )
    embed = build_valuation_list_embed(
        runs, tier_filter=tier_filter, page=view.page
    )
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    await view.bind_message(interaction)


async def _open_rounds(
    interaction: discord.Interaction,
    *,
    opener_id: int,
    parent: HistoryView,
    tier_filter: str | None,
    page: int = 0,
) -> None:
    try:
        rounds = await workflow.list_rounds(
            interaction.guild_id, tier_code=tier_filter
        )
    except workflow.WorkflowError as exc:
        await report_error(interaction, str(exc))
        return
    tier_choices = await workflow.list_tier_choices(interaction.guild_id)
    view = _RoundsBrowserView(
        opener_id=opener_id,
        parent=parent,
        tier_choices=tier_choices,
        rounds=rounds,
        tier_filter=tier_filter,
        page=page,
    )
    embed = build_rounds_list_embed(
        rounds, tier_filter=tier_filter, page=view.page
    )
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
    await view.bind_message(interaction)
