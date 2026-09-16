"""
Audit items G4, G6 and G7.

G4 — a tier move onto a tier the member already occupies collided with
`drivers` UNIQUE (season_id, tier_id, member_id) and surfaced as a raw
asyncpg UniqueViolationError. Nothing converted it, so all three entry
points reported a database error with no plain-language reason.

G6 — void settled the money but never dropped the Discord team role, so
the driver still looked signed. Release and buyout both drop it.

G7 — void lacked the open-trade guard release has, so a commissioner
could void a contract a pending trade depended on, leaving the trade
unresolvable by any button in the queue.
"""

from decimal import Decimal

import pytest

from bot import queries, roster_ops
from bot.contracts import service
from bot.presets import f1 as f1_preset


async def _bootstrap(pg_conn_migrated):
    season_id = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S', TRUE) "
        "RETURNING id",
    )
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    t1 = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    t2 = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't2'", season_id,
    )
    team_id = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES (1, 'a', 'A', 200, 300) RETURNING id",
    )
    other_team_id = await pg_conn_migrated.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES (1, 'b', 'B', 201, 301) RETURNING id",
    )
    return {
        "season_id": season_id, "t1": t1, "t2": t2,
        "team_id": team_id, "other_team_id": other_team_id,
    }


async def _driver(conn, ctx, *, tier_id, member_id=111, name="Driver"):
    return await queries.insert_driver(
        conn, ctx["season_id"], tier_id,
        member_id=member_id, display_name=name, status="active",
    )


async def _contract(conn, ctx, *, driver_id, tier_id, team_id=None, value="10.00"):
    return await queries.insert_contract(
        conn,
        season_id=ctx["season_id"], tier_id=tier_id,
        driver_id=driver_id, team_id=team_id or ctx["team_id"],
        contract_value=Decimal(value),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=2, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )


# ── G4: tier move onto an occupied tier ──────────────────────────────


async def test_tier_move_onto_occupied_tier_is_a_clean_refusal(pg_conn_migrated):
    """The bug: this raised asyncpg.UniqueViolationError, not TransitionError."""
    ctx = await _bootstrap(pg_conn_migrated)
    # Same member holds a seat in both tiers — legal, and the setup the
    # UNIQUE constraint exists to protect.
    t2_seat = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t2"])
    t1_seat = await _driver(
        pg_conn_migrated, ctx, tier_id=ctx["t1"], name="Driver",
    )
    with pytest.raises(service.TransitionError) as excinfo:
        await service.move_driver_between_tiers(
            pg_conn_migrated, t2_seat, new_tier_id=ctx["t1"], actor_id=9,
        )
    message = str(excinfo.value)
    assert "already has an entry" in message
    # Must name the other row so the commissioner can go and act on it.
    assert str(t1_seat) in message


async def test_refusal_names_the_tier_in_words(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    t2_seat = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t2"])
    await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    tier = await queries.fetch_tier_by_id(pg_conn_migrated, ctx["t1"])
    with pytest.raises(service.TransitionError) as excinfo:
        await service.move_driver_between_tiers(
            pg_conn_migrated, t2_seat, new_tier_id=ctx["t1"], actor_id=9,
        )
    assert tier.label in str(excinfo.value)


async def test_refused_move_changes_nothing(pg_conn_migrated):
    """A refusal must not half-apply: no tier change, no ledger row."""
    ctx = await _bootstrap(pg_conn_migrated)
    t2_seat = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t2"])
    t1_seat = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    before = await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM contract_ledger WHERE driver_id = $1", t2_seat,
    )
    with pytest.raises(service.TransitionError):
        await service.move_driver_between_tiers(
            pg_conn_migrated, t2_seat, new_tier_id=ctx["t1"], actor_id=9,
        )
    moved = await queries.fetch_driver_by_id(pg_conn_migrated, t2_seat)
    stayed = await queries.fetch_driver_by_id(pg_conn_migrated, t1_seat)
    assert moved.tier_id == ctx["t2"]
    assert stayed.tier_id == ctx["t1"]
    after = await pg_conn_migrated.fetchval(
        "SELECT count(*) FROM contract_ledger WHERE driver_id = $1", t2_seat,
    )
    assert after == before


