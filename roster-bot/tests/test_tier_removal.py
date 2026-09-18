"""
Removing a tier: the rails, and the override that gets past them.

Two distinct dangers, and the tests keep them apart:

  * Tables that reference `tiers(id)` WITHOUT a cascade (contracts,
    offers, the ledger, dead money) make a plain delete raise a
    foreign-key violation. Those are the blockers.
  * Tables that reference it WITH a cascade (drivers, valuations,
    config, boards, results, budgets) are removed silently. Those do
    not block anything, which is exactly why an admin has to be shown
    them first.

Real SQL throughout. The whole feature is about foreign-key behaviour,
so a mocked connection would test nothing worth testing.
"""

from decimal import Decimal

import pytest

from bot import queries, workflow
from bot.cogs import admin_market
from bot.presets import f1 as f1_preset

GUILD = 9191


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    """
    `workflow` opens its own connection via `db.connect()`. Point that
    at the test transaction so the rails and the purge are exercised
    against the same rows the assertions read.
    """
    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated



async def _season(conn, *, name="S-removal"):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, $2, TRUE) RETURNING id",
        GUILD, name,
    )
    await f1_preset.seed_season(conn, season_id)
    return season_id


async def _tier(conn, season_id, code="t3"):
    return await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = $2",
        season_id, code,
    )


async def _bare_tier(conn, season_id, *, code="t9", rank=99):
    """A tier the preset did not create, so nothing references it."""
    return await conn.fetchval(
        "INSERT INTO tiers (season_id, code, label, rank_order) "
        "VALUES ($1, $2, 'Scratch Tier', $3) RETURNING id",
        season_id, code, rank,
    )


async def _team(conn, key="hrt"):
    return await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, team_role_id, channel_id) "
        "VALUES ($1, $2, 'Hispania', 900, 901) RETURNING id",
        GUILD, key,
    )


