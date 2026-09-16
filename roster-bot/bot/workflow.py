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
from bot.contracts import carryover as contract_carryover
from bot.contracts import service as contracts_service
from bot.market import boards as market_boards
from bot.market import budget_ops, driver_ops, earnings_ops, escrow_ops
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
    # Budget consequences of the import; None when budgets are not
    # configured for the season so the renderer can stay silent.
    budget: budget_ops.RoundBudgetOutcome | None = None
    budget_unattributed: list[str] = field(default_factory=list)
    # Phase 9. None when escrow is off for the season — every season
    # before this update — so the renderer stays silent rather than
    # reporting a round that escrowed nothing.
    escrow: escrow_ops.RoundEscrowOutcome | None = None
    # Phase 10. Driver-side career earnings for this round. Unlike
    # `escrow` above this does NOT follow `escrow_enabled`, so it is
    # populated in both escrow modes; None only when the season has no
    # league config and there is no races_per_season to divide by.
    earnings: earnings_ops.RoundEarningsOutcome | None = None


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
    label: str
    driver_count: int
    has_role: bool
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

        # Same transaction as the results: a round's facts and its money
        # consequences land together or not at all.
        budget_outcome = await budget_ops.apply_round_charges(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            round_id=round_row["id"],
            actor_id=user_id,
        )
        names_by_id = {d.id: d.display_name for d in drivers}
        unattributed = [
            names_by_id.get(did, str(did))
            for did in budget_outcome.unattributed_driver_ids
        ]

        # Salary for this race leaves each team's cash here, and any
        # contract whose term just ended settles. Same transaction as the
        # results and the round charges above: a race's facts and all of
        # its money land together or not at all. Re-importing a sheet
        # advances no term and takes no second payment.
        cfg = await queries.fetch_league_config_row(conn, season.id, tier_row.id)
        if cfg is None:
            cfg = await queries.fetch_league_config_row(conn, season.id, None)
        escrow_outcome = None
        earnings_outcome = None
        if cfg is not None:
            escrow_outcome = await escrow_ops.charge_round_for_tier(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                round_id=round_row["id"],
                races_per_season=cfg.races_per_season,
                actor_id=user_id,
            )

            # The driver side of the same salary. Separate from the
            # escrow call above and NOT gated on escrow_enabled: a
            # driver earned their salary whether or not the league
            # models team cash. Nothing here debits a team.
            earnings_outcome = await earnings_ops.credit_round_for_tier(
                conn,
                guild_id=guild_id,
                season_id=season.id,
                tier_id=tier_row.id,
                round_id=round_row["id"],
                races_per_season=cfg.races_per_season,
                actor_id=user_id,
            )

    return ImportOutcome(
        tier_code=tier,
        round_label=round_label,
        round_order=round_row["round_order"],
        written=written,
        escrow=escrow_outcome,
        earnings=earnings_outcome,
        missing_drivers=missing,
        budget=budget_outcome if budget_outcome.enforced else None,
        budget_unattributed=unattributed if budget_outcome.enforced else [],
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
                f"**{season.name}** has no league settings yet, so there is "
                f"nothing to value against. Seed them with "
                f"`/market-admin season seed-preset name:{season.name}`, or "
                f"open `/league` \u2192 Setup, which offers the same thing. "
                f"Any tiers and drivers you have already added are kept."
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
                    label=t.label,
                    driver_count=len(drivers),
                    has_role=t.tier_role_id is not None,
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


# ── Setup operations ─────────────────────────────────────────────────
#
# These mirror the `/market-admin season|tier|config` commands so the
# panel's Setup screen and the commands cannot diverge. Each raises
# `WorkflowError` with user-facing text and writes nothing on failure.


@dataclass(frozen=True)
class SeasonCreated:
    season_id: int
    name: str
    preset_seeded: bool


async def create_season(
    *, guild_id: int, name: str, preset: str | None = None
) -> SeasonCreated:
    """
    Create a season, optionally seeding a preset.

    Does NOT activate it — that stays a separate, explicit step, matching
    the command behaviour. Activating silently would change which season
    every other command reads from as a side effect of creation.
    """
    name = name.strip()
    if not name:
        raise WorkflowError("Season name cannot be empty.")

    async with db.connect() as conn:
        existing = await queries.fetch_season_by_name(conn, guild_id, name)
        if existing is not None:
            raise WorkflowError(
                f"A season named **{name}** already exists in this server."
            )
        season_id = await queries.insert_season(conn, guild_id, name)
        seeded = False
        if preset == "f1":
            from bot.presets import f1 as f1_preset

            await f1_preset.seed_season(conn, season_id)
            seeded = True

    return SeasonCreated(season_id=season_id, name=name, preset_seeded=seeded)


@dataclass(frozen=True)
class PresetSeeded:
    season_id: int
    name: str
    tiers_created: int
    tiers_kept: int = 0
    settings_only: bool = False


async def seed_preset_into_season(
    *, guild_id: int, name: str, preset: str = "f1"
) -> PresetSeeded:
    """
    Seed a preset into a season that was created without one (G2).

    Before this existed, `create_season` was the only place a preset was
    ever applied, so a season created without one had no tiers and no
    `league_config` row and could never get either. Every downstream
    command then failed with a message telling the admin to "create a
    season with the preset" — advice that could not be followed for the
    season they had already named and possibly activated.

    Refuses only once the season has a config row. Re-seeding over one
    would reset valuation factors and the spending cap underneath
    contracts already signed against them, so this is a recovery path,
    never a reset button.

    Tiers alone do not block it. They used to, which was the second half
    of G2: a season created without a preset, then given a tier through
    Setup → Tiers, had tiers but no config row. Seeding refused because
    tiers existed, and the config panel it redirected to refused because
    there was no row to edit. The season was unusable and nothing in the
    bot could rescue it. When tiers exist, the settings are seeded and
    the tiers are left exactly as the commissioner built them.
    """
    if preset != "f1":
        raise WorkflowError(f"Unknown preset `{preset}`.")

    async with db.connect() as conn:
        season = await queries.fetch_season_by_name(conn, guild_id, name.strip())
        if season is None:
            raise WorkflowError(f"No season named **{name}** in this server.")

        tiers = await queries.fetch_all_tiers(conn, season.id)
        cfg = await queries.fetch_league_config_row(conn, season.id, None)

        # The config row is the thing worth protecting: it carries the
        # spending cap and the valuation factors that live contracts
        # were priced against.
        if cfg is not None:
            if tiers:
                codes = ", ".join(f"`{t.code}`" for t in tiers)
                raise WorkflowError(
                    f"**{season.name}** is already set up — tiers ({codes}) "
                    f"and league settings both. Seeding again would "
                    f"overwrite its valuation factors and spending cap "
                    f"underneath any contracts already signed. Edit "
                    f"settings from the config panel instead."
                )
            raise WorkflowError(
                f"**{season.name}** already has league settings. Edit them "
                f"from the config panel rather than re-seeding."
            )

        from bot.presets import f1 as f1_preset

        if tiers:
            # Tiers by hand, settings never seeded. Fill in the missing
            # half and keep the tiers — seeding t1/t2/t3 alongside them
            # would duplicate rank orders the panel refuses to create.
            await f1_preset.seed_settings_only(conn, season.id)
            return PresetSeeded(
                season_id=season.id,
                name=season.name,
                tiers_created=0,
                tiers_kept=len(tiers),
                settings_only=True,
            )

        await f1_preset.seed_season(conn, season.id)
        seeded = await queries.fetch_all_tiers(conn, season.id)

    return PresetSeeded(
        season_id=season.id, name=season.name, tiers_created=len(seeded)
    )


async def activate_season(*, guild_id: int, name: str) -> int:
    """Make a season active. Returns its id."""
    async with db.connect() as conn:
        target = await queries.fetch_season_by_name(conn, guild_id, name.strip())
        if target is None:
            raise WorkflowError(f"No season named **{name}** in this server.")
        await queries.activate_season(conn, target.id)
    return target.id


async def create_and_activate_season(
    *, guild_id: int, name: str, preset: str | None = None
) -> SeasonCreated:
    """
    Convenience for the Setup screen, which always wants both.

    Kept as a distinct function rather than a flag on `create_season` so
    the command path's create-then-activate semantics stay untouched.
    """
    created = await create_season(guild_id=guild_id, name=name, preset=preset)
    await activate_season(guild_id=guild_id, name=created.name)
    return created


def find_rank_conflict(tiers, rank_order: int, *, exclude_code: str | None = None):
    """
    The tier already holding `rank_order`, or None.

    `rank_order` is only a sort key in the schema — `002_seasons_tiers.sql`
    has no unique constraint on it — but `move_driver_to_tier` decides
    "promotion" versus "relegation" by comparing the two tiers' ranks.
    Two tiers sharing a rank makes that comparison, and every
    ordering-dependent display, meaningless. Gaps are harmless, so only
    an exact collision is reported, and `exclude_code` lets a tier keep
    the rank it already has when only its label or colour is changing.

    Lives here rather than in the setup screen so the typed
    `/market-admin tier add|edit` commands are guarded by the same rule
    as the panel modal. Pure and Discord-free, per CLAUDE.md.
    """
    for tier in tiers:
        if exclude_code is not None and tier.code == exclude_code:
            continue
        if tier.rank_order == rank_order:
            return tier
    return None


def rank_conflict_message(rank_order: int, holder) -> str:
    """Name the tier in the way, so the fix is obvious without a lookup."""
    return (
        f"Rank order {rank_order} is already used by `{holder.code}` "
        f"**{holder.label}**. Ranks decide which way promote and relegate "
        f"move a driver, so two tiers cannot share one — pick a different "
        f"number, or edit `{holder.code}` first."
    )


async def add_tier(
    *,
    guild_id: int,
    code: str,
    label: str,
    rank_order: int,
    tier_role_id: int | None = None,
    accent_color: int | None = None,
) -> int:
    """Add a tier to the active season. Returns the new tier id."""
    code = code.strip().lower()
    label = label.strip()
    if not code or not label:
        raise WorkflowError("Tier code and label are both required.")

    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError(
                "No active season. Create and activate one first."
            )
        if await queries.fetch_tier(conn, season.id, code) is not None:
            raise WorkflowError(
                f"Tier `{code}` already exists in **{season.name}**. "
                f"Use `/market-admin tier edit` to change it."
            )
        # G25 also applies to the typed command, not just the panel modal.
        holder = find_rank_conflict(
            await queries.fetch_all_tiers(conn, season.id), rank_order
        )
        if holder is not None:
            raise WorkflowError(rank_conflict_message(rank_order, holder))
        return await queries.insert_tier(
            conn,
            season.id,
            code=code,
            label=label,
            rank_order=rank_order,
            tier_role_id=tier_role_id,
            accent_color=accent_color,
        )


async def set_commissioner_role(
    *, guild_id: int, role_id: int, tier_code: str | None = None
) -> None:
    """Set the commissioner role on the season default or a tier override."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
            tier_id = tier.id
        if await queries.fetch_league_config_row(conn, season.id, tier_id) is None:
            raise WorkflowError(
                "No config row for that scope. Run `/market-admin config edit` first."
            )
        await queries.set_league_config_channels(
            conn,
            season_id=season.id,
            tier_id=tier_id,
            commissioner_role_id=role_id,
        )


# ── Board operations ─────────────────────────────────────────────────

# Board kinds that are scoped to a single tier. `dashboard` is the only
# cross-tier kind. Derived from the command's own choice list, not
# duplicated as a literal in two places.
TIER_SCOPED_BOARD_KINDS = ("market", "movers", "surplus", "underwater")
CROSS_TIER_BOARD_KINDS = ("dashboard",)
BOARD_KINDS = TIER_SCOPED_BOARD_KINDS + CROSS_TIER_BOARD_KINDS

BOARD_KIND_LABELS = {
    "market": "Market table",
    "movers": "Movers (risers & fallers)",
    "dashboard": "Cross-tier dashboard",
    "surplus": "Surplus (best P/L)",
    "underwater": "Underwater (worst P/L)",
}


@dataclass(frozen=True)
class BoardInfo:
    board_id: int
    kind: str
    tier_code: str | None
    channel_id: int
    healthy: bool


async def list_boards(guild_id: int) -> list[BoardInfo]:
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        boards = await queries.fetch_market_boards_in_season(conn, season.id)
        tier_by_id = {t.id: t for t in await queries.fetch_all_tiers(conn, season.id)}

    return [
        BoardInfo(
            board_id=b.id,
            kind=b.kind,
            tier_code=(tier_by_id[b.tier_id].code if b.tier_id else None),
            channel_id=b.channel_id,
            healthy=b.message_id is not None,
        )
        for b in boards
    ]


async def add_board(
    client, *, guild_id: int, kind: str, channel_id: int, tier_code: str | None = None
) -> int:
    """
    Create a board row and render it immediately. Returns the board id.

    Enforces the same tier/cross-tier pairing the command does: a
    tier-scoped kind without a tier (or a dashboard with one) is a
    user error, not something to guess at.
    """
    if kind not in BOARD_KINDS:
        raise WorkflowError(f"Unknown board kind `{kind}`.")

    needs_tier = kind in TIER_SCOPED_BOARD_KINDS
    if needs_tier and tier_code is None:
        raise WorkflowError(f"`{kind}` boards require a tier.")
    if not needs_tier and tier_code is not None:
        raise WorkflowError(f"`{kind}` boards are cross-tier; do not pass a tier.")

    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if needs_tier:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
            tier_id = tier.id
        board_id = await queries.insert_market_board(
            conn,
            season_id=season.id,
            tier_id=tier_id,
            kind=kind,
            channel_id=channel_id,
        )
        board_row = await queries.fetch_market_board_by_id(conn, board_id)

    assert board_row is not None
    await market_boards.refresh_board(client, board_row)
    return board_id


async def remove_board(client, *, board_id: int) -> None:
    """
    Delete a board and best-effort delete its message.

    A message that is already gone (or unreachable) is not an error —
    the row is what we are authoritative over.
    """
    async with db.connect() as conn:
        board_row = await queries.fetch_market_board_by_id(conn, board_id)
        if board_row is None:
            raise WorkflowError(f"No board with id `{board_id}`.")
        await queries.delete_market_board(conn, board_id)

    if board_row.message_id is None:
        return
    channel = client.get_channel(board_row.channel_id)
    if channel is None:
        return
    try:
        msg = await channel.fetch_message(board_row.message_id)
        await msg.delete()
    except Exception:
        log.debug("Could not delete board message for board %s", board_id, exc_info=True)


# Re-exported so panel screens and cogs can annotate refresh outcomes
# without importing `bot.market` directly (CLAUDE.md keeps the UI layer
# talking to `workflow`).
BoardRefresh = market_boards.BoardRefresh
describe_refresh = market_boards.describe_refresh
BOARD_FAILED_ACTIONS = market_boards.FAILED_ACTIONS


def boards_action_label(action: str) -> str:
    """Human wording for one `BoardRefresh.action` value."""
    return market_boards.ACTION_LABELS.get(action, action)


async def refresh_boards(
    client, *, board_id: int | None = None
) -> list[BoardRefresh]:
    """
    Re-render one board, or every board when `board_id` is None.

    Returns one outcome per board refreshed (G20) so callers can say
    what happened instead of replying ✅ unconditionally.
    """
    if board_id is None:
        return await market_boards.refresh_all_boards(client)
    async with db.connect() as conn:
        board_row = await queries.fetch_market_board_by_id(conn, board_id)
    if board_row is None:
        raise WorkflowError(f"No board with id `{board_id}`.")
    return [await market_boards.refresh_board(client, board_row)]


CHANNEL_KINDS = ("market", "transactions", "approvals")

CHANNEL_KIND_LABELS = {
    "market": "Market",
    "transactions": "Transactions",
    "approvals": "Approvals",
}


async def set_channel(
    *, guild_id: int, kind: str, channel_id: int, tier_code: str | None = None
) -> None:
    """Set one of the league's notification channels."""
    if kind not in CHANNEL_KINDS:
        raise WorkflowError(f"Unknown channel kind `{kind}`.")

    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
            tier_id = tier.id
        if await queries.fetch_league_config_row(conn, season.id, tier_id) is None:
            raise WorkflowError(
                "No config row for that scope. Run `/market-admin config edit` first."
            )
        await queries.set_league_config_channels(
            conn,
            season_id=season.id,
            tier_id=tier_id,
            **{f"{kind}_channel_id": channel_id},
        )


async def set_tier_role(*, guild_id: int, tier_code: str, role_id: int) -> None:
    """
    Attach a Discord role to a tier, preserving its other fields.

    Reuses `update_tier`, which rewrites every column, so the current
    label/rank/colour are read first and passed back unchanged. Skipping
    that read would silently blank them.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier = await queries.fetch_tier(conn, season.id, tier_code)
        if tier is None:
            raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
        await queries.update_tier(
            conn,
            tier.id,
            label=tier.label,
            rank_order=tier.rank_order,
            tier_role_id=role_id,
            accent_color=tier.accent_color,
        )


async def fetch_config_for_edit(*, guild_id: int, tier_code: str | None = None):
    """
    Fetch the config row a modal should pre-fill, with its scope ids.

    Returns `(season_id, tier_id, config_row)`. Raises when there is no
    row to edit, because an empty modal would silently create one with
    whatever the admin happened to type.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(f"No tier `{tier_code}` in **{season.name}**.")
            tier_id = tier.id
        cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)

    if cfg is None:
        scope = f"tier `{tier_code}`" if tier_code else "the season default"
        raise WorkflowError(
            f"No league config row for {scope}. Seed the F1 preset into "
            f"this season with `/market-admin season seed-preset`, or open "
            f"`/league` \u2192 Setup, which offers the same thing."
        )
    return season.id, tier_id, cfg


