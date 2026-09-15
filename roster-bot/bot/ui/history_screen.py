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
    lines = []
    for r in runs:
        state = "🟢 published" if r.published else "⚪ dry-run"
        lines.append(
            f"`#{r.run_id}` · `{r.tier_code}` · **{r.round_label}** · "
            f"{state} · {r.created_at:%Y-%m-%d %H:%M}"
        )
    return discord.Embed(
        title=f"💰 Valuation runs — {scope}",
        description=truncate_field("\n".join(lines), _DESCRIPTION_LIMIT),
        color=COLOR_INFO,
    )


class _ValuationBrowserView(AdminOwnedView):
    def __init__(
        self,
        *,
        opener_id: int,
        parent: HistoryView,
        tier_choices: list[tuple[str, str]],
        runs: list[workflow.ValuationRunSummary],
        tier_filter: str | None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_filter = tier_filter

        if tier_choices:
            self.add_item(
                _TierFilterSelect(
                    self, tier_choices, kind="valuation", current=tier_filter
                )
            )
        if runs:
            self.add_item(_ValuationRunSelect(self, runs))
        self.add_item(BackButton(self._back))

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
                for r in runs[:SELECT_MAX_OPTIONS]
            ],
        )
        self._parent = parent

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
            opener_id=self._parent.opener_id, parent=self._parent
        )
        await interaction.edit_original_response(embed=embed, view=view)


class _ValuationPreviewView(AdminOwnedView):
    def __init__(
        self, *, opener_id: int, parent: _ValuationBrowserView
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.add_item(BackButton(self._back, label="Back to runs"))

    async def _back(self, interaction: discord.Interaction) -> None:
        await _open_valuations(
            interaction,
            opener_id=self.opener_id,
            parent=self.parent.parent,
            tier_filter=self.parent.tier_filter,
        )


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
    return discord.Embed(
        title=(
            f"💰 Run #{preview.run_id} — `{preview.tier_code}` · "
            f"{preview.round_label}"
        ),
        description=truncate_field(f"{state}\n\n{body}", _DESCRIPTION_LIMIT),
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
    lines = []
    for r in rounds:
        held = r.held_on.isoformat() if r.held_on else "date not set"
        lines.append(
            f"`{r.tier_code}` R{r.round_order} — **{r.round_label}** · "
            f"{r.result_count} result(s) · {held}"
        )
    return discord.Embed(
        title=f"🏁 Race rounds — {scope}",
        description=truncate_field("\n".join(lines), _DESCRIPTION_LIMIT),
        color=COLOR_INFO,
    )


class _RoundsBrowserView(AdminOwnedView):
    def __init__(
        self,
        *,
        opener_id: int,
        parent: HistoryView,
        tier_choices: list[tuple[str, str]],
        rounds: list[workflow.RaceRoundSummary],
        tier_filter: str | None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.parent = parent
        self.tier_filter = tier_filter

        if tier_choices:
            self.add_item(
                _TierFilterSelect(
                    self, tier_choices, kind="rounds", current=tier_filter
                )
            )
        if rounds:
            self.add_item(_RoundSelect(self, rounds))
        self.add_item(BackButton(self._back))

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
                for r in rounds[:SELECT_MAX_OPTIONS]
            ],
        )
        self._parent = parent

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
            opener_id=self._parent.opener_id, parent=self._parent
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
        self._parent = parent
        self._kind = kind

    async def callback(self, interaction: discord.Interaction) -> None:
        picked = self.values[0]
        new_filter = None if picked == "__all__" else picked
        opener_id = self._parent.opener_id
        history_parent = self._parent.parent
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


async def _open_valuations(
    interaction: discord.Interaction,
    *,
    opener_id: int,
    parent: HistoryView,
    tier_filter: str | None,
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
    )
    embed = build_valuation_list_embed(runs, tier_filter=tier_filter)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)


async def _open_rounds(
    interaction: discord.Interaction,
    *,
    opener_id: int,
    parent: HistoryView,
    tier_filter: str | None,
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
    )
    embed = build_rounds_list_embed(rounds, tier_filter=tier_filter)
    if interaction.response.is_done():
        await interaction.edit_original_response(embed=embed, view=view)
    else:
        await interaction.response.edit_message(embed=embed, view=view)
