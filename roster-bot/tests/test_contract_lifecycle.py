"""
Contract state-machine tests: every legal transition succeeds, every
illegal one raises TransitionError; accepted terms are immutable
(they land verbatim on the contracts row); expiry is idempotent both
via the loop and via the lazy-check-on-read path.

Uses the DB fixture — the service layer takes an asyncpg.Connection
and the queries it calls hit real SQL, so a fake conn would only
prove that we know how to mock. Persistence bugs matter more.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.presets import f1 as f1_preset


async def _bootstrap(pg_conn_migrated):
    guild_id = 42
    season_id = await pg_conn_migrated.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S', TRUE) "
        "RETURNING id", guild_id,
    )
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    tier_id = await pg_conn_migrated.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id,
    )
    team_id = await pg_conn_migrated.fetchval(
        """
        INSERT INTO teams (guild_id, key, name, team_role_id, channel_id)
        VALUES ($1, 'rb', 'Red Bull', 200, 300) RETURNING id
        """,
        guild_id,
    )
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=111, display_name="Test Driver", status="active",
    )
    return season_id, tier_id, team_id, driver_id


async def _submit(pg_conn_migrated, *, season_id, tier_id, team_id, driver_id,
                  actor_id=100, salary=Decimal("5.00"), term=2,
                  ttl_hours=48):
    return await service.submit_offer(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=actor_id, offer_kind="new",
        salary=salary, term_seasons=term,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=ttl_hours, validation={"ok": True},
    )


# ── legal transitions ────────────────────────────────────────────────


async def test_full_happy_path_new_signing(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    # Driver accepts, commissioner approves.
    await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)
    approval = await service.commissioner_approve(
        pg_conn_migrated, offer_id, actor_id=999,
        value_at_signing=Decimal("6.50"),
    )
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, approval.contract_id)
    assert contract is not None
    assert contract.state == "active"
    assert contract.contract_value == Decimal("5.00")   # immutable — the offer's salary
    assert contract.value_at_signing == Decimal("6.50")
    assert contract.term_seasons == 2
    # Offer terminal.
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.state == "approved"
    # Ledger records the money mutations.
    ledger = await queries.fetch_ledger_for_driver(pg_conn_migrated, driver_id, limit=10)
    kinds = [entry.kind for entry in ledger]
    assert "contract_signed" in kinds
    assert "offer_approved" in kinds
    assert "offer_accepted" in kinds
    assert "offer_created" in kinds


async def test_driver_decline_ends_negotiation(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    await service.driver_decline(pg_conn_migrated, offer_id, actor_id=111, note="nope")
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.state == "declined"


async def test_team_withdraw_frees_the_driver(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    await service.team_withdraw(pg_conn_migrated, offer_id, actor_id=100)
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.state == "withdrawn"

    # Another team can now open an offer to this driver.
    other_team_id = await pg_conn_migrated.fetchval(
        """
        INSERT INTO teams (guild_id, key, name, team_role_id, channel_id)
        VALUES (42, 'mer', 'Mercedes', 400, 500) RETURNING id
        """
    )
    other_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=other_team_id, driver_id=driver_id, actor_id=101,
    )
    assert other_id > 0


async def test_counter_chain_creates_child_and_marks_parent_countered(
    pg_conn_migrated,
):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    parent_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id, salary=Decimal("5.00"),
    )
    child_id = await service.driver_counter(
        pg_conn_migrated, parent_id, actor_id=111,
        salary=Decimal("6.00"), term_seasons=3,
        signing_bonus=Decimal("0.50"),
        incentives=None, message="pay me",
        ttl_hours=24, validation={"ok": True},
    )
    parent = await queries.fetch_offer_by_id(pg_conn_migrated, parent_id)
    child = await queries.fetch_offer_by_id(pg_conn_migrated, child_id)
    assert parent.state == "countered"
    assert child.state == "pending_team"
    assert child.parent_offer_id == parent_id
    assert child.salary == Decimal("6.00")

    # Team counters back.
    grandchild_id = await service.team_counter(
        pg_conn_migrated, child_id, actor_id=100,
        salary=Decimal("5.50"), term_seasons=3,
        signing_bonus=Decimal("0.25"),
        incentives=None, message=None,
        ttl_hours=24, validation={"ok": True},
    )
    child_after = await queries.fetch_offer_by_id(pg_conn_migrated, child_id)
    grandchild = await queries.fetch_offer_by_id(pg_conn_migrated, grandchild_id)
    assert child_after.state == "countered"
    assert grandchild.state == "pending_driver"
    assert grandchild.parent_offer_id == child_id


# ── illegal transitions raise ────────────────────────────────────────


async def test_accept_from_terminal_state_raises(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    await service.driver_decline(pg_conn_migrated, offer_id, actor_id=111)
    with pytest.raises(service.TransitionError):
        await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)


async def test_approve_before_driver_acceptance_raises(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    with pytest.raises(service.TransitionError):
        await service.commissioner_approve(
            pg_conn_migrated, offer_id, actor_id=999, value_at_signing=None,
        )


async def test_counter_on_pending_team_offer_raises_for_driver(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    parent_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    await service.driver_counter(
        pg_conn_migrated, parent_id, actor_id=111,
        salary=Decimal("6.00"), term_seasons=3,
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=24, validation={"ok": True},
    )
    # The child is now pending_team; driver_counter on it must fail
    # (driver already countered — team's turn now).
    children = await queries.fetch_open_offers_for_team(pg_conn_migrated, team_id)
    child = next(o for o in children if o.state == "pending_team")
    with pytest.raises(service.TransitionError):
        await service.driver_counter(
            pg_conn_migrated, child.id, actor_id=111,
            salary=Decimal("7"), term_seasons=3,
            signing_bonus=Decimal("0"), incentives=None, message=None,
            ttl_hours=24, validation={"ok": True},
        )


async def test_void_non_active_contract_raises(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        contract_value=Decimal("5.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="expired",
        value_at_signing=None, approved_by=None,
    )
    with pytest.raises(service.TransitionError):
        await service.void_contract(
            pg_conn_migrated, contract_id, actor_id=999, note="test",
        )


# ── accepted terms are immutable on the contract row ─────────────────


async def test_contract_row_matches_offer_terms_verbatim(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
        salary=Decimal("7.25"), term=3,
    )
    await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)
    approval = await service.commissioner_approve(
        pg_conn_migrated, offer_id, actor_id=999,
        value_at_signing=Decimal("10.00"),
    )
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, approval.contract_id)
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert contract.contract_value == offer.salary
    assert contract.term_seasons == offer.term_seasons
    assert contract.contract_type == offer.contract_type
    assert contract.signing_bonus == offer.signing_bonus


# ── unique-active-contract per driver enforced ───────────────────────


async def test_second_active_contract_for_same_driver_is_rejected(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    await queries.insert_contract(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        contract_value=Decimal("5.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    import asyncpg
    with pytest.raises(asyncpg.UniqueViolationError):
        await queries.insert_contract(
            pg_conn_migrated,
            season_id=season_id, tier_id=tier_id,
            driver_id=driver_id, team_id=team_id,
            contract_value=Decimal("6.00"),
            signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
            term_seasons=1, contract_type="standard", state="active",
            value_at_signing=None, approved_by=None,
        )


# ── expiry: loop + lazy ───────────────────────────────────────────────


async def test_expire_loop_flips_past_ttl_offers(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    # Backdate expires_at.
    past = datetime.now(UTC) - timedelta(hours=1)
    await pg_conn_migrated.execute(
        "UPDATE contract_offers SET expires_at = $1 WHERE id = $2",
        past, offer_id,
    )
    count = await service.expire_all_past_ttl(pg_conn_migrated)
    assert count == 1
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.state == "expired"


async def test_expire_is_idempotent(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    await pg_conn_migrated.execute(
        "UPDATE contract_offers SET expires_at = $1 WHERE id = $2",
        past, offer_id,
    )
    assert await service.expire_offer(pg_conn_migrated, offer_id) is True
    assert await service.expire_offer(pg_conn_migrated, offer_id) is False


async def test_lazy_expiry_on_read_transitions_and_raises(pg_conn_migrated):
    season_id, tier_id, team_id, driver_id = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, season_id=season_id, tier_id=tier_id,
        team_id=team_id, driver_id=driver_id,
    )
    past = datetime.now(UTC) - timedelta(hours=1)
    await pg_conn_migrated.execute(
        "UPDATE contract_offers SET expires_at = $1 WHERE id = $2",
        past, offer_id,
    )
    # Accepting a past-TTL offer must fail — and the offer must be
    # marked expired by the lazy check on the way through.
    with pytest.raises(service.TransitionError):
        await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.state == "expired"