async def _signed_contract(conn, season_id, tier_id):
    """A driver on a real contract, i.e. a tier with money history."""
    from bot.contracts import service

    team_id = await _team(conn)
    driver_id = await queries.insert_driver(
        conn, season_id, tier_id,
        member_id=5150, display_name="Blocker Driver", status="active",
    )
    offer_id = await service.submit_offer(
        conn,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=1, offer_kind="new",
        salary=Decimal("5.00"), term_seasons=1,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    await service.driver_accept(conn, offer_id, actor_id=5150)
    await service.commissioner_approve(
        conn, offer_id, actor_id=1, value_at_signing=None
    )
    return team_id, driver_id


# ── the preview ──────────────────────────────────────────────────────


async def test_an_empty_tier_reports_nothing_at_risk(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    await _bare_tier(pg_conn_migrated, season_id)
    preview = await workflow.preview_tier_removal(guild_id=GUILD, code="t9")
    assert preview.is_empty
    assert not preview.needs_override
    assert preview.blockers == {}
    assert preview.cascade_losses == {}


async def test_the_preview_counts_what_cascades_away(pg_conn_migrated, workflow_db):
    """
    Drivers do not block a tier delete — they are destroyed by it. If
    the preview stayed silent about them, the rails would be guarding
    the wrong thing.
    """
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    for i in range(3):
        await queries.insert_driver(
            pg_conn_migrated, season_id, tier_id,
            member_id=600 + i, display_name=f"D{i}", status="active",
        )
    preview = await workflow.preview_tier_removal(guild_id=GUILD, code="t9")
    assert preview.cascade_losses.get("drivers") == 3
    # Drivers alone must not demand the override; nothing refuses them.
    assert not preview.needs_override
    assert not preview.is_empty


async def test_the_preview_counts_contract_history_as_a_blocker(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    await _signed_contract(pg_conn_migrated, season_id, tier_id)
    preview = await workflow.preview_tier_removal(guild_id=GUILD, code="t3")
    assert preview.needs_override
    assert preview.blockers.get("contracts") == 1
    assert preview.blockers.get("ledger rows", 0) >= 1


async def test_the_preview_changes_nothing(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=601, display_name="D", status="active",
    )
    await workflow.preview_tier_removal(guild_id=GUILD, code="t9")
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", tier_id
    ) == 1
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM drivers WHERE tier_id = $1", tier_id
    ) == 1


async def test_an_unknown_tier_is_reported_not_crashed(pg_conn_migrated, workflow_db):
    await _season(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError) as caught:
        await workflow.preview_tier_removal(guild_id=GUILD, code="nope")
    assert "nope" in str(caught.value)


# ── removal without the override ─────────────────────────────────────


async def test_an_empty_tier_is_removed(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    label = await workflow.remove_tier(guild_id=GUILD, code="t9")
    assert label == "Scratch Tier"
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", tier_id
    ) == 0


async def test_contract_history_refuses_removal(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    await _signed_contract(pg_conn_migrated, season_id, tier_id)
    with pytest.raises(workflow.WorkflowError) as caught:
        await workflow.remove_tier(guild_id=GUILD, code="t3")
    msg = str(caught.value)
    assert "contracts" in msg
    assert "override" in msg.lower()
    # The refusal must leave the tier standing.
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", tier_id
    ) == 1


async def test_a_refused_removal_does_not_half_delete(pg_conn_migrated, workflow_db):
    """
    The refusal happens inside the transaction that would do the work,
    after teams are unlinked in the non-override path. If the ordering
    were wrong, a refused removal could still detach every team from
    the tier and leave no trace of why.
    """
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    team_id, driver_id = await _signed_contract(
        pg_conn_migrated, season_id, tier_id
    )
    await pg_conn_migrated.execute(
        "UPDATE teams SET tier_id = $1 WHERE id = $2", tier_id, team_id
    )
    with pytest.raises(workflow.WorkflowError):
        await workflow.remove_tier(guild_id=GUILD, code="t3")
    assert await pg_conn_migrated.fetchval(
        "SELECT tier_id FROM teams WHERE id = $1", team_id
    ) == tier_id
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM drivers WHERE id = $1", driver_id
    ) == 1


async def test_removing_a_tier_unlinks_teams_rather_than_deleting_them(
    pg_conn_migrated, workflow_db,
):
    # `teams.tier_id` has no cascade, so a team would block the delete
    # outright. Deleting the team instead would be a catastrophic way to
    # resolve that.
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    team_id = await _team(pg_conn_migrated)
    await pg_conn_migrated.execute(
        "UPDATE teams SET tier_id = $1 WHERE id = $2", tier_id, team_id
    )
    await workflow.remove_tier(guild_id=GUILD, code="t9")
    row = await pg_conn_migrated.fetchrow(
        "SELECT id, tier_id FROM teams WHERE id = $1", team_id
    )
    assert row is not None, "the team was deleted with the tier"
    assert row["tier_id"] is None


async def test_drivers_go_with_the_tier(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=610, display_name="Doomed", status="active",
    )
    await workflow.remove_tier(guild_id=GUILD, code="t9")
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM drivers WHERE id = $1", driver_id
    ) == 0


async def test_other_tiers_are_untouched(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    await _bare_tier(pg_conn_migrated, season_id)
    before = await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE season_id = $1", season_id
    )
    await workflow.remove_tier(guild_id=GUILD, code="t9")
    after = await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE season_id = $1", season_id
    )
    assert after == before - 1


# ── the override ─────────────────────────────────────────────────────


async def test_the_override_removes_a_tier_with_contract_history(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    await _signed_contract(pg_conn_migrated, season_id, tier_id)
    await workflow.remove_tier(guild_id=GUILD, code="t3", override=True)
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", tier_id
    ) == 0


