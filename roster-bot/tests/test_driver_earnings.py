"""
Driver career earnings against the real schema.

What only a database can prove, and what this feature has to get right
for a league already seven seasons deep:

  * a race credits each contracted driver exactly one race's share, and
    the same share the team side is charged, so the two halves of a
    salary cannot drift;
  * a re-imported round pays nobody twice (the partial unique index);
  * earnings accrue with escrow OFF, which is the state season 8 is in —
    this is the whole reason earnings are a separate pass rather than
    lines inside `escrow_ops.charge_race`;
  * the career total is keyed on `(guild_id, member_id)`, so it carries
    forward across seasons with no carry step at all;
  * deleting an old season cannot erase career history;
  * no team budget moves.
"""

from decimal import Decimal

import pytest

from bot import queries
from bot.market import earnings_ops, escrow, escrow_ops
from bot.presets import f1 as f1_preset

pytestmark = pytest.mark.asyncio

_MEMBER = 111
_OTHER_MEMBER = 222


async def _bootstrap(
    conn, *, guild_id: int = 88, races_per_season: int = 24, name: str = "S8",
):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, $2, TRUE) RETURNING id",
        guild_id, name,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    await conn.execute(
        "UPDATE league_config SET races_per_season = $2 WHERE season_id = $1",
        season_id, races_per_season,
    )
    # Teams are guild-scoped, not season-scoped: the cross-season tests
    # bootstrap a second season in the same guild and must reuse the
    # team that already exists there rather than colliding with it.
    team = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, 'a', 'A', 200, 300) ON CONFLICT (guild_id, key) "
        "DO UPDATE SET name = EXCLUDED.name RETURNING id",
        guild_id,
    )
    driver = await queries.insert_driver(
        conn, season_id, tier_id, member_id=_MEMBER, display_name="Driver",
        status="active",
    )
    return dict(
        guild_id=guild_id, season_id=season_id, tier_id=tier_id,
        team=team, driver=driver, races_per_season=races_per_season,
    )


async def _sign(conn, ctx, *, value="24.00", term_races=36, driver_id=None):
    contract_id = await queries.insert_contract(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"],
        driver_id=driver_id if driver_id is not None else ctx["driver"],
        team_id=ctx["team"],
        contract_value=Decimal(value), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1, contract_type="standard",
        state="active", value_at_signing=None, approved_by=None,
        term_races=term_races, races_served_before=0,
    )
    return await queries.fetch_contract_by_id(conn, contract_id)


async def _round(conn, ctx, *, number: int):
    return await conn.fetchval(
        "INSERT INTO race_rounds (season_id, tier_id, round_label, round_order) "
        "VALUES ($1, $2, $3, $4) RETURNING id",
        ctx["season_id"], ctx["tier_id"], f"R{number}", number,
    )


async def _pay_round(conn, ctx, round_id):
    return await earnings_ops.credit_round_for_tier(
        conn,
        guild_id=ctx["guild_id"],
        season_id=ctx["season_id"],
        tier_id=ctx["tier_id"],
        round_id=round_id,
        races_per_season=ctx["races_per_season"],
    )


# ── accrual ──────────────────────────────────────────────────────────


