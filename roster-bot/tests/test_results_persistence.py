"""
Round-trip: imported sheet rows → race_rounds/race_results → normalized
observations → the valuation engine → real market movement.

This is the seam that was missing entirely. The engine, the movement
caps, the boards and the P/L maths were all real, but the cog handed
`factor_values={}` to every driver, so every published run scored zero
and returned the previous value unchanged. The last test in this file is
the one that fails if that ever regresses.
"""

from decimal import Decimal

from bot import queries
from bot.market import results as R
from bot.market import valuation as engine
from bot.presets import f1 as f1_preset


async def _bootstrap(conn, names=("Alice", "Bob", "Cara")):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES (1, 'S1', TRUE) RETURNING id"
    )
    await f1_preset.seed_season(conn, season_id)
    tiers = {
        r["code"]: r["id"]
        for r in await conn.fetch(
            "SELECT code, id FROM tiers WHERE season_id = $1", season_id
        )
    }
    drivers = {}
    for i, name in enumerate(names, start=1):
        drivers[name] = await queries.insert_driver(
            conn, season_id, tiers["t1"], member_id=100 + i,
            display_name=name, status="active",
        )
    return season_id, tiers, drivers


# ── Preset seeding ──────────────────────────────────────────────────────


async def test_preset_seeds_the_normalization_curve(pg_conn_migrated):
    season_id, _, _ = await _bootstrap(pg_conn_migrated)
    scores = await queries.fetch_position_scores(pg_conn_migrated, season_id)

    assert len(scores) == 22
    assert scores[0].position == 1
    assert scores[0].race_score == Decimal("1.0000")
    assert scores[0].is_win and scores[0].is_pole and scores[0].is_podium
    assert scores[2].is_podium and not scores[2].is_win
    assert not scores[3].is_podium
    # The curve must never reward a worse result more than a better one.
    assert [s.race_score for s in scores] == sorted(
        (s.race_score for s in scores), reverse=True
    )


async def test_preset_seeds_the_driver_of_day_factor(pg_conn_migrated):
    season_id, _, _ = await _bootstrap(pg_conn_migrated)
    codes = {
        f["code"]
        for f in await queries.fetch_valuation_factors(pg_conn_migrated, season_id)
    }
    # Every code the normalization layer emits must exist as a factor, or
    # the observation is computed and then silently discarded.
    assert {
        R.FACTOR_RACE_FINISH, R.FACTOR_QUALI_FINISH, R.FACTOR_POINTS,
        R.FACTOR_WINS, R.FACTOR_PODIUMS, R.FACTOR_POLES,
        R.FACTOR_FASTEST_LAPS, R.FACTOR_DRIVER_OF_DAY, R.FACTOR_DNF,
        R.FACTOR_INCIDENTS, R.FACTOR_FORM_TREND, R.FACTOR_CONSISTENCY,
    } <= codes


async def test_race_finish_weight_is_no_longer_inverted(pg_conn_migrated):
    """
    The original weight was +1.0000 against a raw finishing position.
    Combined with `contribution = weight * raw_value`, a P20 earned
    twenty times a win.
    """
    season_id, _, _ = await _bootstrap(pg_conn_migrated)
    factors = {
        f["code"]: f
        for f in await queries.fetch_valuation_factors(pg_conn_migrated, season_id)
    }
    assert factors[R.FACTOR_RACE_FINISH]["weight"] == Decimal("0.4500")
    # Weight must not exceed the cap, or the cap silently rewrites it.
    for f in factors.values():
        if f["max_contribution"] is not None:
            assert abs(f["weight"]) <= f["max_contribution"], f["code"]


async def test_preset_seeds_default_results_tuning(pg_conn_migrated):
    season_id, tiers, _ = await _bootstrap(pg_conn_migrated)
    tuning = await queries.fetch_results_tuning(pg_conn_migrated, season_id, tiers["t1"])
    assert tuning is not None
    assert tuning.form_window_rounds == 5
    assert tuning.consistency_window_rounds == 5
    assert tuning.max_incident_points == Decimal("6.00")


async def test_a_tier_override_beats_the_season_default(pg_conn_migrated):
    season_id, tiers, _ = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        """
        INSERT INTO results_config
            (season_id, tier_id, form_window_rounds,
             consistency_window_rounds, max_incident_points)
        VALUES ($1, $2, 3, 3, 10.00)
        """,
        season_id,
        tiers["t2"],
    )
    override = await queries.fetch_results_tuning(pg_conn_migrated, season_id, tiers["t2"])
    default = await queries.fetch_results_tuning(pg_conn_migrated, season_id, tiers["t1"])
    assert override.form_window_rounds == 3
    assert override.max_incident_points == Decimal("10.00")
    assert default.form_window_rounds == 5


# ── Round and result persistence ────────────────────────────────────────