# ── Driver enrolment ─────────────────────────────────────────────────
#
# The panel's Drivers screen and `/market-admin driver add|sync|sync-all`
# both drive these; the underlying idempotent write lives in
# `bot/market/driver_ops.py`. Discord-facing callers gather members from
# tier roles themselves, then hand this layer plain `DriverSeed`s.


DRIVER_STATUS_CHOICES: tuple[tuple[str, str], ...] = (
    ("active", "Active"),
    ("reserve", "Reserve"),
    ("free_agent", "Free agent"),
    ("restricted_fa", "Restricted FA"),
    ("inactive", "Inactive"),
    ("suspended", "Suspended"),
)


@dataclass(frozen=True)
class TierDriverSummary:
    """Lightweight per-tier snapshot for the Drivers screen."""

    code: str
    label: str
    tier_role_id: int | None
    driver_count: int


@dataclass(frozen=True)
class DriverEnrolmentReport:
    tier_code: str
    display_name: str
    created: bool


@dataclass(frozen=True)
class TierSyncReport:
    """
    Result of syncing one tier from its Discord role.

    `skipped_reason` is populated iff the tier could not be sync'd at all
    (no role set, or the role id is not in the guild). `created` and
    `already_registered` are populated when the sync actually ran.
    """

    tier_code: str
    created: int
    already_registered: int
    skipped_reason: str | None = None


