"""
Contract carry-over across seasons (Phase 8).

A contract with term_seasons = N is N seasons of the same money. At the
season boundary every active row either gets a continuation row in the
new season (carried) or ends (expired). Money is copied, never
recomputed; carried payroll is reported against the cap, never blocked;
every state change is a ledger row; re-runs are no-ops.
"""

from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import carryover
from bot.market import budget_ops
from bot.models import Contract
from bot.presets import f1 as f1_preset

GUILD = 77


async def _season(conn, name: str, *, active: bool) -> int:
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, $2, $3) RETURNING id",
        GUILD, name, active,
    )
    await f1_preset.seed_season(conn, season_id)
    return season_id


async def _tier(conn, season_id: int, code: str = "t1") -> int:
    return await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = $2", season_id, code,
    )


async def _team(conn, key: str, role: int) -> int:
    return await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING id",
        GUILD, key, key.upper(), role, role + 100,
    )


async def _sign(
    conn, *, season_id, tier_id, driver_id, team_id, value, term, season_index=1, **extra
) -> int:
    return await queries.insert_contract(
        conn, season_id=season_id, tier_id=tier_id, driver_id=driver_id, team_id=team_id,
        contract_value=Decimal(value), signing_bonus=Decimal("2.00"),
        max_incentives=Decimal("1.00"), term_seasons=term, contract_type="standard",
        state="active", value_at_signing=Decimal("9.00"), approved_by=5,
        external_ref="S7-0001", season_index=season_index, **extra,
    )


async def _ledger(conn, season_id: int, kind: str) -> list:
    return await conn.fetch(
        "SELECT * FROM contract_ledger WHERE season_id = $1 AND kind = $2 ORDER BY id",
        season_id, kind,
    )


async def _setup(conn):
    """S7 (finished) with three signed drivers; S8 active and seeded."""
    s7 = await _season(conn, "S7", active=False)
    s8 = await _season(conn, "S8", active=True)
    t1_s7 = await _tier(conn, s7)
    team_a = await _team(conn, "a", 200)
    team_b = await _team(conn, "b", 201)
    d_multi = await queries.insert_driver(
        conn, s7, t1_s7, member_id=111, display_name="Multi", status="active",
    )
    d_last = await queries.insert_driver(
        conn, s7, t1_s7, member_id=222, display_name="LastYear", status="active",
    )
    d_mid = await queries.insert_driver(
        conn, s7, t1_s7, member_id=333, display_name="MidTerm", status="active",
    )
    c_multi = await _sign(
        conn, season_id=s7, tier_id=t1_s7, driver_id=d_multi, team_id=team_a,
        value="20.00", term=3,
    )
    c_last = await _sign(
        conn, season_id=s7, tier_id=t1_s7, driver_id=d_last, team_id=team_a,
        value="12.00", term=1,
    )
    # Already in season 2 of a 2-season deal → this is its final season.
    c_mid = await _sign(
        conn, season_id=s7, tier_id=t1_s7, driver_id=d_mid, team_id=team_b,
        value="8.00", term=2, season_index=2,
    )
    return dict(
        s7=s7, s8=s8, t1_s7=t1_s7, team_a=team_a, team_b=team_b,
        d_multi=d_multi, d_last=d_last, d_mid=d_mid,
        c_multi=c_multi, c_last=c_last, c_mid=c_mid,
    )


# ── Pure ────────────────────────────────────────────────────────────────