async def test_an_ordinary_promotion_still_works(pg_conn_migrated):
    """Regression guard: the new check must not block the normal case."""
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t2"])
    contract_id = await _contract(
        pg_conn_migrated, ctx, driver_id=driver_id, tier_id=ctx["t2"],
    )
    await service.move_driver_between_tiers(
        pg_conn_migrated, driver_id, new_tier_id=ctx["t1"], actor_id=9,
    )
    driver = await queries.fetch_driver_by_id(pg_conn_migrated, driver_id)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert driver.tier_id == ctx["t1"]
    assert contract.tier_id == ctx["t1"]


async def test_a_different_member_in_the_target_tier_is_not_a_clash(pg_conn_migrated):
    """The constraint is per member. Someone else's seat is irrelevant."""
    ctx = await _bootstrap(pg_conn_migrated)
    mover = await _driver(
        pg_conn_migrated, ctx, tier_id=ctx["t2"], member_id=111, name="Mover",
    )
    await _driver(
        pg_conn_migrated, ctx, tier_id=ctx["t1"], member_id=222, name="Sitter",
    )
    await service.move_driver_between_tiers(
        pg_conn_migrated, mover, new_tier_id=ctx["t1"], actor_id=9,
    )
    driver = await queries.fetch_driver_by_id(pg_conn_migrated, mover)
    assert driver.tier_id == ctx["t1"]


async def test_moving_to_the_same_tier_still_refuses_first(pg_conn_migrated):
    """The pre-existing guard must keep its own message, not the new one."""
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    with pytest.raises(service.TransitionError) as excinfo:
        await service.move_driver_between_tiers(
            pg_conn_migrated, driver_id, new_tier_id=ctx["t1"], actor_id=9,
        )
    assert "already in tier" in str(excinfo.value)


# ── G7: void must respect open trades ────────────────────────────────


async def _trade_over(conn, ctx, contract_id):
    other_driver = await _driver(
        conn, ctx, tier_id=ctx["t1"], member_id=222, name="Other",
    )
    other_contract = await _contract(
        conn, ctx, driver_id=other_driver, tier_id=ctx["t1"],
        team_id=ctx["other_team_id"], value="12.00",
    )
    return await service.propose_trade(
        conn,
        season_id=ctx["season_id"],
        proposing_team_id=ctx["team_id"],
        other_team_id=ctx["other_team_id"],
        proposed_by=100,
        items=[
            (ctx["team_id"], contract_id),
            (ctx["other_team_id"], other_contract),
        ],
        message=None, ttl_hours=24,
    )