async def test_a_round_imports_and_reloads_intact(pg_conn_migrated):
    season_id, tiers, drivers = await _bootstrap(pg_conn_migrated)
    round_row = await queries.upsert_race_round(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=tiers["t1"],
        round_label="R1 Bahrain",
        imported_by=999,
        source="sheet:abc/A1:Z100",
    )
    assert round_row["round_order"] == 1

    await queries.upsert_race_results(
        pg_conn_migrated,
        round_id=round_row["id"],
        rows=[
            {
                "driver_id": drivers["Alice"], "finish_position": 1,
                "grid_position": 2, "fastest_lap": True,
                "driver_of_day": True, "incident_points": Decimal("0"),
            },
            {
                "driver_id": drivers["Bob"], "finish_position": None,
                "grid_position": 1, "dnf": True,
                "incident_points": Decimal("2.0"),
            },
        ],
    )

    loaded = {
        r.driver_id: r
        for r in await queries.fetch_results_for_round(pg_conn_migrated, round_row["id"])
    }
    alice = loaded[drivers["Alice"]]
    assert alice.finish_position == 1
    assert alice.grid_position == 2
    assert alice.fastest_lap and alice.driver_of_day
    assert alice.round_order == 1

    bob = loaded[drivers["Bob"]]
    assert bob.dnf and bob.finish_position is None
    assert bob.incident_points == Decimal("2.00")
    assert isinstance(bob.incident_points, Decimal)


async def test_reimporting_a_round_corrects_it_in_place(pg_conn_migrated):
    """
    A stewards' decision after publication must be able to correct the
    facts without creating a phantom second round, which would otherwise
    double-count in every form and consistency window after it.
    """
    season_id, tiers, drivers = await _bootstrap(pg_conn_migrated)
    first = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        round_label="R1 Bahrain",
    )
    await queries.upsert_race_results(
        pg_conn_migrated, round_id=first["id"],
        rows=[{"driver_id": drivers["Alice"], "finish_position": 1}],
    )

    second = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        round_label="R1 Bahrain",
    )
    await queries.upsert_race_results(
        pg_conn_migrated, round_id=second["id"],
        rows=[{
            "driver_id": drivers["Alice"], "finish_position": 5,
            "note": "post-race penalty",
        }],
    )

    assert second["id"] == first["id"]
    assert second["round_order"] == 1
    assert await pg_conn_migrated.fetchval("SELECT COUNT(*) FROM race_rounds") == 1
    assert await pg_conn_migrated.fetchval("SELECT COUNT(*) FROM race_results") == 1

    results = await queries.fetch_results_for_round(pg_conn_migrated, first["id"])
    assert results[0].finish_position == 5


async def test_round_order_increments_per_tier(pg_conn_migrated):
    """Tiers race their own calendars, so ordering must not be shared."""
    season_id, tiers, _ = await _bootstrap(pg_conn_migrated)
    for label in ("R1", "R2", "R3"):
        await queries.upsert_race_round(
            pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
            round_label=label,
        )
    t2_first = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t2"], round_label="R1",
    )
    assert t2_first["round_order"] == 1
    assert await queries.next_round_order(pg_conn_migrated, season_id, tiers["t1"]) == 4


async def test_history_is_bounded_by_round_order(pg_conn_migrated):
    """
    Re-running an earlier round must reproduce exactly the form and
    consistency it originally saw. A later race must never leak
    backwards into an earlier valuation.
    """
    season_id, tiers, drivers = await _bootstrap(pg_conn_migrated)
    for order, (label, pos) in enumerate(
        [("R1", 1), ("R2", 5), ("R3", 20)], start=1
    ):
        row = await queries.upsert_race_round(
            pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
            round_label=label,
        )
        assert row["round_order"] == order
        await queries.upsert_race_results(
            pg_conn_migrated, round_id=row["id"],
            rows=[{"driver_id": drivers["Alice"], "finish_position": pos}],
        )

    through_two = await queries.fetch_results_history(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        through_round_order=2,
    )
    assert [r.finish_position for r in through_two[drivers["Alice"]]] == [1, 5]

    through_three = await queries.fetch_results_history(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        through_round_order=3,
    )
    assert [r.finish_position for r in through_three[drivers["Alice"]]] == [1, 5, 20]


async def test_history_is_isolated_per_tier(pg_conn_migrated):
    """
    ADR-001: markets are strictly tier-isolated. A Tier 2 result must be
    invisible to a Tier 1 valuation.
    """
    season_id, tiers, _ = await _bootstrap(pg_conn_migrated)
    t2_driver = await queries.insert_driver(
        pg_conn_migrated, season_id, tiers["t2"], member_id=500,
        display_name="T2Driver", status="active",
    )
    t2_round = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t2"], round_label="R1",
    )
    await queries.upsert_race_results(
        pg_conn_migrated, round_id=t2_round["id"],
        rows=[{"driver_id": t2_driver, "finish_position": 1}],
    )

    t1_history = await queries.fetch_results_history(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        through_round_order=99,
    )
    assert t1_history == {}


