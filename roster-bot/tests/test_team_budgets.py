"""
Team budgets (Phase 7): the budget is a team's own money, the spending
cap is the league ceiling, and both must clear on every signing.

Pure-engine tests need no DB. Persistence tests run against the real
migrated schema so the sign trigger, partial unique indexes, and
net-delta corrections are exercised for real, not mocked.
"""

from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.market import budget as engine
from bot.market import budget_ops
from bot.presets import f1 as f1_preset

# ── Pure engine ─────────────────────────────────────────────────────────


def _cfg(**over) -> engine.BudgetConfig:
    base = dict(
        season_id=1, enforce_budget=True, rollover_enabled=True,
        opening_budget=Decimal("145.00"),
        earnings_per_point=Decimal("0.0500"),
        dnf_penalty=Decimal("0.50"),
        dns_penalty=Decimal("1.00"),
        penalty_per_incident_pt=Decimal("0.2500"),
    )
    base.update(over)
    return engine.BudgetConfig(**base)


_POINTS = {1: Decimal("25"), 2: Decimal("18"), 3: Decimal("15")}


def _fact(**over) -> engine.ResultFacts:
    base = dict(
        race_result_id=1, driver_id=10, team_id=100,
        finish_position=None, dnf=False, dns=False,
        incident_points=Decimal("0"),
    )
    base.update(over)
    return engine.ResultFacts(**base)


def test_win_earns_points_times_rate():
    charges, un = engine.charges_for_round([_fact(finish_position=1)], _cfg(), _POINTS)
    assert not un
    assert [(c.kind, c.amount) for c in charges] == [("race_earnings", Decimal("1.25"))]


def test_dnf_is_a_debit_and_earns_nothing():
    charges, _ = engine.charges_for_round([_fact(dnf=True)], _cfg(), _POINTS)
    assert [(c.kind, c.amount) for c in charges] == [("dnf_penalty", Decimal("-0.50"))]


def test_dns_priced_separately_from_dnf():
    charges, _ = engine.charges_for_round([_fact(dns=True)], _cfg(), _POINTS)
    assert [(c.kind, c.amount) for c in charges] == [("dns_penalty", Decimal("-1.00"))]


def test_incident_points_scale_linearly():
    charges, _ = engine.charges_for_round(
        [_fact(finish_position=3, incident_points=Decimal("3.00"))], _cfg(), _POINTS
    )
    by_kind = {c.kind: c.amount for c in charges}
    assert by_kind["race_earnings"] == Decimal("0.75")
    assert by_kind["incident_penalty"] == Decimal("-0.75")


def test_zero_rate_switches_a_rule_off():
    charges, _ = engine.charges_for_round(
        [_fact(dnf=True, incident_points=Decimal("2"))],
        _cfg(dnf_penalty=Decimal("0"), penalty_per_incident_pt=Decimal("0")),
        _POINTS,
    )
    assert charges == []


def test_free_agent_result_is_reported_not_charged():
    charges, un = engine.charges_for_round(
        [_fact(team_id=None, dnf=True)], _cfg(), _POINTS
    )
    assert charges == []
    assert [f.driver_id for f in un] == [10]


def test_available_and_rollover_share_arithmetic():
    assert engine.available_to_spend(Decimal("150"), Decimal("120")) == Decimal("30.00")
    assert engine.rollover_amount(Decimal("150"), Decimal("120")) == Decimal("30.00")
    # Underwater teams roll over debt.
    assert engine.rollover_amount(Decimal("100"), Decimal("120")) == Decimal("-20.00")


# ── Persistence ─────────────────────────────────────────────────────────


