"""
Shared league workflows, independent of how they were invoked.

The slash commands in `cogs/admin_market.py` and the guided panel in
`cogs/panel.py` must do exactly the same thing. Rather than the panel
re-implementing the command bodies (two code paths that drift, and the
button one inevitably becomes the buggy one), both call into here.

Everything in this module is Discord-free apart from the ids it is
handed. It opens its own connection, raises `WorkflowError` with text
meant to be shown to a user, and returns a result object the caller
renders however it likes.

This module is NOT under the magic-number guard's watch the way
`bot/market/` is, but it holds no business numbers regardless — every
threshold it uses is read from `league_config`, `results_config`, or
`valuation_factors`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from bot import db, queries, results_ingest, sheets
from bot.market import boards as market_boards
from bot.market import results as results_engine
from bot.market import valuation as valuation_engine

log = logging.getLogger(__name__)


class WorkflowError(Exception):
    """A user-facing failure. The message is safe to show in Discord."""


class ImportAborted(WorkflowError):
    """
    Import found problems and wrote nothing.

    Carries the per-row errors so the caller can render them as a
    numbered list.
    """

    def __init__(self, errors: list[str]) -> None:
        self.errors = errors
        super().__init__(f"Import aborted — {len(errors)} problem(s) found. Nothing was written.")


@dataclass(frozen=True)
class ImportOutcome:
    tier_code: str
    round_label: str
    round_order: int
    written: int
    missing_drivers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RunOutcome:
    run_id: int
    tier_code: str
    round_label: str
    outcomes: list[valuation_engine.DriverValuation]
    priced_round: bool


@dataclass(frozen=True)
class TierStatus:
    code: str
    name: str
    driver_count: int
    latest_round_label: str | None
    latest_round_order: int | None
    unpublished_run_id: int | None
    has_published_valuation: bool


@dataclass(frozen=True)
class LeagueStatus:
    """
    Everything the home panel needs to tell an admin where they are.

    Read-only. Safe to build on every panel open.
    """

    season_name: str | None
    season_id: int | None
    tiers: list[TierStatus]
    has_config: bool
    commissioner_role_id: int | None
    board_count: int
    pending_offers: int
    pending_trades: int

    @property
    def has_season(self) -> bool:
        return self.season_id is not None

    @property
    def has_tiers(self) -> bool:
        return bool(self.tiers)

    @property
    def has_drivers(self) -> bool:
        return any(t.driver_count for t in self.tiers)

    @property
    def setup_complete(self) -> bool:
        return self.has_season and self.has_tiers and self.has_config and self.has_drivers


async def _require_season_and_tier(conn, guild_id: int, tier_code: str):
    season = await queries.fetch_active_season(conn, guild_id)
    if season is None:
        raise WorkflowError("No active season. Create one first, then activate it.")
    tier_row = await queries.fetch_tier(conn, season.id, tier_code)
    if tier_row is None:
        raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
    return season, tier_row


async def import_round(
    *,
    guild_id: int,
    user_id: int,
    tier: str,
    round_label: str,
    sheet: str,
    sheet_range: str,
    held_on: str | None = None,
) -> ImportOutcome:
    """
    Pull one round of race results from a sheet into `race_results`.

    All-or-nothing: any unresolvable row aborts the whole import. A
    half-imported round means a driver silently gets no market movement
    for the week, which is much harder to spot later than a loud failure
    now.

    Re-importing the same `round_label` corrects that round in place.
    """
    sheet_id = sheets.parse_sheet_id(sheet) or sheet.strip()

    race_date: date | None = None
    if held_on:
        try:
            race_date = date.fromisoformat(held_on.strip())
        except ValueError as exc:
            raise WorkflowError(f"Couldn't read `{held_on}` as a date. Use YYYY-MM-DD.") from exc

    values = await sheets.fetch_values(sheet_id, sheet_range)

    outcome = results_ingest.parse_results(values)
    if not outcome.rows:
        raise ImportAborted(outcome.errors or ["The sheet range held no readable result rows."])

    async with db.connect() as conn:
        season, tier_row = await _require_season_and_tier(conn, guild_id, tier)

        drivers = await queries.fetch_drivers_in_tier(conn, tier_row.id)
        if not drivers:
            raise WorkflowError(f"No drivers registered in tier `{tier}` yet.")
        roster = {d.display_name.casefold(): d.id for d in drivers}

        resolved, resolve_errors = results_ingest.resolve_drivers(outcome.rows, roster)
        all_errors = outcome.errors + resolve_errors
        if all_errors:
            raise ImportAborted(all_errors)

        round_row = await queries.upsert_race_round(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            round_label=round_label,
            held_on=race_date,
            imported_by=user_id,
            source=f"sheet:{sheet_id}/{sheet_range}",
        )
        written = await queries.upsert_race_results(conn, round_id=round_row["id"], rows=resolved)

        seen = {r["driver_id"] for r in resolved}
        missing = [d.display_name for d in drivers if d.id not in seen]

    return ImportOutcome(
        tier_code=tier,
        round_label=round_label,
        round_order=round_row["round_order"],
        written=written,
        missing_drivers=missing,
    )


async def run_valuation(
    *,
    guild_id: int,
    user_id: int,
    tier: str,
    round_label: str,
) -> RunOutcome:
    """
    Price a tier as an unpublished dry run.

    With no imported round under `round_label` this is a baseline run —
    empty observations, no movement — which is how a pre-season baseline
    is still created.
    """
    async with db.connect() as conn:
        season, tier_row = await _require_season_and_tier(conn, guild_id, tier)

        cfg = await queries.fetch_league_config_row(conn, season.id, tier_row.id)
        if cfg is None:
            cfg = await queries.fetch_league_config_row(conn, season.id, None)
        if cfg is None:
            raise WorkflowError(
                "No league config for that scope. Seed one with the F1 preset first."
            )

        factor_rows = await queries.fetch_valuation_factors(conn, season.id)
        drivers = await queries.fetch_drivers_in_tier(conn, tier_row.id)
        if not drivers:
            raise WorkflowError(
                f"No drivers in tier `{tier}` yet. Add drivers before running a valuation."
            )

        prev_values: dict[int, Decimal] = {}
        for d in drivers:
            latest = await queries.fetch_latest_published_valuation(conn, d.id)
            if latest is not None:
                prev_values[d.id] = latest

        factors = [
            valuation_engine.FactorWeight(
                code=row["code"],
                weight=row["weight"],
                max_contribution=row["max_contribution"],
            )
            for row in factor_rows
        ]

        round_row = await queries.fetch_race_round(conn, season.id, tier_row.id, round_label)
        observations: dict[int, results_engine.DriverObservations] = {}
        if round_row is not None:
            current = await queries.fetch_results_for_round(conn, round_row["id"])
            tuning = await queries.fetch_results_tuning(conn, season.id, tier_row.id)
            scores = await queries.fetch_position_scores(conn, season.id)
            if current and tuning is not None and scores:
                history = await queries.fetch_results_history(
                    conn,
                    season_id=season.id,
                    tier_id=tier_row.id,
                    through_round_order=round_row["round_order"],
                )
                for obs in results_engine.build_observations(
                    current=current,
                    history=history,
                    position_scores=scores,
                    tuning=tuning,
                ):
                    observations[obs.driver_id] = obs

        engine_inputs = [
            valuation_engine.DriverInput(
                driver_id=d.id,
                display_name=d.display_name,
                previous_value=prev_values.get(d.id, cfg.min_salary),
                factor_values=(observations[d.id].factor_values if d.id in observations else {}),
                exceptional=(observations[d.id].exceptional if d.id in observations else False),
            )
            for d in drivers
        ]
        caps = valuation_engine.MovementCaps(
            weekly=cfg.weekly_move_cap,
            exceptional=cfg.exceptional_move_cap,
        )

        outcomes = valuation_engine.compute_run(factors, engine_inputs, caps)
        run_id = await queries.insert_valuation_run(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            round_label=round_label,
            created_by=user_id,
            published=False,
        )
        if round_row is not None:
            await queries.set_valuation_run_round(conn, run_id, round_row["id"])

        rows = [
            {
                "driver_id": v.driver_id,
                "market_value": v.market_value,
                "previous_value": v.previous_value,
                "delta": v.delta,
                "rank_in_tier": v.rank_in_tier,
                "capped": v.capped,
                "breakdown": valuation_engine.breakdown_to_json(v.breakdown),
            }
            for v in outcomes
        ]
        await queries.insert_driver_valuations(conn, run_id, rows)

    return RunOutcome(
        run_id=run_id,
        tier_code=tier,
        round_label=round_label,
        outcomes=outcomes,
        priced_round=round_row is not None,
    )


async def fetch_league_status(guild_id: int) -> LeagueStatus:
    """
    Build the at-a-glance state used by the guided panel.

    Purely read-only, and tolerant of a half-configured server: every
    field degrades to a sensible empty rather than raising, because this
    runs on a brand-new server with nothing set up at all.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return LeagueStatus(
                season_name=None,
                season_id=None,
                tiers=[],
                has_config=False,
                commissioner_role_id=None,
                board_count=0,
                pending_offers=0,
                pending_trades=0,
            )

        cfg = await queries.fetch_league_config_row(conn, season.id, None)
        tier_rows = await queries.fetch_all_tiers(conn, season.id)

        tiers: list[TierStatus] = []
        for t in tier_rows:
            drivers = await queries.fetch_drivers_in_tier(conn, t.id)
            rounds = await queries.list_race_rounds(conn, season.id, t.id)
            # list_race_rounds orders ascending by round_order, so the
            # most recent round is the LAST element, not the first.
            latest = rounds[-1] if rounds else None
            tiers.append(
                TierStatus(
                    code=t.code,
                    name=t.name,
                    driver_count=len(drivers),
                    latest_round_label=latest["round_label"] if latest else None,
                    latest_round_order=latest["round_order"] if latest else None,
                    unpublished_run_id=await queries.fetch_latest_unpublished_run_id(
                        conn, season.id, t.id
                    ),
                    has_published_valuation=await queries.tier_has_published_valuation(
                        conn, t.id
                    ),
                )
            )

        boards = await queries.fetch_market_boards_in_season(conn, season.id)
        pending_offers = await queries.count_offers_awaiting_approval(conn, season.id)
        pending_trades = await queries.count_trades_awaiting_approval(conn, season.id)

    return LeagueStatus(
        season_name=season.name,
        season_id=season.id,
        tiers=tiers,
        has_config=cfg is not None,
        commissioner_role_id=(cfg.commissioner_role_id if cfg is not None else None),
        board_count=len(boards),
        pending_offers=pending_offers,
        pending_trades=pending_trades,
    )


