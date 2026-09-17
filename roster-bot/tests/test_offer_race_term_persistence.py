"""
A race term must survive the whole round trip: offer -> accept ->
contract, and through a counter and an extension.

Migration 018 put `term_races` on `contract_offers`. Before it, the
offer stored only seasons and the race term was derived at signing as
`term_seasons x races_per_season`, so the shortest deal anyone could
offer was one full season. A league could set a 5-race minimum and no
Team Principal could offer a 5-race deal.

These tests hit real SQL. The derivation, the NOT NULL default and the
CHECK all live in the migration, so a fake connection would only prove
we know how to mock.
"""

from decimal import Decimal

import pytest

from bot import queries
from bot.contracts import service
from bot.presets import f1 as f1_preset


async def _bootstrap(conn):
    guild_id = 4242
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, 'S-races', TRUE) RETURNING id",
        guild_id,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'",
        season_id,
    )
    team_id = await conn.fetchval(
        """
        INSERT INTO teams (guild_id, key, name, team_role_id, channel_id)
        VALUES ($1, 'mcl', 'McLaren', 201, 301) RETURNING id
        """,
        guild_id,
    )
    driver_id = await queries.insert_driver(
        conn, season_id, tier_id,
        member_id=777, display_name="Race Term Driver", status="active",
    )
    return season_id, tier_id, team_id, driver_id


async def _submit(conn, ids, *, term_seasons=1, term_races=None,
                  salary=Decimal("5.00"), actor_id=100):
    season_id, tier_id, team_id, driver_id = ids
    return await service.submit_offer(
        conn,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=actor_id, offer_kind="new",
        salary=salary, term_seasons=term_seasons,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
        term_races=term_races,
    )


# ── the offer row ────────────────────────────────────────────────────


async def test_a_ten_race_offer_is_stored_as_ten_races(pg_conn_migrated):
    ids = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    assert offer.term_races == 10
    # The season span is what the salary rate is quoted against; a
    # 10-race deal touches one season.
    assert offer.term_seasons == 1


async def test_omitting_the_race_term_derives_it_from_seasons(
    pg_conn_migrated,
):
    # What every pre-018 caller did. Must still describe the same deal
    # the migration's backfill would have produced.
    ids = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(pg_conn_migrated, ids, term_seasons=2)
    offer = await queries.fetch_offer_by_id(pg_conn_migrated, offer_id)
    per_season = await pg_conn_migrated.fetchval(
        "SELECT races_per_season FROM league_config "
        "WHERE season_id = $1 AND tier_id IS NULL",
        ids[0],
    )
    assert offer.term_races == 2 * per_season


async def test_a_zero_race_offer_is_refused_by_the_database(
    pg_conn_migrated,
):
    ids = await _bootstrap(pg_conn_migrated)
    with pytest.raises(Exception) as caught:
        await _submit(pg_conn_migrated, ids, term_seasons=1, term_races=0)
    assert "term_races" in str(caught.value).lower()


# ── offer -> contract ────────────────────────────────────────────────


async def _sign(conn, ids, offer_id, *, actor_id=100):
    driver_id = ids[3]
    await service.driver_accept(conn, offer_id, actor_id=actor_id)
    return await service.commissioner_approve(
        conn, offer_id, actor_id=actor_id, value_at_signing=None,
    ), driver_id


async def test_a_short_race_term_is_not_rounded_up_at_signing(
    pg_conn_migrated,
):
    """
    The bug this guards: `_approve_new_signing` used to let
    `insert_contract` re-derive the race term from `term_seasons`, which
    turned a 10-race deal into a full 24-race season at the moment it
    became a contract.
    """
    ids = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    result, driver_id = await _sign(pg_conn_migrated, ids, offer_id)
    contract = await queries.fetch_active_contract_for_driver(
        pg_conn_migrated, driver_id
    )
    assert contract is not None
    assert contract.term_races == 10, (
        "a 10-race deal became "
        f"{contract.term_races} races at signing"
    )
    assert contract.id == result.contract_id