def _contract(**over) -> Contract:
    base = dict(
        id=1, season_id=1, tier_id=1, driver_id=1, team_id=1,
        contract_value=Decimal("1"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1, contract_type="standard",
        state="active",
    )
    base.update(over)
    return Contract(**base)


def test_continues_when_index_below_term():
    assert carryover.continues_next_season(_contract(term_seasons=3, season_index=1))
    assert carryover.continues_next_season(_contract(term_seasons=3, season_index=2))
    assert not carryover.continues_next_season(_contract(term_seasons=3, season_index=3))
    assert not carryover.continues_next_season(_contract(term_seasons=1, season_index=1))


def test_seasons_remaining_never_negative():
    assert _contract(term_seasons=2, season_index=1).seasons_remaining_after_this == 1
    assert _contract(term_seasons=1, season_index=1).seasons_remaining_after_this == 0
    assert _contract(term_seasons=1, season_index=4).seasons_remaining_after_this == 0


# ── Persistence ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_migration_defaults_existing_rows_to_first_season(pg_conn_migrated):
    ctx = await _setup(pg_conn_migrated)
    row = await queries.fetch_contract_by_id(pg_conn_migrated, ctx["c_multi"])
    assert row.season_index == 1
    assert row.carried_from_contract_id is None
    assert row.origin_contract_id is None
    states = {
        r["code"] for r in await pg_conn_migrated.fetch("SELECT code FROM contract_states")
    }
    assert "carried" in states
    kinds = {
        r["code"] for r in await pg_conn_migrated.fetch("SELECT code FROM transaction_kinds")
    }
    assert {"contract_carried", "contract_expired"} <= kinds


@pytest.mark.asyncio
async def test_carry_creates_next_row_and_expires_finished_deals(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    assert (outcome.carried, outcome.expired, outcome.skipped) == (1, 2, 0)
    assert outcome.drivers_created == 1  # Multi had no S8 driver row yet

    old = await queries.fetch_contract_by_id(conn, ctx["c_multi"])
    assert old.state == "carried"
    assert old.contract_value == Decimal("20.00")  # untouched

    [line] = [ln for ln in outcome.lines if ln.outcome == "carried"]
    new = await queries.fetch_contract_by_id(conn, line.new_contract_id)
    assert new.season_id == ctx["s8"]
    assert new.state == "active"
    assert new.contract_value == Decimal("20.00")
    assert new.term_seasons == 3
    assert new.season_index == 2
    assert new.signing_bonus == Decimal("0")  # one-time, already paid on origin
    assert new.max_incentives == Decimal("1.00")
    assert new.value_at_signing == Decimal("9.00")
    assert new.external_ref == "S7-0001"
    assert new.carried_from_contract_id == ctx["c_multi"]
    assert new.origin_contract_id == ctx["c_multi"]
    assert new.signed_at == old.signed_at
    assert new.team_id == ctx["team_a"]
    assert new.tier_id == await _tier(conn, ctx["s8"])

    # Driver row was created in S8, same member, same tier code.
    s8_driver = await queries.fetch_driver_by_member(conn, ctx["s8"], 111)
    assert s8_driver is not None and s8_driver.id == new.driver_id

    for cid in (ctx["c_last"], ctx["c_mid"]):
        row = await queries.fetch_contract_by_id(conn, cid)
        assert row.state == "expired"
    assert sorted(outcome.expired_members) == [(222, ctx["team_a"]), (333, ctx["team_b"])]
    # Expired drivers get no S8 driver row from carry-over — that is enrolment's job.
    assert await queries.fetch_driver_by_member(conn, ctx["s8"], 222) is None


@pytest.mark.asyncio
async def test_ledger_rows_in_both_seasons(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    out_rows = await _ledger(conn, ctx["s7"], "contract_carried")
    in_rows = await _ledger(conn, ctx["s8"], "contract_carried")
    assert len(out_rows) == 1 and len(in_rows) == 1
    assert out_rows[0]["contract_id"] == ctx["c_multi"]
    assert in_rows[0]["contract_id"] != ctx["c_multi"]
    assert out_rows[0]["amount"] == in_rows[0]["amount"] == Decimal("20.00")
    assert out_rows[0]["actor_id"] == in_rows[0]["actor_id"] == 9

    expired = await _ledger(conn, ctx["s7"], "contract_expired")
    assert sorted(r["contract_id"] for r in expired) == sorted([ctx["c_last"], ctx["c_mid"]])
    assert all(r["team_id"] is not None for r in expired)
    # Nothing expired in the target season's book.
    assert await _ledger(conn, ctx["s8"], "contract_expired") == []


@pytest.mark.asyncio
async def test_second_run_is_a_no_op(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    first = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    before = await conn.fetchval("SELECT COUNT(*) FROM contract_ledger")
    second = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    assert second.lines == []
    assert await conn.fetchval("SELECT COUNT(*) FROM contract_ledger") == before
    assert await conn.fetchval(
        "SELECT COUNT(*) FROM contracts WHERE season_id = $1", ctx["s8"]
    ) == first.carried


@pytest.mark.asyncio
async def test_chain_spans_three_seasons_then_expires(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    s9 = await _season(conn, "S9", active=False)
    await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    o2 = await carryover.carry_over(conn, from_season_id=ctx["s8"], to_season_id=s9, actor_id=9)
    assert (o2.carried, o2.expired) == (1, 0)
    s9_row = await queries.fetch_active_contract_for_driver(
        conn, (await queries.fetch_driver_by_member(conn, s9, 111)).id
    )
    assert s9_row.season_index == 3
    assert s9_row.origin_contract_id == ctx["c_multi"]
    assert s9_row.carried_from_contract_id != ctx["c_multi"]

    chain = await queries.fetch_contract_chain(conn, s9_row.id)
    assert [c.season_index for c in chain] == [1, 2, 3]
    assert [c.state for c in chain] == ["carried", "carried", "active"]
    assert chain == await queries.fetch_contract_chain(conn, ctx["c_multi"])

    s10 = await _season(conn, "S10", active=False)
    o3 = await carryover.carry_over(conn, from_season_id=s9, to_season_id=s10, actor_id=9)
    assert (o3.carried, o3.expired) == (0, 1)
    assert o3.expired_members == [(111, ctx["team_a"])]


@pytest.mark.asyncio
async def test_prefers_existing_target_driver_row_even_if_retiered(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    t2_s8 = await _tier(conn, ctx["s8"], "t2")
    pre = await queries.insert_driver(
        conn, ctx["s8"], t2_s8, member_id=111, display_name="Multi", status="active",
    )
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    [line] = [ln for ln in outcome.lines if ln.outcome == "carried"]
    assert not line.driver_created
    assert line.new_tier_code == "t2" and line.tier_code == "t1"
    new = await queries.fetch_contract_by_id(conn, line.new_contract_id)
    assert new.driver_id == pre and new.tier_id == t2_s8
    in_row = (await _ledger(conn, ctx["s8"], "contract_carried"))[0]
    assert in_row["tier_id"] == t2_s8


@pytest.mark.asyncio
async def test_skips_when_driver_already_signed_fresh_in_target(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    t1_s8 = await _tier(conn, ctx["s8"])
    d8 = await queries.insert_driver(
        conn, ctx["s8"], t1_s8, member_id=111, display_name="Multi", status="active",
    )
    fresh = await _sign(
        conn, season_id=ctx["s8"], tier_id=t1_s8, driver_id=d8, team_id=ctx["team_b"],
        value="30.00", term=1,
    )
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    [line] = [ln for ln in outcome.lines if ln.outcome == "skipped"]
    assert line.contract_id == ctx["c_multi"]
    assert "already has an active contract" in line.reason
    # Old row is left active so the commissioner sees it again next run.
    assert (await queries.fetch_contract_by_id(conn, ctx["c_multi"])).state == "active"
    assert (await queries.fetch_contract_by_id(conn, fresh)).state == "active"
    assert await _ledger(conn, ctx["s8"], "contract_carried") == []


@pytest.mark.asyncio
async def test_skips_when_target_lacks_tier_code(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    await conn.execute(
        "DELETE FROM tiers WHERE season_id = $1 AND code = 't1'", ctx["s8"]
    )
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    [line] = [ln for ln in outcome.lines if ln.outcome == "skipped"]
    assert "no tier `t1`" in line.reason
    assert not line.driver_created
    # Expiries still processed on the same run.
    assert outcome.expired == 2


@pytest.mark.asyncio
async def test_cannot_carry_a_row_twice(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    t1_s8 = await _tier(conn, ctx["s8"])
    d8 = await queries.insert_driver(
        conn, ctx["s8"], t1_s8, member_id=111, display_name="Multi", status="active",
    )
    await _sign(
        conn, season_id=ctx["s8"], tier_id=t1_s8, driver_id=d8, team_id=ctx["team_a"],
        value="20.00", term=3, season_index=2, carried_from_contract_id=ctx["c_multi"],
    )
    # Second continuation of the same origin row must be impossible at the DB level.
    s9 = await _season(conn, "S9", active=False)
    d9 = await queries.insert_driver(
        conn, s9, await _tier(conn, s9), member_id=111, display_name="Multi", status="active",
    )
    with pytest.raises(Exception, match="uq_contracts_carried_once"):
        await _sign(
            conn, season_id=s9, tier_id=await _tier(conn, s9), driver_id=d9,
            team_id=ctx["team_a"], value="20.00", term=3, season_index=2,
            carried_from_contract_id=ctx["c_multi"],
        )


@pytest.mark.asyncio
async def test_over_cap_is_reported_not_blocked(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    # Team A carries 20 from S7 and already committed 130 in S8 → 150 > 145 cap.
    t1_s8 = await _tier(conn, ctx["s8"])
    d_big = await queries.insert_driver(
        conn, ctx["s8"], t1_s8, member_id=444, display_name="Big", status="active",
    )
    await _sign(
        conn, season_id=ctx["s8"], tier_id=t1_s8, driver_id=d_big, team_id=ctx["team_a"],
        value="130.00", term=1,
    )
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    assert outcome.carried == 1
    [flag] = outcome.over_cap
    assert flag.team_id == ctx["team_a"]
    assert flag.salary_cap == Decimal("145.00")
    assert flag.over_by == Decimal("5.00")


@pytest.mark.asyncio
async def test_rejects_same_or_foreign_seasons(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    with pytest.raises(carryover.CarryOverError, match="differ"):
        await carryover.carry_over(
            conn, from_season_id=ctx["s7"], to_season_id=ctx["s7"], actor_id=9,
        )
    other = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (99, 'X', FALSE) RETURNING id"
    )
    with pytest.raises(carryover.CarryOverError, match="different servers"):
        await carryover.carry_over(
            conn, from_season_id=ctx["s7"], to_season_id=other, actor_id=9,
        )


@pytest.mark.asyncio
async def test_season_payroll_is_pinned_to_the_season_it_served(pg_conn_migrated):
    """
    Team A's S7 book was 20 + 12 = 32. After carry-over the 20 row is
    `carried` and the 12 row `expired`; the season-scoped payroll must
    still say 32, while the live (active-only) payroll now shows what
    is committed going forward.
    """
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    before = await queries.fetch_team_season_payroll(conn, ctx["team_a"], ctx["s7"])
    assert before == Decimal("32.00")
    await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    after = await queries.fetch_team_season_payroll(conn, ctx["team_a"], ctx["s7"])
    assert after == Decimal("32.00")
    assert await queries.fetch_team_season_payroll(conn, ctx["team_a"], ctx["s8"]) == Decimal(
        "20.00"
    )
    # Live payroll (the signing rule's input) no longer counts the expired 12.
    assert await queries.fetch_team_payroll(conn, ctx["team_a"]) == Decimal("20.00")


@pytest.mark.asyncio
async def test_budget_rollover_is_order_independent(pg_conn_migrated):
    """Rolling budget before or after carrying contracts yields the same number."""
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    await budget_ops.award(
        conn, season_id=ctx["s7"], tier_id=None, team_id=ctx["team_a"],
        kind="prize_money", amount=Decimal("15"), note="P2", actor_id=1,
    )
    await budget_ops.award(
        conn, season_id=ctx["s7"], tier_id=None, team_id=ctx["team_b"],
        kind="prize_money", amount=Decimal("3"), note="P6", actor_id=1,
    )
    teams = [(ctx["team_a"], "A"), (ctx["team_b"], "B")]

    # Order 1: budget first.
    lines = await budget_ops.rollover(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], teams=teams, actor_id=1,
    )
    first = {ln.team_id: ln.carried for ln in lines}
    assert first[ctx["team_a"]] == Decimal("145") + Decimal("15") - Decimal("32")
    assert first[ctx["team_b"]] == Decimal("145") + Decimal("3") - Decimal("8")

    # Order 2: contracts first, then budget — into a fresh target season.
    s8b = await _season(conn, "S8b", active=False)
    await conn.execute("UPDATE seasons SET is_active = FALSE WHERE id = $1", ctx["s8"])
    await carryover.carry_over(conn, from_season_id=ctx["s7"], to_season_id=s8b, actor_id=9)
    lines = await budget_ops.rollover(
        conn, from_season_id=ctx["s7"], to_season_id=s8b, teams=teams, actor_id=1,
    )
    second = {ln.team_id: ln.carried for ln in lines}
    assert second == first


@pytest.mark.asyncio
async def test_extension_resets_season_index(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _setup(conn)
    await queries.update_contract_terms(
        conn, ctx["c_mid"], contract_value=Decimal("9.00"), term_seasons=2,
        signing_bonus=Decimal("0"),
    )
    row = await queries.fetch_contract_by_id(conn, ctx["c_mid"])
    assert row.season_index == 1  # an extension is a new term starting now
    outcome = await carryover.carry_over(
        conn, from_season_id=ctx["s7"], to_season_id=ctx["s8"], actor_id=9,
    )
    carried_ids = {ln.contract_id for ln in outcome.lines if ln.outcome == "carried"}
    assert ctx["c_mid"] in carried_ids