async def test_a_valuation_run_records_its_source_round(pg_conn_migrated):
    """A published value must be traceable back to the results behind it."""
    season_id, tiers, _ = await _bootstrap(pg_conn_migrated)
    round_row = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"], round_label="R1",
    )
    run_id = await queries.insert_valuation_run(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        round_label="R1", created_by=None, published=False,
    )
    await queries.set_valuation_run_round(pg_conn_migrated, run_id, round_row["id"])

    assert await pg_conn_migrated.fetchval(
        "SELECT round_id FROM valuation_runs WHERE id = $1", run_id
    ) == round_row["id"]


# ── The whole pipeline ──────────────────────────────────────────────────


async def test_imported_results_produce_real_market_movement(pg_conn_migrated):
    """
    The regression test for the inert market.

    Before this work the cog passed `factor_values={}`, so every driver
    scored exactly zero and every published run returned the previous
    value unchanged. If that regresses, this fails: a race winner must
    gain value and a retirement must lose it.
    """
    season_id, tiers, drivers = await _bootstrap(pg_conn_migrated)

    round_row = await queries.upsert_race_round(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        round_label="R1 Bahrain",
    )
    await queries.upsert_race_results(
        pg_conn_migrated, round_id=round_row["id"],
        rows=[
            {
                "driver_id": drivers["Alice"], "finish_position": 1,
                "grid_position": 1, "fastest_lap": True, "driver_of_day": True,
            },
            {"driver_id": drivers["Bob"], "finish_position": 10, "grid_position": 9},
            {
                "driver_id": drivers["Cara"], "finish_position": None,
                "grid_position": 4, "dnf": True, "incident_points": Decimal("4.0"),
            },
        ],
    )

    current = await queries.fetch_results_for_round(pg_conn_migrated, round_row["id"])
    scores = await queries.fetch_position_scores(pg_conn_migrated, season_id)
    tuning = await queries.fetch_results_tuning(pg_conn_migrated, season_id, tiers["t1"])
    history = await queries.fetch_results_history(
        pg_conn_migrated, season_id=season_id, tier_id=tiers["t1"],
        through_round_order=round_row["round_order"],
    )
    observations = {
        o.driver_id: o
        for o in R.build_observations(
            current=current, history=history, position_scores=scores, tuning=tuning,
        )
    }

    factor_rows = await queries.fetch_valuation_factors(pg_conn_migrated, season_id)
    cfg = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)
    outcomes = engine.compute_run(
        factors=[
            engine.FactorWeight(
                code=f["code"],
                weight=f["weight"],
                max_contribution=f["max_contribution"],
            )
            for f in factor_rows
        ],
        drivers=[
            engine.DriverInput(
                driver_id=did,
                display_name=name,
                previous_value=Decimal("20.00"),
                factor_values=observations[did].factor_values,
                exceptional=observations[did].exceptional,
            )
            for name, did in drivers.items()
        ],
        caps=engine.MovementCaps(
            weekly=cfg.weekly_move_cap, exceptional=cfg.exceptional_move_cap
        ),
    )
    by_id = {o.driver_id: o for o in outcomes}

    # The market actually moves.
    assert all(o.delta != Decimal(0) for o in outcomes)

    winner = by_id[drivers["Alice"]]
    midfield = by_id[drivers["Bob"]]
    retiree = by_id[drivers["Cara"]]

    assert winner.delta > Decimal(0)
    assert retiree.delta < Decimal(0)
    assert winner.delta > midfield.delta > retiree.delta
    assert winner.market_value > midfield.market_value > retiree.market_value

    # Pole + win + fastest lap earns the wider cap, and the clean sweep
    # is large enough to actually reach it.
    assert observations[drivers["Alice"]].exceptional is True
    assert winner.delta <= cfg.exceptional_move_cap
    assert midfield.delta <= cfg.weekly_move_cap

    # Money stays exact and the breakdown is a complete receipt.
    for o in outcomes:
        assert isinstance(o.market_value, Decimal)
        assert o.market_value == o.previous_value + o.delta
        assert len(o.breakdown) == len(factor_rows)


async def test_a_round_with_no_results_leaves_the_market_still(pg_conn_migrated):
    """
    A pre-season baseline run has no round behind it and must not invent
    movement out of nothing.
    """
    _, _, drivers = await _bootstrap(pg_conn_migrated, names=("Alice",))
    outcomes = engine.compute_run(
        factors=[
            engine.FactorWeight(
                code=R.FACTOR_WINS, weight=Decimal("0.18"),
                max_contribution=Decimal("0.25"),
            )
        ],
        drivers=[
            engine.DriverInput(
                driver_id=drivers["Alice"], display_name="Alice",
                previous_value=Decimal("20.00"), factor_values={},
            )
        ],
        caps=engine.MovementCaps(weekly=Decimal("0.75"), exceptional=Decimal("1.25")),
    )
    assert outcomes[0].delta == Decimal(0)
    assert outcomes[0].market_value == Decimal("20.00")