async def test_the_override_clears_every_blocking_table(pg_conn_migrated, workflow_db):
    """
    The point of the purge order. Any table left behind would surface a
    raw ForeignKeyViolationError instead of removing the tier.
    """
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    await _signed_contract(pg_conn_migrated, season_id, tier_id)
    await workflow.remove_tier(guild_id=GUILD, code="t3", override=True)
    for _label, table in queries.TIER_DELETE_BLOCKERS:
        remaining = await pg_conn_migrated.fetchval(
            f"SELECT COUNT(*) FROM {table} WHERE tier_id = $1",  # noqa: S608
            tier_id,
        )
        assert remaining == 0, f"{table} still references the tier"


async def test_the_override_breaks_counter_offer_chains(pg_conn_migrated, workflow_db):
    """
    `contract_offers.parent_offer_id` is a self-reference with no
    cascade. A countered offer is a parent row, so purging a tier that
    saw any negotiation has to unlink the chain before deleting it.
    """
    from bot.contracts import service

    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    team_id = await _team(pg_conn_migrated, key="cnt")
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, tier_id,
        member_id=5151, display_name="Counter Driver", status="active",
    )
    offer_id = await service.submit_offer(
        pg_conn_migrated,
        season_id=season_id, tier_id=tier_id,
        driver_id=driver_id, team_id=team_id,
        offered_by=1, offer_kind="new",
        salary=Decimal("5.00"), term_seasons=1,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    child_id = await service.driver_counter(
        pg_conn_migrated, offer_id,
        actor_id=5151, salary=Decimal("6.00"), term_seasons=1,
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    assert child_id is not None

    await workflow.remove_tier(guild_id=GUILD, code="t3", override=True)
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM contract_offers WHERE tier_id = $1", tier_id
    ) == 0


async def test_the_override_still_spares_teams(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _tier(pg_conn_migrated, season_id)
    team_id, _ = await _signed_contract(pg_conn_migrated, season_id, tier_id)
    await pg_conn_migrated.execute(
        "UPDATE teams SET tier_id = $1 WHERE id = $2", tier_id, team_id
    )
    await workflow.remove_tier(guild_id=GUILD, code="t3", override=True)
    row = await pg_conn_migrated.fetchrow(
        "SELECT id, tier_id FROM teams WHERE id = $1", team_id
    )
    assert row is not None, "the override deleted a team"
    assert row["tier_id"] is None


async def test_the_override_leaves_other_tiers_history_alone(pg_conn_migrated, workflow_db):
    """
    The purge deletes by `tier_id`. A stray unfiltered DELETE would
    take the whole league's contract history with it and this is the
    test that would notice.
    """
    season_id = await _season(pg_conn_migrated)
    doomed = await _tier(pg_conn_migrated, season_id, code="t3")
    keeper = await _tier(pg_conn_migrated, season_id, code="t1")
    await _signed_contract(pg_conn_migrated, season_id, doomed)

    team_id = await _team(pg_conn_migrated, key="keep")
    driver_id = await queries.insert_driver(
        pg_conn_migrated, season_id, keeper,
        member_id=7000, display_name="Survivor", status="active",
    )
    from bot.contracts import service

    keeper_offer = await service.submit_offer(
        pg_conn_migrated,
        season_id=season_id, tier_id=keeper,
        driver_id=driver_id, team_id=team_id,
        offered_by=1, offer_kind="new",
        salary=Decimal("5.00"), term_seasons=1,
        contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    await service.driver_accept(pg_conn_migrated, keeper_offer, actor_id=7000)
    await service.commissioner_approve(
        pg_conn_migrated, keeper_offer, actor_id=1, value_at_signing=None
    )

    await workflow.remove_tier(guild_id=GUILD, code="t3", override=True)

    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM contracts WHERE tier_id = $1", keeper
    ) == 1
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM drivers WHERE id = $1", driver_id
    ) == 1
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", keeper
    ) == 1