async def test_void_blocked_when_contract_in_open_trade(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    contract_id = await _contract(
        pg_conn_migrated, ctx, driver_id=driver_id, tier_id=ctx["t1"],
    )
    await _trade_over(pg_conn_migrated, ctx, contract_id)
    with pytest.raises(service.TransitionError) as excinfo:
        await service.void_contract(pg_conn_migrated, contract_id, actor_id=9)
    assert "open trade" in str(excinfo.value)


async def test_blocked_void_leaves_the_contract_active(pg_conn_migrated):
    """The refusal must come before any write, as it does for release."""
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    contract_id = await _contract(
        pg_conn_migrated, ctx, driver_id=driver_id, tier_id=ctx["t1"],
    )
    await _trade_over(pg_conn_migrated, ctx, contract_id)
    with pytest.raises(service.TransitionError):
        await service.void_contract(pg_conn_migrated, contract_id, actor_id=9)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert contract.state == "active"
    kinds = await pg_conn_migrated.fetch(
        "SELECT kind FROM contract_ledger WHERE contract_id = $1", contract_id,
    )
    assert "contract_voided" not in [k["kind"] for k in kinds]


async def test_void_allowed_once_the_trade_is_resolved(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    contract_id = await _contract(
        pg_conn_migrated, ctx, driver_id=driver_id, tier_id=ctx["t1"],
    )
    trade_id = await _trade_over(pg_conn_migrated, ctx, contract_id)
    await pg_conn_migrated.execute(
        "UPDATE trades SET state = 'withdrawn' WHERE id = $1", trade_id,
    )
    await service.void_contract(pg_conn_migrated, contract_id, actor_id=9)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert contract.state == "voided"


async def test_void_with_no_trade_is_unaffected(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    driver_id = await _driver(pg_conn_migrated, ctx, tier_id=ctx["t1"])
    contract_id = await _contract(
        pg_conn_migrated, ctx, driver_id=driver_id, tier_id=ctx["t1"],
    )
    await service.void_contract(pg_conn_migrated, contract_id, actor_id=9)
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert contract.state == "voided"


# ── G6: the role drop ────────────────────────────────────────────────


class _FakeTeam:
    def __init__(self) -> None:
        self.name = "Williams"
        self.team_role_id = 200


class _FakeGuild:
    def __init__(self, member) -> None:
        self._member = member

    def get_member(self, member_id):  # noqa: ARG002
        return self._member


async def test_role_drop_reports_failure_instead_of_raising(monkeypatch):
    """
    The money is already committed when this runs, so a role failure has
    to be reported rather than raised — raising would strand the caller
    after a settled void.
    """
    async def _boom(**kwargs):
        raise roster_ops.RoleAssignmentError("missing Manage Roles")

    monkeypatch.setattr(roster_ops, "drop_from_team", _boom)
    failure = await roster_ops.drop_from_team_best_effort(
        guild=_FakeGuild(object()), member_id=1, team=_FakeTeam(),
        actor=object(), reason="voided",
    )
    assert failure == "missing Manage Roles"


async def test_role_drop_returns_none_on_success(monkeypatch):
    seen = {}

    async def _ok(**kwargs):
        seen.update(kwargs)

    monkeypatch.setattr(roster_ops, "drop_from_team", _ok)
    failure = await roster_ops.drop_from_team_best_effort(
        guild=_FakeGuild(object()), member_id=1, team=_FakeTeam(),
        actor=object(), reason="Contract 5 voided",
    )
    assert failure is None
    # The audit reason has to reach Discord, or the role log says nothing.
    assert seen["reason"] == "Contract 5 voided"


async def test_role_drop_is_a_no_op_when_the_driver_has_left(monkeypatch):
    """A member who has left the guild has no roles left to strip."""
    called = False

    async def _track(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(roster_ops, "drop_from_team", _track)
    failure = await roster_ops.drop_from_team_best_effort(
        guild=_FakeGuild(None), member_id=1, team=_FakeTeam(),
        actor=object(),
    )
    assert failure is None
    assert called is False


async def test_role_drop_is_a_no_op_without_a_team(monkeypatch):
    called = False

    async def _track(**kwargs):
        nonlocal called
        called = True

    monkeypatch.setattr(roster_ops, "drop_from_team", _track)
    assert await roster_ops.drop_from_team_best_effort(
        guild=_FakeGuild(object()), member_id=1, team=None, actor=object(),
    ) is None
    assert await roster_ops.drop_from_team_best_effort(
        guild=None, member_id=1, team=_FakeTeam(), actor=object(),
    ) is None
    assert called is False


async def test_a_bare_exception_still_yields_a_reason(monkeypatch):
    """`str(exc)` is empty for some exceptions; the caller needs words."""
    async def _boom(**kwargs):
        raise RuntimeError

    monkeypatch.setattr(roster_ops, "drop_from_team", _boom)
    failure = await roster_ops.drop_from_team_best_effort(
        guild=_FakeGuild(object()), member_id=1, team=_FakeTeam(),
        actor=object(),
    )
    assert failure == "RuntimeError"
