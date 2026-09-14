"""
Extension: approving an offer with offer_kind='extension' updates the
existing active contract in place instead of creating a new one.
CLAUDE.md §2 rule 6 — Contract Value resets only on
extension/renegotiation.

Promotion/relegation: driver + active contract move to the new tier
together; a ledger entry captures the tier change.
"""

from decimal import Decimal

import pytest

from bot import queries
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
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, t1,
        member_id=111, display_name="Driver", status="active",
    )
    return {
        "season_id": season_id, "t1": t1, "t2": t2,
        "team_id": team_id, "driver_id": driver_id,
    }


# ── extension ────────────────────────────────────────────────────────


async def test_extension_updates_existing_contract_in_place(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    original_contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["t1"],
        driver_id=ctx["driver_id"], team_id=ctx["team_id"],
        contract_value=Decimal("5.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
        external_ref="ORIGINAL",
    )
    offer_id = await service.submit_offer(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["t1"],
        driver_id=ctx["driver_id"], team_id=ctx["team_id"],
        offered_by=100, offer_kind="extension",
        salary=Decimal("8.00"), term_seasons=3,
        contract_type="standard",
        signing_bonus=Decimal("0.50"),
        incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)
    result = await service.commissioner_approve(
        pg_conn_migrated, offer_id, actor_id=999,
        value_at_signing=Decimal("10.00"),
    )
    # Same contract row, updated terms — NOT a new contract id.
    assert result.contract_id == original_contract_id
    contract = await queries.fetch_contract_by_id(
        pg_conn_migrated, original_contract_id,
    )
    assert contract.state == "active"
    assert contract.contract_value == Decimal("8.00")
    assert contract.term_seasons == 3
    assert contract.signing_bonus == Decimal("0.50")
    # External ref preserved from the original.
    assert contract.external_ref == "ORIGINAL"

    # No second active row was created for this driver.
    contracts = await queries.fetch_contract_history_for_driver(
        pg_conn_migrated, ctx["driver_id"],
    )
    active_rows = [c for c in contracts if c.state == "active"]
    assert len(active_rows) == 1

    # Ledger captures the extension with old vs new values.
    ledger = await queries.fetch_ledger_for_contract(
        pg_conn_migrated, original_contract_id,
    )
    ext = next(e for e in ledger if e.kind == "contract_extended")
    assert ext.detail["old_contract_value"] == "5.00"
    assert ext.detail["new_contract_value"] == "8.00"
    assert ext.detail["old_term_seasons"] == 1
    assert ext.detail["new_term_seasons"] == 3


async def test_extension_without_active_contract_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    # No active contract exists — extension is a nonsense path.
    offer_id = await service.submit_offer(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["t1"],
        driver_id=ctx["driver_id"], team_id=ctx["team_id"],
        offered_by=100, offer_kind="extension",
        salary=Decimal("8.00"), term_seasons=3,
        contract_type="standard",
        signing_bonus=Decimal("0"),
        incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    await service.driver_accept(pg_conn_migrated, offer_id, actor_id=111)
    with pytest.raises(service.TransitionError):
        await service.commissioner_approve(
            pg_conn_migrated, offer_id, actor_id=999,
            value_at_signing=None,
        )


# ── promotion / relegation ───────────────────────────────────────────


async def test_promotion_moves_driver_and_contract(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    contract_id = await queries.insert_contract(
        pg_conn_migrated,
        season_id=ctx["season_id"], tier_id=ctx["t1"],
        driver_id=ctx["driver_id"], team_id=ctx["team_id"],
        contract_value=Decimal("5.00"),
        signing_bonus=Decimal("0"), max_incentives=Decimal("0"),
        term_seasons=1, contract_type="standard", state="active",
        value_at_signing=None, approved_by=None,
    )
    await service.move_driver_between_tiers(
        pg_conn_migrated, ctx["driver_id"],
        new_tier_id=ctx["t2"], actor_id=999, note="relegated",
    )
    driver_row = await pg_conn_migrated.fetchrow(
        "SELECT tier_id FROM drivers WHERE id = $1", ctx["driver_id"],
    )
    contract = await queries.fetch_contract_by_id(pg_conn_migrated, contract_id)
    assert driver_row["tier_id"] == ctx["t2"]
    assert contract.tier_id == ctx["t2"]
    # Contract value untouched.
    assert contract.contract_value == Decimal("5.00")

    # Ledger captured the change.
    ledger = await queries.fetch_ledger_for_driver(
        pg_conn_migrated, ctx["driver_id"], limit=5,
    )
    tier_change = next(
        e for e in ledger
        if e.kind == "status_change" and e.detail.get("event") == "tier_change"
    )
    assert tier_change.detail["from_tier_id"] == ctx["t1"]
    assert tier_change.detail["to_tier_id"] == ctx["t2"]


async def test_promotion_without_active_contract_still_moves_driver(
    pg_conn_migrated,
):
    ctx = await _bootstrap(pg_conn_migrated)
    await service.move_driver_between_tiers(
        pg_conn_migrated, ctx["driver_id"],
        new_tier_id=ctx["t2"], actor_id=999,
    )
    driver_row = await pg_conn_migrated.fetchrow(
        "SELECT tier_id FROM drivers WHERE id = $1", ctx["driver_id"],
    )
    assert driver_row["tier_id"] == ctx["t2"]


async def test_promotion_to_same_tier_raises(pg_conn_migrated):
    ctx = await _bootstrap(pg_conn_migrated)
    with pytest.raises(service.TransitionError):
        await service.move_driver_between_tiers(
            pg_conn_migrated, ctx["driver_id"],
            new_tier_id=ctx["t1"], actor_id=999,
        )