async def test_a_whole_season_term_still_signs_as_a_whole_season(
    pg_conn_migrated,
):
    ids = await _bootstrap(pg_conn_migrated)
    per_season = await pg_conn_migrated.fetchval(
        "SELECT races_per_season FROM league_config "
        "WHERE season_id = $1 AND tier_id IS NULL",
        ids[0],
    )
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=per_season
    )
    _, driver_id = await _sign(pg_conn_migrated, ids, offer_id)
    contract = await queries.fetch_active_contract_for_driver(
        pg_conn_migrated, driver_id
    )
    assert contract.term_races == per_season


# ── counters ─────────────────────────────────────────────────────────


async def test_a_counter_can_change_the_race_term(pg_conn_migrated):
    ids = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    child_id = await service.driver_counter(
        pg_conn_migrated, offer_id,
        actor_id=777, salary=Decimal("6.00"), term_seasons=1,
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
        term_races=16,
    )
    child = await queries.fetch_offer_by_id(pg_conn_migrated, child_id)
    assert child.term_races == 16


async def test_a_counter_silent_on_term_inherits_the_parents_races(
    pg_conn_migrated,
):
    # Countering on salary alone must not reset a 10-race deal to a
    # whole season.
    ids = await _bootstrap(pg_conn_migrated)
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    child_id = await service.driver_counter(
        pg_conn_migrated, offer_id,
        actor_id=777, salary=Decimal("6.00"), term_seasons=1,
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    child = await queries.fetch_offer_by_id(pg_conn_migrated, child_id)
    assert child.term_races == 10


# ── extensions ───────────────────────────────────────────────────────


async def test_an_extension_actually_changes_the_race_term(
    pg_conn_migrated,
):
    """
    `update_contract_terms` wrote `term_seasons` but never `term_races`,
    so an extension was recorded in the ledger and then ignored: the
    contract kept running to its original race term.
    """
    ids = await _bootstrap(pg_conn_migrated)
    season_id, tier_id, team_id, driver_id = ids
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    await _sign(pg_conn_migrated, ids, offer_id)

    ext_id = await service.submit_offer(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=100, offer_kind="extension",
        salary=Decimal("7.00"), term_seasons=1,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
        term_races=20,
    )
    await service.driver_accept(pg_conn_migrated, ext_id, actor_id=777)
    await service.commissioner_approve(
        pg_conn_migrated, ext_id, actor_id=100, value_at_signing=None,
    )

    contract = await queries.fetch_active_contract_for_driver(
        pg_conn_migrated, driver_id
    )
    assert contract.term_races == 20, (
        "extension left the term at "
        f"{contract.term_races} races"
    )


async def test_the_extension_ledger_records_both_race_terms(
    pg_conn_migrated,
):
    ids = await _bootstrap(pg_conn_migrated)
    season_id, tier_id, team_id, driver_id = ids
    offer_id = await _submit(
        pg_conn_migrated, ids, term_seasons=1, term_races=10
    )
    await _sign(pg_conn_migrated, ids, offer_id)
    ext_id = await service.submit_offer(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=100, offer_kind="extension",
        salary=Decimal("7.00"), term_seasons=1,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
        term_races=20,
    )
    await service.driver_accept(pg_conn_migrated, ext_id, actor_id=777)
    await service.commissioner_approve(
        pg_conn_migrated, ext_id, actor_id=100, value_at_signing=None,
    )

    row = await pg_conn_migrated.fetchrow(
        "SELECT detail FROM contract_ledger "
        "WHERE kind = 'contract_extended' ORDER BY id DESC LIMIT 1"
    )
    assert row is not None
    import json
    detail = row["detail"]
    detail = json.loads(detail) if isinstance(detail, str) else detail
    assert detail["old_term_races"] == 10
    assert detail["new_term_races"] == 20