async def list_driver_summary(guild_id: int) -> list[TierDriverSummary]:
    """Every tier in the active season with its current driver count."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tiers = await queries.fetch_all_tiers(conn, season.id)
        summaries: list[TierDriverSummary] = []
        for t in tiers:
            drivers = await queries.fetch_drivers_in_tier(conn, t.id)
            summaries.append(
                TierDriverSummary(
                    code=t.code,
                    label=t.label,
                    tier_role_id=t.tier_role_id,
                    driver_count=len(drivers),
                )
            )
    return summaries


async def enrol_driver(
    *,
    guild_id: int,
    actor_id: int,
    tier_code: str,
    member_id: int,
    display_name: str,
    status: str,
) -> DriverEnrolmentReport:
    """Enrol one member into a tier; idempotent."""
    if status not in {code for code, _ in DRIVER_STATUS_CHOICES}:
        raise WorkflowError(f"Unknown driver status `{status}`.")
    async with db.connect() as conn:
        season, tier_row = await _require_season_and_tier(conn, guild_id, tier_code)
        result = await driver_ops.enrol_driver(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            seed=driver_ops.DriverSeed(
                member_id=member_id, display_name=display_name
            ),
            status=status,
            actor_id=actor_id,
        )
    return DriverEnrolmentReport(
        tier_code=tier_row.code,
        display_name=display_name,
        created=result.created,
    )


async def sync_drivers_in_tier(
    *,
    guild_id: int,
    actor_id: int,
    tier_code: str,
    seeds: list[driver_ops.DriverSeed],
) -> TierSyncReport:
    """
    Enrol every seed missing in the tier.

    Callers built the seed list from a Discord role's members; this layer
    stays Discord-free and just persists.
    """
    async with db.connect() as conn:
        season, tier_row = await _require_season_and_tier(conn, guild_id, tier_code)
        results = await driver_ops.sync_tier(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            seeds=seeds,
            status="active",
            actor_id=actor_id,
        )
    created = sum(1 for r in results if r.created)
    return TierSyncReport(
        tier_code=tier_row.code,
        created=created,
        already_registered=len(results) - created,
    )


async def sync_drivers_all_tiers(
    *,
    guild_id: int,
    actor_id: int,
    seeds_by_tier_code: dict[str, list[driver_ops.DriverSeed]],
    skipped_by_tier_code: dict[str, str],
) -> list[TierSyncReport]:
    """
    Sync every tier in the active season.

    `seeds_by_tier_code` carries the members the caller resolved from
    Discord for the tiers whose role is available; `skipped_by_tier_code`
    carries the human-facing reason a tier could not be sync'd, so the
    screen can render one row per tier either way.
    """
    reports: list[TierSyncReport] = []
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tiers = await queries.fetch_all_tiers(conn, season.id)
        for tier_row in tiers:
            if tier_row.code in skipped_by_tier_code:
                reports.append(
                    TierSyncReport(
                        tier_code=tier_row.code,
                        created=0,
                        already_registered=0,
                        skipped_reason=skipped_by_tier_code[tier_row.code],
                    )
                )
                continue
            seeds = seeds_by_tier_code.get(tier_row.code, [])
            results = await driver_ops.sync_tier(
                conn,
                season_id=season.id,
                tier_id=tier_row.id,
                seeds=seeds,
                status="active",
                actor_id=actor_id,
            )
            created = sum(1 for r in results if r.created)
            reports.append(
                TierSyncReport(
                    tier_code=tier_row.code,
                    created=created,
                    already_registered=len(results) - created,
                )
            )
    return reports


# ── Per-driver admin actions ─────────────────────────────────────────
#
# Backs the /league Drivers screen's driver-detail flow and mirrors
# `/market-admin void|set-status|promote|relegate`. Every mutation
# raises `WorkflowError` with user-facing text; the underlying transitions
# live in `bot/contracts/service.py`.


@dataclass(frozen=True)
class DriverForPanel:
    """Rich driver info the Drivers picker + detail view render off."""

    driver_id: int
    member_id: int
    display_name: str
    tier_code: str
    tier_label: str
    status: str
    active_contract_id: int | None
    active_team_name: str | None
    contract_value: Decimal | None
    market_value: Decimal | None

    @property
    def pl(self) -> Decimal | None:
        if self.market_value is None or self.contract_value is None:
            return None
        return self.market_value - self.contract_value


async def _driver_for_panel(conn, driver, tier) -> DriverForPanel:
    """Assemble a DriverForPanel row given already-fetched driver + tier."""
    contract = await queries.fetch_active_contract_for_driver(conn, driver.id)
    team_name: str | None = None
    if contract is not None:
        team = await queries.fetch_team_by_id(conn, contract.team_id)
        team_name = team.name if team else None
    market_value = await queries.fetch_latest_published_valuation(conn, driver.id)
    return DriverForPanel(
        driver_id=driver.id,
        member_id=driver.member_id,
        display_name=driver.display_name,
        tier_code=tier.code,
        tier_label=tier.label,
        status=driver.status,
        active_contract_id=contract.id if contract else None,
        active_team_name=team_name,
        contract_value=contract.contract_value if contract else None,
        market_value=market_value,
    )


async def list_drivers_in_season(guild_id: int) -> list[DriverForPanel]:
    """
    Every enrolled driver in the active season, richest first.

    Sorted by (tier rank, contract value desc, display_name) so the
    picker's top options are the highest-visibility drivers.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tiers = await queries.fetch_all_tiers(conn, season.id)
        out: list[DriverForPanel] = []
        for t in tiers:
            drivers = await queries.fetch_drivers_in_tier(conn, t.id)
            for d in drivers:
                out.append(await _driver_for_panel(conn, d, t))
    out.sort(
        key=lambda d: (
            d.tier_code,
            -(d.contract_value or Decimal("0")),
            d.display_name.casefold(),
        )
    )
    return out