@dataclass(frozen=True)
class PublishOutcome:
    """
    Result of publishing a dry-run.

    `already_published` distinguishes a no-op from a real publish so the
    caller can say so instead of implying it moved the market twice.
    `boards_refreshed` is False when values went live but the board
    redraw failed — the publish still stands, and the safety poll heals
    the boards, so this is reported rather than raised.
    """

    run_id: int
    season_id: int
    tier_id: int
    already_published: bool
    boards_refreshed: bool


async def publish_valuation(client, *, guild_id: int, run_id: int) -> PublishOutcome:
    """
    Publish a dry-run and refresh the boards that show it.

    `client` is only passed through to the board refresher; this module
    never touches it otherwise.
    """
    async with db.connect() as conn:
        run = await queries.fetch_valuation_run(conn, run_id)
        if run is None:
            raise WorkflowError(f"No valuation run with id `{run_id}`.")
        season_id = run["season_id"]
        tier_id = run["tier_id"]
        if run["published"]:
            return PublishOutcome(
                run_id=run_id,
                season_id=season_id,
                tier_id=tier_id,
                already_published=True,
                boards_refreshed=False,
            )
        await queries.publish_valuation_run(conn, run_id)

    # Refresh every market/movers board scoped to this tier plus every
    # cross-tier dashboard, so a publish is visible to everyone without
    # waiting for the periodic safety poll.
    refreshed = True
    try:
        await market_boards.refresh_boards_for_tier(
            client,
            guild_id=guild_id,
            season_id=season_id,
            tier_id=tier_id,
        )
    except Exception:
        refreshed = False
        log.exception(
            "Post-publish board refresh failed for run %s (values are "
            "published; boards will heal on the next poll)",
            run_id,
        )

    return PublishOutcome(
        run_id=run_id,
        season_id=season_id,
        tier_id=tier_id,
        already_published=False,
        boards_refreshed=refreshed,
    )