async def test_one_race_credits_one_races_share(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    outcome = await _pay_round(conn, ctx, await _round(conn, ctx, number=1))

    # $24.00M over a 24-race season is $1.00M a race.
    assert outcome.paid_count == 1
    assert outcome.total_paid == Decimal("1.00")
    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("1.00")


async def test_the_share_matches_what_the_team_side_is_charged(pg_conn_migrated):
    """
    Guards against the two halves of a salary drifting. If the team is
    debited one figure and the driver credited another, the league's
    books stop reconciling and nobody notices for a season.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=22)
    contract = await _sign(conn, ctx, value="17.50")
    outcome = await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    team_share = escrow.per_race_share(
        contract.contract_value, ctx["races_per_season"]
    )
    assert outcome.total_paid == team_share


async def test_earnings_accumulate_race_after_race(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    for number in (1, 2, 3):
        await _pay_round(conn, ctx, await _round(conn, ctx, number=number))
    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("3.00")


async def test_a_reimported_round_does_not_pay_twice(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    round_id = await _round(conn, ctx, number=1)

    first = await _pay_round(conn, ctx, round_id)
    second = await _pay_round(conn, ctx, round_id)

    assert first.paid_count == 1
    assert second.paid_count == 0
    assert second.already_paid == 1
    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("1.00")


async def test_a_driver_with_no_contract_earns_nothing(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    outcome = await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    assert outcome.paid_count == 0
    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("0")


async def test_a_sub_cent_share_is_reported_not_silently_dropped(pg_conn_migrated):
    """
    The ledger rejects a zero row, so a share that rounds to nothing
    cannot be written. It has to surface in the receipt rather than
    vanish, or an admin sees a driver stuck on $0.00M with no reason.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="0.001")
    outcome = await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    assert outcome.paid_count == 0
    assert outcome.too_small_to_pay == 1


# ── independent of escrow ────────────────────────────────────────────


async def test_earnings_accrue_with_escrow_switched_off(pg_conn_migrated):
    """
    Season 8 runs with escrow off. If earnings were a side effect of
    `charge_race` they would never accrue at all — which is exactly why
    they are a separate pass.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await conn.execute(
        "UPDATE budget_config SET escrow_enabled = FALSE WHERE season_id = $1",
        ctx["season_id"],
    )
    assert not await escrow_ops.is_enabled(
        conn, season_id=ctx["season_id"], tier_id=ctx["tier_id"]
    )
    await _sign(conn, ctx, value="24.00")
    outcome = await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    assert outcome.total_paid == Decimal("1.00")


async def test_paying_a_driver_does_not_move_the_team_budget(pg_conn_migrated):
    """
    The user's rule: drivers keep their salary, teams are not charged for
    it twice. Career earnings are a record, not a transfer.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    before = await queries.fetch_budget_balance(
        conn, ctx["team"], ctx["season_id"]
    )
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    after = await queries.fetch_budget_balance(
        conn, ctx["team"], ctx["season_id"]
    )
    assert after == before


# ── carry-forward across seasons ─────────────────────────────────────


async def test_the_career_total_spans_seasons_with_no_carry_step(pg_conn_migrated):
    """
    Identity is `(guild_id, member_id)`, not the driver row, so a new
    season needs no carry-over job — the total simply keeps counting.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))

    # Next season, same guild, same member, a fresh driver row.
    await conn.execute(
        "UPDATE seasons SET is_active = FALSE WHERE id = $1", ctx["season_id"]
    )
    nxt = await _bootstrap(
        conn, guild_id=ctx["guild_id"], races_per_season=24, name="S9",
    )
    await _sign(conn, nxt, value="48.00")
    await _pay_round(conn, nxt, await _round(conn, nxt, number=1))

    career = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    season_only = await queries.fetch_season_earnings(
        conn, _MEMBER, nxt["season_id"]
    )
    assert career == Decimal("3.00")
    assert season_only == Decimal("2.00")


async def test_deleting_an_old_season_keeps_the_career_total(pg_conn_migrated):
    """
    The FKs are ON DELETE SET NULL, not CASCADE. An owner tidying up
    season 3 must not wipe three seasons of a driver's earnings.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    await conn.execute("DELETE FROM seasons WHERE id = $1", ctx["season_id"])

    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("1.00")


async def test_another_guild_is_not_counted(pg_conn_migrated):
    conn = pg_conn_migrated
    await _bootstrap(conn, guild_id=88)
    other = await _bootstrap(conn, guild_id=99)
    await _sign(conn, other, value="24.00")
    await _pay_round(conn, other, await _round(conn, other, number=1))

    assert await queries.fetch_career_earnings(conn, _MEMBER, 88) == Decimal("0")
    assert await queries.fetch_career_earnings(conn, _MEMBER, 99) == Decimal("1.00")


# ── leaderboard ──────────────────────────────────────────────────────