async def _bootstrap(conn, *, guild_id: int = 42, season_name: str = "S7"):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, $2, TRUE) RETURNING id",
        guild_id, season_name,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    team_a = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'A', 200, 300) RETURNING id", guild_id,
    )
    team_b = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'b', 'B', 201, 301) RETURNING id", guild_id,
    )
    d_a = await queries.insert_driver(
        conn, season_id, tier_id, member_id=111, display_name="A-Driver", status="active",
    )
    d_b = await queries.insert_driver(
        conn, season_id, tier_id, member_id=222, display_name="B-Driver", status="active",
    )
    d_free = await queries.insert_driver(
        conn, season_id, tier_id, member_id=333, display_name="Free", status="active",
    )
    c_a = await queries.insert_contract(
        conn, season_id=season_id, tier_id=tier_id, driver_id=d_a, team_id=team_a,
        contract_value=Decimal("10.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1, contract_type="standard",
        state="active", value_at_signing=None, approved_by=None,
    )
    c_b = await queries.insert_contract(
        conn, season_id=season_id, tier_id=tier_id, driver_id=d_b, team_id=team_b,
        contract_value=Decimal("12.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1, contract_type="standard",
        state="active", value_at_signing=None, approved_by=None,
    )
    return dict(
        guild_id=guild_id, season_id=season_id, tier_id=tier_id,
        team_a=team_a, team_b=team_b, d_a=d_a, d_b=d_b, d_free=d_free,
        c_a=c_a, c_b=c_b,
    )


@pytest.mark.asyncio
async def test_preset_seeds_budget_config_equal_to_cap(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    cfg = await queries.fetch_budget_config(pg_conn_migrated, ctx["season_id"], None)
    league = await queries.fetch_league_config_row(pg_conn_migrated, ctx["season_id"], None)
    assert cfg is not None
    assert cfg.opening_budget == league.salary_cap
    assert cfg.rollover_enabled and cfg.enforce_budget


@pytest.mark.asyncio
async def test_tier_override_falls_back_to_season_default(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    resolved = await queries.fetch_budget_config(
        pg_conn_migrated, ctx["season_id"], ctx["tier_id"]
    )
    assert resolved is not None and resolved.tier_id is None
    await queries.upsert_budget_config(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        enforce_budget=True, rollover_enabled=False, opening_budget=Decimal("100"),
        earnings_per_point=Decimal("0.1"), dnf_penalty=Decimal("1"),
        dns_penalty=Decimal("2"), penalty_per_incident_pt=Decimal("0.5"),
    )
    resolved = await queries.fetch_budget_config(
        pg_conn_migrated, ctx["season_id"], ctx["tier_id"]
    )
    assert resolved.tier_id == ctx["tier_id"] and resolved.opening_budget == Decimal("100")


@pytest.mark.asyncio
async def test_snapshot_opens_balance_once_and_reports_available(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    snap = await budget_ops.snapshot(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
    )
    assert snap.opened_now
    assert snap.balance == Decimal("145.00")
    assert snap.effective_payroll == Decimal("10.00")
    assert snap.available == Decimal("135.00")
    again = await budget_ops.snapshot(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
    )
    assert not again.opened_now and again.balance == Decimal("145.00")


@pytest.mark.asyncio
async def test_snapshot_is_none_when_not_configured(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "DELETE FROM budget_config WHERE season_id = $1", ctx["season_id"]
    )
    snap = await budget_ops.snapshot(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
    )
    assert snap is None


@pytest.mark.asyncio
async def test_prize_money_lets_teams_start_unequal_and_exceed_cap(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    bal_a = await budget_ops.award(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
        kind="prize_money", amount=Decimal("20.00"), note="S7 constructors P1", actor_id=1,
    )
    bal_b = await budget_ops.award(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_b"],
        kind="prize_money", amount=Decimal("5.00"), note="S7 constructors P6", actor_id=1,
    )
    # Opening balance was credited implicitly before the award.
    assert bal_a == Decimal("165.00")  # more money than the $145M cap
    assert bal_b == Decimal("150.00")
    league = await queries.fetch_league_config_row(pg_conn_migrated, ctx["season_id"], None)
    assert bal_a > league.salary_cap


@pytest.mark.asyncio
async def test_manual_kinds_are_guarded(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    common = dict(season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"], actor_id=1)
    with pytest.raises(budget_ops.BudgetError):
        await budget_ops.award(
            pg_conn_migrated, kind="race_earnings", amount=Decimal("1"), note="x", **common,
        )
    with pytest.raises(budget_ops.BudgetError):
        await budget_ops.award(
            pg_conn_migrated, kind="prize_money", amount=Decimal("-1"), note="x", **common,
        )
    with pytest.raises(budget_ops.BudgetError):
        await budget_ops.award(
            pg_conn_migrated, kind="adjustment", amount=Decimal("1"), note="  ", **common,
        )
    # Adjustment may debit.
    bal = await budget_ops.award(
        pg_conn_migrated, kind="adjustment", amount=Decimal("-2.50"), note="fine", **common,
    )
    assert bal == Decimal("142.50")


@pytest.mark.asyncio
async def test_db_trigger_rejects_wrong_sign_for_penalty_kinds(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    with pytest.raises(Exception, match="debit-only"):
        await queries.insert_budget_entry(
            pg_conn_migrated, season_id=ctx["season_id"], team_id=ctx["team_a"],
            kind="dnf_penalty", amount=Decimal("0.50"),
        )
    # ...unless it is a flagged correction.
    await queries.insert_budget_entry(
        pg_conn_migrated, season_id=ctx["season_id"], team_id=ctx["team_a"],
        kind="dnf_penalty", amount=Decimal("0.50"), is_correction=True,
    )


async def _import(conn, ctx, rows, label="R1"):
    rnd = await queries.upsert_race_round(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"], round_label=label,
    )
    await queries.upsert_race_results(conn, round_id=rnd["id"], rows=rows)
    outcome = await budget_ops.apply_round_charges(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        round_id=rnd["id"], actor_id=9,
    )
    return rnd["id"], outcome


@pytest.mark.asyncio
async def test_round_charges_hit_the_contracted_team(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    _, outcome = await _import(pg_conn_migrated, ctx, [
        {"driver_id": ctx["d_a"], "finish_position": 1},                       # +1.25
        {"driver_id": ctx["d_b"], "dnf": True, "incident_points": Decimal("2")},  # -0.50 -0.50
        {"driver_id": ctx["d_free"], "dns": True},                              # nobody
    ])
    assert outcome.enforced
    assert outcome.entries_written == 3
    assert outcome.total_credited == Decimal("1.25")
    assert outcome.total_debited == Decimal("1.00")
    assert outcome.unattributed_driver_ids == (ctx["d_free"],)
    assert await queries.fetch_budget_balance(
        pg_conn_migrated, ctx["team_a"], ctx["season_id"]
    ) == Decimal("146.25")
    assert await queries.fetch_budget_balance(
        pg_conn_migrated, ctx["team_b"], ctx["season_id"]
    ) == Decimal("144.00")


@pytest.mark.asyncio
async def test_reimport_identical_round_writes_nothing(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    rows = [{"driver_id": ctx["d_b"], "dnf": True}]
    await _import(pg_conn_migrated, ctx, rows)
    _, second = await _import(pg_conn_migrated, ctx, rows)
    assert second.entries_written == 0
    assert await queries.fetch_budget_balance(
        pg_conn_migrated, ctx["team_b"], ctx["season_id"]
    ) == Decimal("144.50")


@pytest.mark.asyncio
async def test_reimport_with_revised_facts_writes_only_the_delta(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    # Stewards first record a DNF with 4 incident points...
    await _import(pg_conn_migrated, ctx, [
        {"driver_id": ctx["d_b"], "dnf": True, "incident_points": Decimal("4")},
    ])
    assert await queries.fetch_budget_balance(
        pg_conn_migrated, ctx["team_b"], ctx["season_id"]
    ) == Decimal("143.50")  # 145 - 0.50 (DNF) - 1.00 (4 incident pts)
    # ...then reverse the DNF (classified P2) and cut incident points to 1.
    _, outcome = await _import(pg_conn_migrated, ctx, [
        {"driver_id": ctx["d_b"], "finish_position": 2, "incident_points": Decimal("1")},
    ])
    # dnf_penalty +0.50 (correction), incident +0.75 (correction), earnings +0.90 (new)
    assert outcome.entries_written == 3
    assert outcome.corrections_written == 2
    assert await queries.fetch_budget_balance(
        pg_conn_migrated, ctx["team_b"], ctx["season_id"]
    ) == Decimal("145.65")  # 145 - 0.25 + 0.90
    entries = await queries.fetch_budget_entries(
        pg_conn_migrated, ctx["team_b"], ctx["season_id"], 20
    )
    kinds = sorted((e.kind, e.amount, e.is_correction) for e in entries)
    assert ("dnf_penalty", Decimal("0.50"), True) in kinds
    assert ("dnf_penalty", Decimal("-0.50"), False) in kinds
    # Ledger is append-only: the original charge is still there.
    assert len([e for e in entries if e.kind == "dnf_penalty"]) == 2


@pytest.mark.asyncio
async def test_round_charges_noop_when_budgets_off(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "UPDATE budget_config SET enforce_budget = FALSE WHERE season_id = $1",
        ctx["season_id"],
    )
    _, outcome = await _import(pg_conn_migrated, ctx, [{"driver_id": ctx["d_b"], "dnf": True}])
    assert not outcome.enforced and outcome.entries_written == 0


@pytest.mark.asyncio
async def test_rollover_carries_unspent_and_is_idempotent(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    # Season 7: A has 145 + 20 prize, payroll 10 → 155 unspent. B: 145 − 0.50, payroll 12.
    await budget_ops.award(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
        kind="prize_money", amount=Decimal("20"), note="P1", actor_id=1,
    )
    await _import(pg_conn_migrated, ctx, [{"driver_id": ctx["d_b"], "dnf": True}])
    # Season 8.
    await pg_conn_migrated.execute(
        "UPDATE seasons SET is_active = FALSE WHERE id = $1", ctx["season_id"]
    )
    s8 = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S8', TRUE) RETURNING id",
        ctx["guild_id"],
    )
    await f1_preset.seed_season(pg_conn_migrated, s8)
    teams = [(ctx["team_a"], "A"), (ctx["team_b"], "B")]
    lines = await budget_ops.rollover(
        pg_conn_migrated, from_season_id=ctx["season_id"], to_season_id=s8,
        teams=teams, actor_id=1,
    )
    by_team = {line.team_id: line for line in lines}
    assert by_team[ctx["team_a"]].carried == Decimal("155.00")
    assert by_team[ctx["team_b"]].carried == Decimal("132.50")
    # New season balance = opening 145 + rollover; A now holds far more than the cap.
    bal_a_s8 = await queries.fetch_budget_balance(pg_conn_migrated, ctx["team_a"], s8)
    assert bal_a_s8 == Decimal("300.00")
    snap = await budget_ops.snapshot(
        pg_conn_migrated, season_id=s8, tier_id=None, team_id=ctx["team_a"],
    )
    assert snap.available == Decimal("300.00") - snap.effective_payroll

    again = await budget_ops.rollover(
        pg_conn_migrated, from_season_id=ctx["season_id"], to_season_id=s8,
        teams=teams, actor_id=1,
    )
    assert all(line.skipped_reason == "already rolled over" for line in again)
    bal_a_s8 = await queries.fetch_budget_balance(pg_conn_migrated, ctx["team_a"], s8)
    assert bal_a_s8 == Decimal("300.00")


@pytest.mark.asyncio
async def test_rollover_respects_target_flag_and_distinct_seasons(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    s8 = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S8', FALSE) RETURNING id",
        ctx["guild_id"],
    )
    await f1_preset.seed_season(pg_conn_migrated, s8)
    await pg_conn_migrated.execute(
        "UPDATE budget_config SET rollover_enabled = FALSE WHERE season_id = $1", s8
    )
    with pytest.raises(budget_ops.BudgetError, match="disabled"):
        await budget_ops.rollover(
            pg_conn_migrated, from_season_id=ctx["season_id"], to_season_id=s8,
            teams=[(ctx["team_a"], "A")], actor_id=1,
        )
    with pytest.raises(budget_ops.BudgetError, match="differ"):
        await budget_ops.rollover(
            pg_conn_migrated, from_season_id=s8, to_season_id=s8,
            teams=[(ctx["team_a"], "A")], actor_id=1,
        )


# ── Trades: cap and budget both gate approval ───────────────────────────


async def _lopsided_trade(conn, ctx) -> int:
    """A gives 10.00, B gives 12.00: A's payroll rises by 2.00."""
    trade_id = await service.propose_trade(
        conn, season_id=ctx["season_id"],
        proposing_team_id=ctx["team_a"], other_team_id=ctx["team_b"],
        proposed_by=100,
        items=[(ctx["team_a"], ctx["c_a"]), (ctx["team_b"], ctx["c_b"])],
        message=None, ttl_hours=24,
    )
    await service.accept_trade(conn, trade_id, actor_id=200)
    return trade_id


@pytest.mark.asyncio
async def test_trade_blocked_when_receiving_team_lacks_budget(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    # A's budget: 145 opening − 134 adjustment = 11.00; payroll after trade = 12.00.
    await budget_ops.award(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
        kind="adjustment", amount=Decimal("-134.00"), note="squeeze", actor_id=1,
    )
    trade_id = await _lopsided_trade(pg_conn_migrated, ctx)
    with pytest.raises(service.TransitionError, match="budget"):
        await service.commissioner_approve_trade(pg_conn_migrated, trade_id, actor_id=1)


@pytest.mark.asyncio
async def test_trade_blocked_at_cap_even_with_budget(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    await budget_ops.award(
        pg_conn_migrated, season_id=ctx["season_id"], tier_id=None, team_id=ctx["team_a"],
        kind="prize_money", amount=Decimal("100.00"), note="rich", actor_id=1,
    )
    await pg_conn_migrated.execute(
        "UPDATE league_config SET salary_cap = 11.00 WHERE season_id = $1", ctx["season_id"]
    )
    trade_id = await _lopsided_trade(pg_conn_migrated, ctx)
    with pytest.raises(service.TransitionError, match="spending cap"):
        await service.commissioner_approve_trade(pg_conn_migrated, trade_id, actor_id=1)


@pytest.mark.asyncio
async def test_trade_approves_when_both_clear(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    trade_id = await _lopsided_trade(pg_conn_migrated, ctx)
    await service.commissioner_approve_trade(pg_conn_migrated, trade_id, actor_id=1)
    moved = await queries.fetch_contract_by_id(pg_conn_migrated, ctx["c_b"])
    assert moved.team_id == ctx["team_a"]