async def test_the_override_is_harmless_on_an_empty_tier(pg_conn_migrated, workflow_db):
    season_id = await _season(pg_conn_migrated)
    tier_id = await _bare_tier(pg_conn_migrated, season_id)
    await workflow.remove_tier(guild_id=GUILD, code="t9", override=True)
    assert await pg_conn_migrated.fetchval(
        "SELECT COUNT(*) FROM tiers WHERE id = $1", tier_id
    ) == 0


async def test_the_override_cannot_invent_a_tier(pg_conn_migrated, workflow_db):
    await _season(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError):
        await workflow.remove_tier(guild_id=GUILD, code="ghost", override=True)


# ── the confirmation UI ──────────────────────────────────────────────


def _preview(**kw):
    base = dict(
        tier_id=1, code="t3", label="Tier 3", season_name="S7",
        blockers={}, cascade_losses={}, teams_unlinked=0,
    )
    base.update(kw)
    return workflow.TierRemovalPreview(**base)


def test_the_refusal_embed_names_what_blocks_it():
    embed = admin_market._tier_removal_embed(
        _preview(blockers={"contracts": 4, "ledger rows": 88}),
        override=False,
    )
    body = " ".join(f.value for f in embed.fields)
    assert "4 contracts" in body
    assert "88 ledger rows" in body
    assert "override" in body.lower(), "the way forward is not offered"


def test_the_embed_warns_about_silent_cascade_losses():
    # Drivers do not block anything, so if the embed omitted them an
    # admin would delete twelve drivers without being told.
    embed = admin_market._tier_removal_embed(
        _preview(cascade_losses={"drivers": 12}), override=False
    )
    assert "12 drivers" in " ".join(f.value for f in embed.fields)


def test_the_embed_promises_teams_survive():
    embed = admin_market._tier_removal_embed(
        _preview(teams_unlinked=2), override=False
    )
    body = " ".join(f.value for f in embed.fields)
    assert "unlinked" in body.lower()
    assert "not deleted" in body.lower()


def test_the_override_embed_says_it_is_permanent():
    embed = admin_market._tier_removal_embed(
        _preview(blockers={"contracts": 4}, cascade_losses={"drivers": 2}),
        override=True,
    )
    text = (embed.description or "") + " ".join(
        f.name + f.value for f in embed.fields
    )
    assert "cannot be undone" in text.lower()
    assert "WILL BE DELETED" in text


def test_an_empty_tier_is_not_dressed_up_as_a_catastrophe():
    embed = admin_market._tier_removal_embed(_preview(), override=True)
    assert "empty" in (embed.description or "").lower()
    assert not embed.fields


def test_every_embed_stays_inside_discord_limits():
    big = _preview(
        blockers={label: 99999 for label, _ in queries.TIER_DELETE_BLOCKERS},
        cascade_losses={
            label: 99999 for label, _ in queries.TIER_CASCADE_LOSSES
        },
        teams_unlinked=40,
    )
    for override in (False, True):
        embed = admin_market._tier_removal_embed(big, override=override)
        assert len(embed.title) <= 256
        assert len(embed.description or "") <= 4096
        for f in embed.fields:
            assert len(f.name) <= 256
            assert len(f.value) <= 1024


def test_the_override_button_is_labelled_for_what_it_does():
    view = admin_market._ConfirmRemoveTierView(
        preview=_preview(), override=True, opener_id=1
    )
    labels = [c.label for c in view.children]
    assert "Delete tier and history" in labels
    assert "Cancel" in labels


def test_the_plain_button_does_not_threaten_history():
    view = admin_market._ConfirmRemoveTierView(
        preview=_preview(), override=False, opener_id=1
    )
    labels = [c.label for c in view.children]
    assert "Remove tier" in labels
    assert "Delete tier and history" not in labels


def test_the_modal_asks_for_the_tier_code():
    view = admin_market._ConfirmRemoveTierView(
        preview=_preview(), override=True, opener_id=1
    )
    modal = admin_market._TypeTheCodeModal(preview=_preview(), parent=view)
    assert len(modal.children) == 1
    assert "t3" in modal.children[0].label