async def test_the_leaderboard_ranks_by_total(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    second = await queries.insert_driver(
        conn, ctx["season_id"], ctx["tier_id"], member_id=_OTHER_MEMBER,
        display_name="Rival", status="active",
    )
    await _sign(conn, ctx, value="24.00")
    await _sign(conn, ctx, value="48.00", driver_id=second)
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))

    rows = await queries.fetch_earnings_leaderboard(
        conn, guild_id=ctx["guild_id"], season_id=None, limit=10
    )
    assert [r.member_id for r in rows] == [_OTHER_MEMBER, _MEMBER]
    assert rows[0].total == Decimal("2.00")
    assert rows[0].display_name == "Rival"
    assert rows[0].races_paid == 1
    assert rows[0].seasons_paid == 1


async def test_the_leaderboard_can_be_scoped_to_one_season(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await _sign(conn, ctx, value="24.00")
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    await conn.execute(
        "UPDATE seasons SET is_active = FALSE WHERE id = $1", ctx["season_id"]
    )
    nxt = await _bootstrap(
        conn, guild_id=ctx["guild_id"], races_per_season=24, name="S9",
    )
    await _sign(conn, nxt, value="24.00")
    await _pay_round(conn, nxt, await _round(conn, nxt, number=1))

    career = await queries.fetch_earnings_leaderboard(
        conn, guild_id=ctx["guild_id"], season_id=None, limit=10
    )
    scoped = await queries.fetch_earnings_leaderboard(
        conn, guild_id=ctx["guild_id"], season_id=nxt["season_id"], limit=10
    )
    assert career[0].total == Decimal("2.00")
    assert career[0].seasons_paid == 2
    assert scoped[0].total == Decimal("1.00")
    assert scoped[0].seasons_paid == 1


# ── admin seeding and corrections ────────────────────────────────────


async def test_carry_in_seeds_a_total_from_seasons_never_tracked(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    total = await earnings_ops.adjust_career_total(
        conn,
        guild_id=ctx["guild_id"],
        member_id=_MEMBER,
        amount=Decimal("180.00"),
        note="S1-S7 from the old spreadsheet",
        kind=earnings_ops.KIND_CARRY_IN,
        season_id=ctx["season_id"],
        actor_id=5,
    )
    assert total == Decimal("180.00")


async def test_a_carried_in_total_keeps_accruing(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn, races_per_season=24)
    await earnings_ops.adjust_career_total(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
        amount=Decimal("180.00"), note="S1-S7", kind=earnings_ops.KIND_CARRY_IN,
        season_id=ctx["season_id"], actor_id=5,
    )
    await _sign(conn, ctx, value="24.00")
    await _pay_round(conn, ctx, await _round(conn, ctx, number=1))
    total = await queries.fetch_career_earnings(conn, _MEMBER, ctx["guild_id"])
    assert total == Decimal("181.00")


async def test_an_adjustment_may_be_negative(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    await earnings_ops.adjust_career_total(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
        amount=Decimal("10.00"), note="seed", kind=earnings_ops.KIND_CARRY_IN,
        season_id=None, actor_id=5,
    )
    total = await earnings_ops.adjust_career_total(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
        amount=Decimal("-2.50"), note="double-counted S6",
        kind=earnings_ops.KIND_ADJUSTMENT, season_id=None, actor_id=5,
    )
    assert total == Decimal("7.50")


async def test_a_zero_adjustment_is_refused(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    with pytest.raises(earnings_ops.EarningsError):
        await earnings_ops.adjust_career_total(
            conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
            amount=Decimal("0"), note="nothing",
            kind=earnings_ops.KIND_ADJUSTMENT, season_id=None, actor_id=5,
        )


async def test_an_adjustment_without_a_reason_is_refused(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    with pytest.raises(earnings_ops.EarningsError):
        await earnings_ops.adjust_career_total(
            conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
            amount=Decimal("5.00"), note="   ",
            kind=earnings_ops.KIND_ADJUSTMENT, season_id=None, actor_id=5,
        )


async def test_race_salary_cannot_be_written_by_hand(pg_conn_migrated):
    """
    Race salary is the machine's to write. Allowing it from the admin
    path would break the `(contract_id, round_id)` idempotency the
    re-import guard depends on.
    """
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    with pytest.raises(earnings_ops.EarningsError):
        await earnings_ops.adjust_career_total(
            conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
            amount=Decimal("5.00"), note="sneaking one in",
            kind=earnings_ops.KIND_RACE_SALARY, season_id=None, actor_id=5,
        )


async def test_history_reads_newest_first(pg_conn_migrated):
    conn = pg_conn_migrated
    ctx = await _bootstrap(conn)
    await earnings_ops.adjust_career_total(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
        amount=Decimal("10.00"), note="first", kind=earnings_ops.KIND_CARRY_IN,
        season_id=None, actor_id=5,
    )
    await earnings_ops.adjust_career_total(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER,
        amount=Decimal("1.00"), note="second",
        kind=earnings_ops.KIND_ADJUSTMENT, season_id=None, actor_id=5,
    )
    rows = await queries.fetch_driver_earnings_history(
        conn, guild_id=ctx["guild_id"], member_id=_MEMBER, limit=10
    )
    assert [r.note for r in rows] == ["second", "first"]


# ── receipt and leaderboard rendering (no database) ──────────────────


def _outcome(**kw):
    from bot import workflow
    return workflow.ImportOutcome(
        tier_code="t1", round_label="R1", round_order=1, written=1, **kw
    )


async def test_the_receipt_says_no_team_budget_was_touched():
    """
    The most likely misreading of this feature is that the league now
    pays salaries twice. The receipt has to say outright that it does
    not, on the line where the money appears.
    """
    from bot.ui import receipts

    outcome = _outcome(
        earnings=earnings_ops.RoundEarningsOutcome(
            round_id=1,
            credits=(
                earnings_ops.EarningCredit(
                    member_id=_MEMBER,
                    driver_id=1,
                    contract_id=1,
                    team_id=1,
                    amount=Decimal("1.00"),
                ),
            ),
            total_paid=Decimal("1.00"),
            already_paid=0,
            too_small_to_pay=0,
        ),
    )
    lines = receipts.render_earnings_outcome(outcome)
    assert len(lines) == 1
    assert "+$1.00M" in lines[0]
    assert "no team budget is touched" in lines[0]


async def test_the_receipt_is_silent_when_nobody_was_paid():
    from bot.ui import receipts

    assert receipts.render_earnings_outcome(_outcome()) == []
    empty = earnings_ops.RoundEarningsOutcome(
        round_id=1, credits=(), total_paid=Decimal("0"),
        already_paid=0, too_small_to_pay=0,
    )
    assert receipts.render_earnings_outcome(_outcome(earnings=empty)) == []


async def test_the_leaderboard_numbers_ranks_across_pages():
    """
    Page 2 must start at 16, not restart at 1 — the commonest paging
    bug, and one a reader cannot detect from a single page.
    """
    from bot.market import render as market_render
    from bot.models import CareerEarnings

    rows = [
        CareerEarnings(
            member_id=1000 + i,
            total=Decimal("100.00") - Decimal(i),
            display_name=f"D{i}",
            races_paid=1,
            seasons_paid=1,
        )
        for i in range(20)
    ]
    page2 = market_render.render_earnings_leaderboard(
        rows=rows, page=2, season_label=None
    )
    assert page2.description.startswith("16. D15")
    assert "Page 2/2" in page2.footer.text


async def test_the_leaderboard_prefers_a_live_discord_name():
    from bot.market import render as market_render
    from bot.models import CareerEarnings

    rows = [
        CareerEarnings(
            member_id=7,
            total=Decimal("5.00"),
            display_name="OldName",
            races_paid=1,
            seasons_paid=1,
        )
    ]
    embed = market_render.render_earnings_leaderboard(
        rows=rows, page=1, season_label=None, names={7: "NewName"}
    )
    assert "NewName" in embed.description
    assert "OldName" not in embed.description