async def fetch_driver_detail(guild_id: int, driver_id: int) -> DriverForPanel:
    """Single-driver refresh for the detail view."""
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            raise WorkflowError(f"No driver with id `{driver_id}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None or season.id != driver.season_id:
            raise WorkflowError(
                "That driver isn't in the currently active season."
            )
        tier = await queries.fetch_tier_by_id(conn, driver.tier_id)
        if tier is None:
            raise WorkflowError(
                f"Driver {driver_id}'s tier row is missing."
            )
        return await _driver_for_panel(conn, driver, tier)


@dataclass
class VoidOutcome:
    """
    What a void did, and what the caller still has to do about it.

    `team` is the team the driver was signed to, carried out so the
    Discord-facing caller can strip the role. This module cannot do it
    itself — it has no `discord` import by design — so a void that left
    the role in place was invisible to every caller until they were
    handed the team to act on.
    """
    contract_id: int
    member_id: int
    display_name: str
    team: object | None
    team_name: str | None


async def void_active_contract(
    *,
    guild_id: int,
    actor_id: int,
    driver_id: int,
    note: str | None,
) -> VoidOutcome:
    """
    Void a driver's active contract. Raises WorkflowError if the driver
    has no active contract in the current season.

    Returns the detail the caller needs to drop the team role, which is
    the caller's job rather than this module's.
    """
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            raise WorkflowError(f"No driver with id `{driver_id}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None or season.id != driver.season_id:
            raise WorkflowError(
                "That driver isn't in the currently active season."
            )
        contract = await queries.fetch_active_contract_for_driver(conn, driver_id)
        if contract is None:
            raise WorkflowError(
                f"{driver.display_name} has no active contract to void."
            )
        # Read the team before the void, while the contract still
        # points at it.
        team = await queries.fetch_team_by_id(conn, contract.team_id)
        try:
            await contracts_service.void_contract(
                conn, contract.id, actor_id=actor_id, note=note
            )
        except contracts_service.TransitionError as exc:
            raise WorkflowError(str(exc)) from exc
    return VoidOutcome(
        contract_id=contract.id,
        member_id=driver.member_id,
        display_name=driver.display_name,
        team=team,
        team_name=team.name if team is not None else None,
    )


async def set_driver_status(
    *,
    guild_id: int,
    actor_id: int,
    driver_id: int,
    status: str,
) -> None:
    """
    Change a driver's status and append a `status_change` ledger row.

    Mirrors `/market-admin set-status`: the ledger detail records
    `{"from": old, "to": new}` so the audit trail is complete.
    """
    if status not in {code for code, _ in DRIVER_STATUS_CHOICES}:
        raise WorkflowError(f"Unknown driver status `{status}`.")
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            raise WorkflowError(f"No driver with id `{driver_id}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None or season.id != driver.season_id:
            raise WorkflowError(
                "That driver isn't in the currently active season."
            )
        prior = driver.status
        if prior == status:
            return
        await queries.set_driver_status(conn, driver_id, status)
        await queries.append_ledger(
            conn,
            season_id=driver.season_id,
            tier_id=driver.tier_id,
            driver_id=driver_id,
            kind="status_change",
            detail={"from": prior, "to": status},
            actor_id=actor_id,
        )


async def move_driver_to_tier(
    *,
    guild_id: int,
    actor_id: int,
    driver_id: int,
    new_tier_code: str,
    note: str | None,
) -> None:
    """
    Promote or relegate a driver. Direction is derived from the target
    tier's `rank_order` — the caller doesn't need to know which is which.

    The driver's active contract (if any) moves with them; the underlying
    service layer handles the transfer.
    """
    async with db.connect() as conn:
        driver = await queries.fetch_driver_by_id(conn, driver_id)
        if driver is None:
            raise WorkflowError(f"No driver with id `{driver_id}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None or season.id != driver.season_id:
            raise WorkflowError(
                "That driver isn't in the currently active season."
            )
        new_tier = await queries.fetch_tier(conn, season.id, new_tier_code)
        if new_tier is None:
            raise WorkflowError(
                f"No tier `{new_tier_code}` in **{season.name}**."
            )
        try:
            await contracts_service.move_driver_between_tiers(
                conn, driver_id,
                new_tier_id=new_tier.id,
                actor_id=actor_id,
                note=note,
            )
        except contracts_service.TransitionError as exc:
            raise WorkflowError(str(exc)) from exc


# ── History (valuation runs + race rounds) ───────────────────────────
#
# Backs the Race Night → Browse history sub-panel. Mirrors
# `/market-admin valuation list|preview` and `/market-admin results
# list|show`; the panel uses these to let an admin inspect past runs
# and rounds without leaving Discord.


@dataclass(frozen=True)
class ValuationRunSummary:
    run_id: int
    tier_code: str
    round_label: str
    published: bool
    created_at: date


@dataclass(frozen=True)
class ValuationRunPreview:
    run_id: int
    tier_code: str
    round_label: str
    published: bool
    rows: list  # list of asyncpg.Record — display_name, market_value, delta, rank_in_tier, capped


@dataclass(frozen=True)
class RaceRoundSummary:
    tier_code: str
    round_order: int
    round_label: str
    held_on: date | None
    result_count: int


@dataclass(frozen=True)
class RoundResultRow:
    driver_name: str
    finish_position: int | None
    grid_position: int | None
    dnf: bool
    dns: bool
    fastest_lap: bool
    driver_of_day: bool
    incident_points: Decimal
    factor_values: dict[str, Decimal]
    exceptional: bool


@dataclass(frozen=True)
class RaceRoundDetail:
    tier_code: str
    round_label: str
    round_order: int
    held_on: date | None
    results: list[RoundResultRow]


async def list_valuations(
    guild_id: int, *, tier_code: str | None = None, limit: int = 20
) -> list[ValuationRunSummary]:
    """Recent runs for the season, most recent first."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tier_id: int | None = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(
                    f"No tier `{tier_code}` in **{season.name}**."
                )
            tier_id = tier.id
        rows = await queries.list_valuation_runs(
            conn, season.id, tier_id=tier_id, limit=limit
        )
    return [
        ValuationRunSummary(
            run_id=r["id"],
            tier_code=r["tier_code"],
            round_label=r["round_label"],
            published=r["published"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


async def preview_valuation(guild_id: int, run_id: int) -> ValuationRunPreview:
    """Re-fetch a stored run's driver rows for re-rendering."""
    async with db.connect() as conn:
        run = await queries.fetch_valuation_run(conn, run_id)
        if run is None:
            raise WorkflowError(f"No valuation run with id `{run_id}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None or season.id != run["season_id"]:
            raise WorkflowError(
                "That run isn't in the currently active season."
            )
        tier_row = await queries.fetch_tier_by_id(conn, run["tier_id"])
        rows = await queries.fetch_driver_valuations_for_run(conn, run_id)
    return ValuationRunPreview(
        run_id=run_id,
        tier_code=tier_row.code if tier_row else "?",
        round_label=run["round_label"],
        published=run["published"],
        rows=list(rows),
    )


async def list_rounds(
    guild_id: int, *, tier_code: str | None = None
) -> list[RaceRoundSummary]:
    """Imported rounds for the season, grouped by tier + round_order asc."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tier_id: int | None = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(
                    f"No tier `{tier_code}` in **{season.name}**."
                )
            tier_id = tier.id
        rows = await queries.list_race_rounds(conn, season.id, tier_id)
    return [
        RaceRoundSummary(
            tier_code=r["tier_code"],
            round_order=r["round_order"],
            round_label=r["round_label"],
            held_on=r["held_on"],
            result_count=r["result_count"],
        )
        for r in rows
    ]


async def show_round(
    guild_id: int, *, tier_code: str, round_label: str
) -> RaceRoundDetail:
    """
    One round's raw results paired with the normalized observations the
    valuation engine would compute for them. Mirrors
    `/market-admin results show`.
    """
    async with db.connect() as conn:
        season, tier_row = await _require_season_and_tier(conn, guild_id, tier_code)
        round_row = await queries.fetch_race_round(
            conn, season.id, tier_row.id, round_label
        )
        if round_row is None:
            raise WorkflowError(
                f"No round `{round_label}` imported for `{tier_code}`."
            )
        current = await queries.fetch_results_for_round(conn, round_row["id"])
        scores = await queries.fetch_position_scores(conn, season.id)
        tuning = await queries.fetch_results_tuning(conn, season.id, tier_row.id)
        history = await queries.fetch_results_history(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            through_round_order=round_row["round_order"],
        )
        drivers = await queries.fetch_drivers_in_tier(conn, tier_row.id)

    names = {d.id: d.display_name for d in drivers}
    observations = {}
    if current and tuning is not None and scores:
        observations = {
            o.driver_id: o
            for o in results_engine.build_observations(
                current=current,
                history=history,
                position_scores=scores,
                tuning=tuning,
            )
        }

    ordered = sorted(
        current,
        key=lambda r: (
            r.finish_position is None,
            r.finish_position or 0,
        ),
    )
    result_rows: list[RoundResultRow] = []
    for r in ordered:
        obs = observations.get(r.driver_id)
        result_rows.append(
            RoundResultRow(
                driver_name=names.get(r.driver_id, f"driver {r.driver_id}"),
                finish_position=r.finish_position,
                grid_position=r.grid_position,
                dnf=r.dnf,
                dns=r.dns,
                fastest_lap=r.fastest_lap,
                driver_of_day=r.driver_of_day,
                incident_points=r.incident_points,
                factor_values=dict(obs.factor_values) if obs else {},
                exceptional=bool(obs and obs.exceptional),
            )
        )
    return RaceRoundDetail(
        tier_code=tier_row.code,
        round_label=round_row["round_label"],
        round_order=round_row["round_order"],
        held_on=round_row["held_on"],
        results=result_rows,
    )


# ── Setup polish (list seasons/tiers, edit tier, show config, FA) ────
#
# Backs the Setup sub-panels for browsing seasons and tiers, viewing
# the current league config, and toggling free agency. Mirrors
# `/market-admin season list`, `tier list|edit`, `config show`, and
# `config free-agency`.


async def list_seasons(guild_id: int):
    """Every season for the guild, order not guaranteed here."""
    async with db.connect() as conn:
        return await queries.fetch_all_seasons(conn, guild_id)


async def list_tiers(guild_id: int):
    """
    Every tier in the active season with rank / label / role / colour.

    Returns [] when no season is active — the caller renders an empty
    state rather than raising.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        return await queries.fetch_all_tiers(conn, season.id)


async def edit_tier(
    *,
    guild_id: int,
    tier_code: str,
    label: str,
    rank_order: int,
    accent_color: int | None,
) -> None:
    """
    Rewrite a tier's label / rank / accent colour, preserving its role.

    Reuses `update_tier`, which rewrites every column, so we read the
    current tier_role_id first and pass it back unchanged (identical
    pattern to `set_tier_role`).
    """
    label = label.strip()
    if not label:
        raise WorkflowError("Tier label cannot be empty.")
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier = await queries.fetch_tier(conn, season.id, tier_code)
        if tier is None:
            raise WorkflowError(
                f"No tier `{tier_code}` in **{season.name}**."
            )
        # Excluding this tier so keeping its own rank is not a conflict.
        holder = find_rank_conflict(
            await queries.fetch_all_tiers(conn, season.id),
            rank_order,
            exclude_code=tier.code,
        )
        if holder is not None:
            raise WorkflowError(rank_conflict_message(rank_order, holder))
        await queries.update_tier(
            conn,
            tier.id,
            label=label,
            rank_order=rank_order,
            tier_role_id=tier.tier_role_id,
            accent_color=accent_color,
        )


async def show_config(guild_id: int, *, tier_code: str | None = None):
    """
    Read the league config for the season default or a tier override.

    Raises WorkflowError when the target scope has no row so the panel
    can render a "seed with the F1 preset" hint.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id: int | None = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(
                    f"No tier `{tier_code}` in **{season.name}**."
                )
            tier_id = tier.id
        cfg = await queries.fetch_league_config_row(conn, season.id, tier_id)
    if cfg is None:
        scope = f"tier `{tier_code}`" if tier_code else "the season default"
        raise WorkflowError(
            f"No league settings for {scope} yet. Seed them into this "
            f"season with `/market-admin season seed-preset "
            f"name:{season.name}`, or open `/league` \u2192 Setup. Creating "
            f"a new season is not necessary, and your tiers are kept."
        )
    return cfg


async def set_free_agency(
    *,
    guild_id: int,
    is_open: bool,
    tier_code: str | None = None,
) -> None:
    """
    Toggle the free_agency_open flag on the season default or a tier
    override. Same fallback shape as `set_channel` / `set_commissioner_role`.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id: int | None = None
        if tier_code is not None:
            tier = await queries.fetch_tier(conn, season.id, tier_code)
            if tier is None:
                raise WorkflowError(
                    f"No tier `{tier_code}` in **{season.name}**."
                )
            tier_id = tier.id
        if await queries.fetch_league_config_row(conn, season.id, tier_id) is None:
            raise WorkflowError(
                "No config row for that scope. Run Setup → Cap & rules first."
            )
        await queries.set_league_config_channels(
            conn,
            season_id=season.id,
            tier_id=tier_id,
            free_agency_open=is_open,
        )


# ── Team cap adjustments (rare admin action) ─────────────────────────


@dataclass(frozen=True)
class TeamForPanel:
    team_id: int
    key: str
    name: str
    payroll: Decimal


async def list_teams(guild_id: int) -> list[TeamForPanel]:
    """Every team in the guild with its current payroll (highest first)."""
    async with db.connect() as conn:
        teams = await queries.fetch_all_teams(conn, guild_id)
        out: list[TeamForPanel] = []
        for t in teams:
            payroll = await queries.fetch_team_payroll(conn, t.id)
            out.append(
                TeamForPanel(
                    team_id=t.id, key=t.key, name=t.name, payroll=payroll
                )
            )
    out.sort(key=lambda t: (-(t.payroll or Decimal("0")), t.name.casefold()))
    return out


async def remove_team(client, *, guild_id: int, team_key: str) -> str:
    """
    Delete a team and best-effort delete its roster message.

    Refuses when anything references the team. `contracts`, offers,
    trades, dead money and the budget ledger all reference `teams(id)`
    WITHOUT `ON DELETE CASCADE`, so deleting a team that ever signed
    anyone used to surface a raw `ForeignKeyViolationError` to the
    admin. A league's money history is not something to delete by
    accident, so the refusal is the correct behaviour — it just has to
    be explained.

    Returns the removed team's display name.
    """
    async with db.connect() as conn:
        team = await queries.fetch_team(conn, guild_id, team_key.lower())
        if team is None:
            raise WorkflowError(f"No team `{team_key}` in this server.")
        blockers = await queries.fetch_team_delete_blockers(conn, team.id)
        if blockers:
            detail = ", ".join(
                f"{count} {label}" for label, count in blockers.items()
            )
            raise WorkflowError(
                f"**{team.name}** cannot be deleted — it still has "
                f"{detail}. Deleting it would take the league's money "
                f"history with it.\n\nRelease or trade its drivers "
                f"first, or leave the team in place: a team with no "
                f"drivers signed costs nothing and keeps the records "
                f"intact."
            )
        message_id, channel_id = team.message_id, team.channel_id
        name = team.name
        await queries.delete_team(conn, team.id)

    # Duck-typed on purpose: this module must not import `discord`
    # (CLAUDE.md), so the message clean-up mirrors `remove_board` —
    # best-effort, and never a reason to fail a completed delete.
    if message_id:
        channel = client.get_channel(channel_id)
        if channel is not None:
            try:
                msg = await channel.fetch_message(message_id)
                await msg.delete()
            except Exception:
                log.debug(
                    "Could not delete roster message for team %s", name,
                    exc_info=True,
                )
    return name


async def adjust_team_cap(
    *,
    guild_id: int,
    actor_id: int,
    team_key: str,
    delta: Decimal,
    note: str,
) -> Decimal:
    """
    Append a `cap_adjustment` ledger row for the team.

    Returns the delta as recorded so the caller can echo the sign back.
    Mirrors `/market-admin adjust-cap`. The row is both the audit trail
    and the mechanism: `rules.cap_headroom_ok` sums these rows into the
    team's effective cap, so a dock really does reduce what they can
    spend (G18).
    """
    if not note.strip():
        raise WorkflowError("Cap adjustments require a note for the audit trail.")
    async with db.connect() as conn:
        team = await queries.fetch_team(conn, guild_id, team_key.lower())
        if team is None:
            raise WorkflowError(f"No team `{team_key}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        # Cap adjustments are per-team, not per-tier, so the tier column
        # takes any tier in the season for schema satisfaction. The
        # ledger detail carries the semantic meaning.
        tier_row = (await queries.fetch_all_tiers(conn, season.id))[0]
        await queries.append_ledger(
            conn,
            season_id=season.id,
            tier_id=tier_row.id,
            team_id=team.id,
            kind="cap_adjustment",
            amount=delta,
            detail={"note": note, "team_key": team.key},
            actor_id=actor_id,
        )
    return delta


# ── Team budgets (Phase 7) ───────────────────────────────────────────
# The spending cap is a league rule; the budget is a team's money. Both
# are enforced on every signing. These wrappers give slash commands and
# panel screens one Discord-free entry point each.


@dataclass(frozen=True)
class TeamBudgetSummary:
    team_id: int
    team_key: str
    team_name: str
    season_name: str
    salary_cap: Decimal
    balance: Decimal
    effective_payroll: Decimal
    available: Decimal
    cap_space: Decimal
    totals_by_kind: dict[str, Decimal]
    recent: list
    config: budget_ops.budget_engine.BudgetConfig


async def team_budget(*, guild_id: int, team_key: str, recent_limit: int) -> TeamBudgetSummary:
    """A team's budget position alongside its cap position, for `budget show`."""
    async with db.connect() as conn:
        team = await queries.fetch_team(conn, guild_id, team_key.lower())
        if team is None:
            raise WorkflowError(f"No team `{team_key}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None  # teams are not tier-scoped in the model; season default applies
        snap = await budget_ops.snapshot(
            conn, season_id=season.id, tier_id=tier_id, team_id=team.id,
        )
        if snap is None:
            raise WorkflowError(
                "Budgets are not configured for this season. Run "
                "`/market-admin budget config` to enable them."
            )
        league_cfg = await queries.fetch_league_config_row(conn, season.id, tier_id) \
            or await queries.fetch_league_config_row(conn, season.id, None)
        salary_cap = league_cfg.salary_cap if league_cfg else Decimal(0)
        totals = await queries.fetch_budget_totals_by_kind(conn, team.id, season.id)
        recent = await queries.fetch_budget_entries(conn, team.id, season.id, recent_limit)
    return TeamBudgetSummary(
        team_id=team.id,
        team_key=team.key,
        team_name=team.name,
        season_name=season.name,
        salary_cap=salary_cap,
        balance=snap.balance,
        effective_payroll=snap.effective_payroll,
        available=snap.available,
        cap_space=salary_cap - snap.effective_payroll,
        totals_by_kind=totals,
        recent=recent,
        config=snap.config,
    )


async def award_budget(
    *,
    guild_id: int,
    actor_id: int,
    team_key: str,
    kind: str,
    amount: Decimal,
    note: str,
) -> Decimal:
    """
    Commissioner credit/debit to a team's budget. `kind` is `prize_money`
    (credit only) or `adjustment` (either sign). Returns the new balance.
    """
    async with db.connect() as conn:
        team = await queries.fetch_team(conn, guild_id, team_key.lower())
        if team is None:
            raise WorkflowError(f"No team `{team_key}`.")
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        try:
            return await budget_ops.award(
                conn,
                season_id=season.id,
                tier_id=None,
                team_id=team.id,
                kind=kind,
                amount=amount,
                note=note,
                actor_id=actor_id,
            )
        except budget_ops.BudgetError as exc:
            raise WorkflowError(str(exc)) from exc


async def rollover_budgets(
    *,
    guild_id: int,
    actor_id: int,
    from_season_name: str,
) -> list[budget_ops.RolloverLine]:
    """
    Carry every team's unspent budget from `from_season_name` into the
    ACTIVE season. Idempotent per team. Run this once after activating
    the new season and before opening free agency.
    """
    async with db.connect() as conn:
        to_season = await queries.fetch_active_season(conn, guild_id)
        if to_season is None:
            raise WorkflowError("No active season to roll into. Activate the new season first.")
        from_season = await queries.fetch_season_by_name(conn, guild_id, from_season_name)
        if from_season is None:
            raise WorkflowError(f"No season named `{from_season_name}`.")
        teams = await queries.fetch_all_teams(conn, guild_id)
        try:
            return await budget_ops.rollover(
                conn,
                from_season_id=from_season.id,
                to_season_id=to_season.id,
                teams=[(t.id, t.name) for t in teams],
                actor_id=actor_id,
            )
        except budget_ops.BudgetError as exc:
            raise WorkflowError(str(exc)) from exc


@dataclass(frozen=True)
class CarryOverReport:
    from_season_name: str
    to_season_name: str
    outcome: contract_carryover.CarryOutcome
    team_names: dict[int, str]


async def carry_over_contracts(
    *,
    guild_id: int,
    actor_id: int,
    from_season_name: str,
) -> CarryOverReport:
    """
    Carry every active contract in `from_season_name` into the ACTIVE
    season: multi-season deals get their next row, finished deals expire.
    One transaction; idempotent; skipped rows stay active and are
    reported again on the next run. Run once after activating the new
    season, alongside `/market-admin budget rollover`.
    """
    async with db.connect() as conn:
        to_season = await queries.fetch_active_season(conn, guild_id)
        if to_season is None:
            raise WorkflowError("No active season to carry into. Activate the new season first.")
        from_season = await queries.fetch_season_by_name(conn, guild_id, from_season_name)
        if from_season is None:
            raise WorkflowError(f"No season named `{from_season_name}`.")
        try:
            outcome = await contract_carryover.carry_over(
                conn,
                from_season_id=from_season.id,
                to_season_id=to_season.id,
                actor_id=actor_id,
            )
        except contract_carryover.CarryOverError as exc:
            raise WorkflowError(str(exc)) from exc
        teams = await queries.fetch_all_teams(conn, guild_id)
    return CarryOverReport(
        from_season_name=from_season.name,
        to_season_name=to_season.name,
        outcome=outcome,
        team_names={t.id: t.name for t in teams},
    )


async def get_budget_config(*, guild_id: int, tier: str | None):
    """Resolved budget config for the active season (tier override if any)."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if tier:
            tier_row = await queries.fetch_tier(conn, season.id, tier)
            if tier_row is None:
                raise WorkflowError(f"No tier `{tier}` in **{season.name}**.")
            tier_id = tier_row.id
        return await queries.fetch_budget_config(conn, season.id, tier_id)


async def set_budget_config(
    *,
    guild_id: int,
    tier: str | None,
    enforce_budget: bool,
    rollover_enabled: bool,
    opening_budget: Decimal,
    earnings_per_point: Decimal,
    dnf_penalty: Decimal,
    dns_penalty: Decimal,
    penalty_per_incident_pt: Decimal,
    escrow_enabled: bool | None = None,
):
    """
    Create or update the budget config row for the active season / tier.

    `escrow_enabled` None leaves the stored setting untouched, so a caller
    editing rates or enforcement cannot move escrow by accident.
    """
    for label, value in (
        ("opening_budget", opening_budget),
        ("earnings_per_point", earnings_per_point),
        ("dnf_penalty", dnf_penalty),
        ("dns_penalty", dns_penalty),
        ("penalty_per_incident_pt", penalty_per_incident_pt),
    ):
        if value < 0:
            raise WorkflowError(
                f"`{label}` must be zero or positive (penalties are stored as magnitudes)."
            )
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            raise WorkflowError("No active season.")
        tier_id = None
        if tier:
            tier_row = await queries.fetch_tier(conn, season.id, tier)
            if tier_row is None:
                raise WorkflowError(f"No tier `{tier}` in **{season.name}**.")
            tier_id = tier_row.id
        return await queries.upsert_budget_config(
            conn,
            season_id=season.id,
            tier_id=tier_id,
            enforce_budget=enforce_budget,
            rollover_enabled=rollover_enabled,
            opening_budget=opening_budget,
            earnings_per_point=earnings_per_point,
            dnf_penalty=dnf_penalty,
            dns_penalty=dns_penalty,
            penalty_per_incident_pt=penalty_per_incident_pt,
            escrow_enabled=escrow_enabled,
        )


async def list_tier_choices(guild_id: int) -> list[tuple[str, str]]:
    """
    `(code, label)` pairs for the active season's tiers, in rank order.

    Returns an empty list rather than raising when there is no season, so
    selection menus can render a disabled empty state instead of erroring.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return []
        tiers = await queries.fetch_all_tiers(conn, season.id)
    return [(t.code, f"{t.code} — {t.label}") for t in tiers]


async def adjust_driver_earnings(
    *,
    guild_id: int,
    actor_id: int,
    member_id: int,
    kind: str,
    amount: Decimal,
    note: str,
) -> Decimal:
    """
    Commissioner credit/debit to a driver's career earnings. Returns the
    new career total.

    `kind` is `carry_in` (seeding a league that raced before earnings
    were tracked) or `adjustment` (either sign). Deliberately does NOT
    require an active season: a league seeding seven seasons of history
    may well do it before creating season 9, and the season link is only
    provenance. When a season IS active it is recorded.
    """
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        try:
            return await earnings_ops.adjust_career_total(
                conn,
                guild_id=guild_id,
                member_id=member_id,
                amount=amount,
                note=note,
                kind=kind,
                season_id=season.id if season is not None else None,
                actor_id=actor_id,
            )
        except earnings_ops.EarningsError as exc:
            raise WorkflowError(str(exc)) from exc


async def get_driver_earnings(
    *, guild_id: int, member_id: int
) -> tuple[Decimal, Decimal | None, str | None]:
    """
    A driver's (career total, active-season total, season name).

    The season parts are None when no season is active, so a caller can
    show a career total in the off-season without inventing a zero for a
    season that does not exist.
    """
    async with db.connect() as conn:
        career = await queries.fetch_career_earnings(conn, member_id, guild_id)
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return (career, None, None)
        season_total = await queries.fetch_season_earnings(
            conn, member_id, season.id
        )
        return (career, season_total, season.name)
