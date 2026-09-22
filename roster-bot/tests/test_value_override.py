"""
Manual per-driver value overrides.

The engine is the only thing that could price a driver, which left no
way to correct one wrong number without editing the race results that
produced it. An override fixes that, and the interesting risk is not
the arithmetic — it is that an override is stored as a *valuation run*,
and every board renders all rows of the tier's latest published run.

So the load-bearing property, which most of these tests exist to pin,
is that an override re-states the entire tier. Get that wrong and
repricing one driver deletes everyone else from the market board.
"""

from decimal import Decimal

import pytest

from bot import queries, workflow
from bot.cogs import admin_market
from bot.market import valuation as valuation_engine
from bot.presets import f1 as f1_preset
from bot.ui import drivers_screen

GUILD = 8484


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


@pytest.fixture
def no_boards(monkeypatch):
    """Board refresh needs a Discord client; the DB effects are the point."""
    async def _noop(*a, **kw):
        return None

    monkeypatch.setattr(
        workflow.market_boards, "refresh_boards_for_tier", _noop
    )


async def _season(conn, *, name="S-override"):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) "
        "VALUES ($1, $2, TRUE) RETURNING id",
        GUILD, name,
    )
    await f1_preset.seed_season(conn, season_id)
    return season_id


async def _tier(conn, season_id, code="t1"):
    return await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = $2",
        season_id, code,
    )


async def _driver(conn, season_id, tier_id, *, member_id, name):
    return await queries.insert_driver(
        conn, season_id, tier_id,
        member_id=member_id, display_name=name, status="active",
    )


async def _publish_run(conn, season_id, tier_id, values, *, label="R1"):
    """A published computed run giving each driver a value."""
    run_id = await queries.insert_valuation_run(
        conn, season_id=season_id, tier_id=tier_id,
        round_label=label, created_by=1, published=True,
    )
    ranked = sorted(values.items(), key=lambda kv: (-kv[1], kv[0]))
    await queries.insert_driver_valuations(
        conn, run_id,
        [
            {
                "driver_id": did, "market_value": val,
                "previous_value": None, "delta": Decimal("0"),
                "rank_in_tier": i + 1, "capped": False, "breakdown": [],
            }
            for i, (did, val) in enumerate(ranked)
        ],
    )
    return run_id


async def _setup(conn):
    season_id = await _season(conn)
    tier_id = await _tier(conn, season_id)
    a = await _driver(conn, season_id, tier_id, member_id=101, name="Alpha")
    b = await _driver(conn, season_id, tier_id, member_id=102, name="Bravo")
    c = await _driver(conn, season_id, tier_id, member_id=103, name="Charlie")
    await _publish_run(conn, season_id, tier_id, {
        a: Decimal("20.00"), b: Decimal("15.00"), c: Decimal("10.00"),
    })
    return season_id, tier_id, a, b, c


# ── the engine ──────────────────────────────────────────────────────


def _carried(pairs):
    return [
        valuation_engine.CarriedValue(
            driver_id=did, display_name=name, market_value=val
        )
        for did, name, val in pairs
    ]


def test_every_carried_driver_survives_the_override():
    # THE test. A run holding only the overridden driver would render a
    # market board with exactly one name on it.
    rows = valuation_engine.build_override_rows(
        carried=_carried([
            (1, "Alpha", Decimal("20.00")),
            (2, "Bravo", Decimal("15.00")),
            (3, "Charlie", Decimal("10.00")),
        ]),
        target_driver_id=2,
        new_value=Decimal("25.00"),
        reason="corrected import",
        actor_id=7,
    )
    assert {r.driver_id for r in rows} == {1, 2, 3}


def test_untouched_drivers_keep_their_value_and_show_no_movement():
    # A zero delta matters: the movers board reads delta, and an
    # override of one driver must not fabricate movement for the rest.
    rows = valuation_engine.build_override_rows(
        carried=_carried([
            (1, "Alpha", Decimal("20.00")),
            (2, "Bravo", Decimal("15.00")),
        ]),
        target_driver_id=2,
        new_value=Decimal("25.00"),
        reason="r", actor_id=7,
    )
    alpha = next(r for r in rows if r.driver_id == 1)
    assert alpha.market_value == Decimal("20.00")
    assert alpha.delta == Decimal("0")


def test_the_overridden_driver_records_a_real_delta():
    rows = valuation_engine.build_override_rows(
        carried=_carried([(2, "Bravo", Decimal("15.00"))]),
        target_driver_id=2,
        new_value=Decimal("25.00"),
        reason="r", actor_id=7,
    )
    bravo = rows[0]
    assert bravo.previous_value == Decimal("15.00")
    assert bravo.delta == Decimal("10.00")


def test_a_downward_override_records_a_negative_delta():
    rows = valuation_engine.build_override_rows(
        carried=_carried([(2, "Bravo", Decimal("15.00"))]),
        target_driver_id=2,
        new_value=Decimal("9.00"),
        reason="r", actor_id=7,
    )
    assert rows[0].delta == Decimal("-6.00")


def test_ranks_are_recomputed_so_the_table_reorders():
    rows = valuation_engine.build_override_rows(
        carried=_carried([
            (1, "Alpha", Decimal("20.00")),
            (2, "Bravo", Decimal("15.00")),
            (3, "Charlie", Decimal("10.00")),
        ]),
        target_driver_id=3,
        new_value=Decimal("30.00"),
        reason="r", actor_id=7,
    )
    by_id = {r.driver_id: r.rank_in_tier for r in rows}
    assert by_id[3] == 1, "the repriced driver should now top the table"
    assert by_id[1] == 2
    assert by_id[2] == 3


def test_ranks_are_contiguous_from_one():
    rows = valuation_engine.build_override_rows(
        carried=_carried([
            (1, "Alpha", Decimal("20.00")),
            (2, "Bravo", Decimal("15.00")),
            (3, "Charlie", Decimal("10.00")),
        ]),
        target_driver_id=2,
        new_value=Decimal("11.00"),
        reason="r", actor_id=7,
    )
    assert sorted(r.rank_in_tier for r in rows) == [1, 2, 3]


def test_a_driver_with_no_prior_value_is_added():
    rows = valuation_engine.build_override_rows(
        carried=_carried([(1, "Alpha", Decimal("20.00"))]),
        target_driver_id=99,
        new_value=Decimal("5.00"),
        reason="r", actor_id=7,
    )
    newcomer = next(r for r in rows if r.driver_id == 99)
    assert newcomer.previous_value is None
    assert newcomer.delta == Decimal("0"), (
        "a first value is not movement — it would top the movers board"
    )


def test_the_override_is_not_clipped_by_anything():
    # The whole point: the engine's caps are what the admin is
    # overriding, so a huge jump must land in full.
    rows = valuation_engine.build_override_rows(
        carried=_carried([(2, "Bravo", Decimal("1.00"))]),
        target_driver_id=2,
        new_value=Decimal("500.00"),
        reason="r", actor_id=7,
    )
    assert rows[0].market_value == Decimal("500.00")
    assert rows[0].capped is False


def test_the_reason_and_actor_are_written_into_the_breakdown():
    rows = valuation_engine.build_override_rows(
        carried=_carried([(2, "Bravo", Decimal("15.00"))]),
        target_driver_id=2,
        new_value=Decimal("25.00"),
        reason="bad import, corrected",
        actor_id=4242,
    )
    entry = rows[0].breakdown[0]
    assert entry["factor"] == "manual_override"
    assert entry["reason"] == "bad import, corrected"
    assert entry["actor_id"] == 4242


def test_carried_rows_are_labelled_as_carried_not_as_overrides():
    rows = valuation_engine.build_override_rows(
        carried=_carried([
            (1, "Alpha", Decimal("20.00")),
            (2, "Bravo", Decimal("15.00")),
        ]),
        target_driver_id=2,
        new_value=Decimal("25.00"),
        reason="r", actor_id=7,
    )
    alpha = next(r for r in rows if r.driver_id == 1)
    assert alpha.breakdown[0]["factor"] == "carried_forward"


# ── the workflow, against a real database ───────────────────────────


async def test_the_new_value_is_what_readers_see(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, _t, _a, b, _c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="corrected import", actor_id=7,
    )
    assert await queries.fetch_latest_published_valuation(conn, b) == Decimal("25.00")


async def test_the_market_board_still_shows_everyone(
    pg_conn_migrated, workflow_db, no_boards
):
    # The regression this feature could most easily have caused.
    conn = pg_conn_migrated
    _s, tier_id, _a, _b, _c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="r", actor_id=7,
    )
    rows = await queries.fetch_market_table_for_tier(conn, tier_id)
    assert len(rows) == 3, "repricing one driver emptied the board"


async def test_the_board_reorders_around_the_new_value(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, tier_id, _a, b, _c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("99.00"), reason="r", actor_id=7,
    )
    rows = await queries.fetch_market_table_for_tier(conn, tier_id)
    assert rows[0]["driver_id"] == b


async def test_only_the_repriced_driver_appears_in_movers(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, tier_id, _a, b, _c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="r", actor_id=7,
    )
    risers, fallers = await queries.fetch_movers_for_tier(conn, tier_id, 10)
    assert [r["driver_id"] for r in risers] == [b]
    assert fallers == []


async def test_the_override_is_attributed_and_explained(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, _t, _a, b, _c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="bad import", actor_id=4242,
    )
    history = await queries.fetch_override_history(conn, b, 10)
    assert len(history) == 1
    assert history[0]["override_reason"] == "bad import"
    assert history[0]["created_by"] == 4242


async def test_a_computed_run_is_not_logged_as_an_override(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, _t, a, _b, _c = await _setup(conn)
    assert await queries.fetch_override_history(conn, a, 10) == []


async def test_overrides_stack_and_keep_their_order(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, _t, _a, b, _c = await _setup(conn)
    for amount, why in ((Decimal("25.00"), "first"), (Decimal("30.00"), "second")):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="t1", member_id=102,
            new_value=amount, reason=why, actor_id=7,
        )
    history = await queries.fetch_override_history(conn, b, 10)
    assert [h["override_reason"] for h in history] == ["second", "first"]
    assert await queries.fetch_latest_published_valuation(conn, b) == Decimal("30.00")


async def test_a_second_override_carries_the_first_one_forward(
    pg_conn_migrated, workflow_db, no_boards
):
    # Overriding Bravo then Charlie must not roll Bravo back.
    conn = pg_conn_migrated
    _s, _t, _a, b, c = await _setup(conn)
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="one", actor_id=7,
    )
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=103,
        new_value=Decimal("40.00"), reason="two", actor_id=7,
    )
    assert await queries.fetch_latest_published_valuation(conn, b) == Decimal("25.00")
    assert await queries.fetch_latest_published_valuation(conn, c) == Decimal("40.00")


async def test_an_override_in_one_tier_leaves_another_alone(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    season_id, _t1, _a, _b, _c = await _setup(conn)
    t2 = await _tier(conn, season_id, "t2")
    d = await _driver(conn, season_id, t2, member_id=201, name="Delta")
    await _publish_run(conn, season_id, t2, {d: Decimal("7.00")})
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=102,
        new_value=Decimal("25.00"), reason="r", actor_id=7,
    )
    assert await queries.fetch_latest_published_valuation(conn, d) == Decimal("7.00")
    assert len(await queries.fetch_market_table_for_tier(conn, t2)) == 1


async def test_a_driver_with_no_published_value_can_be_priced(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    season_id = await _season(conn)
    tier_id = await _tier(conn, season_id)
    e = await _driver(conn, season_id, tier_id, member_id=301, name="Echo")
    await workflow.apply_value_override(
        None, guild_id=GUILD, tier="t1", member_id=301,
        new_value=Decimal("5.00"), reason="new signing", actor_id=7,
    )
    assert await queries.fetch_latest_published_valuation(conn, e) == Decimal("5.00")


async def test_an_unknown_driver_is_refused(
    pg_conn_migrated, workflow_db, no_boards
):
    await _setup(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError, match="not a driver"):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="t1", member_id=999999,
            new_value=Decimal("5.00"), reason="r", actor_id=7,
        )


async def test_an_unknown_tier_is_refused(
    pg_conn_migrated, workflow_db, no_boards
):
    await _setup(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError, match="No tier"):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="nope", member_id=102,
            new_value=Decimal("5.00"), reason="r", actor_id=7,
        )


async def test_a_negative_value_is_refused(
    pg_conn_migrated, workflow_db, no_boards
):
    await _setup(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError, match="negative"):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="t1", member_id=102,
            new_value=Decimal("-1.00"), reason="r", actor_id=7,
        )


async def test_a_blank_reason_is_refused(
    pg_conn_migrated, workflow_db, no_boards
):
    # The reason is the entire audit trail; without it the run is
    # indistinguishable from engine output.
    await _setup(pg_conn_migrated)
    with pytest.raises(workflow.WorkflowError, match="reason"):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="t1", member_id=102,
            new_value=Decimal("5.00"), reason="   ", actor_id=7,
        )


async def test_a_refused_override_writes_nothing(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, tier_id, _a, b, _c = await _setup(conn)
    before = await queries.fetch_latest_published_run_id(conn, tier_id)
    with pytest.raises(workflow.WorkflowError):
        await workflow.apply_value_override(
            None, guild_id=GUILD, tier="t1", member_id=102,
            new_value=Decimal("5.00"), reason="", actor_id=7,
        )
    assert await queries.fetch_latest_published_run_id(conn, tier_id) == before
    assert await queries.fetch_latest_published_valuation(conn, b) == Decimal("15.00")


# ── the preview ─────────────────────────────────────────────────────


async def test_the_preview_changes_nothing(
    pg_conn_migrated, workflow_db, no_boards
):
    conn = pg_conn_migrated
    _s, tier_id, _a, b, _c = await _setup(conn)
    before = await queries.fetch_latest_published_run_id(conn, tier_id)
    await workflow.preview_value_override(
        guild_id=GUILD, tier="t1", member_id=102, new_value=Decimal("25.00")
    )
    assert await queries.fetch_latest_published_run_id(conn, tier_id) == before
    assert await queries.fetch_latest_published_valuation(conn, b) == Decimal("15.00")


async def test_the_preview_reports_the_change(
    pg_conn_migrated, workflow_db, no_boards
):
    await _setup(pg_conn_migrated)
    p = await workflow.preview_value_override(
        guild_id=GUILD, tier="t1", member_id=102, new_value=Decimal("25.00")
    )
    assert p.current_value == Decimal("15.00")
    assert p.delta == Decimal("10.00")
    assert p.is_first_value is False


async def test_a_big_jump_is_flagged_but_not_blocked(
    pg_conn_migrated, workflow_db, no_boards
):
    await _setup(pg_conn_migrated)
    p = await workflow.preview_value_override(
        guild_id=GUILD, tier="t1", member_id=102, new_value=Decimal("120.00")
    )
    assert any("weekly cap" in w for w in p.warnings)


async def test_a_driver_under_contract_is_flagged(
    pg_conn_migrated, workflow_db, no_boards
):
    # Repricing changes the P/L their team settles at — quiet money
    # consequences deserve a loud warning.
    conn = pg_conn_migrated
    season_id, tier_id, _a, b, _c = await _setup(conn)
    team_id = await conn.fetchval(
        "INSERT INTO teams (guild_id, key, name, tier_id, team_role_id, "
        "channel_id) VALUES ($1, 'wil', 'Williams', $2, 555, 556) "
        "RETURNING id",
        GUILD, tier_id,
    )
    await queries.insert_contract(
        conn,
        season_id=season_id, tier_id=tier_id, driver_id=b, team_id=team_id,
        contract_value=Decimal("12.00"), signing_bonus=Decimal("0"),
        max_incentives=Decimal("0"), term_seasons=1,
        contract_type="standard", state="active",
        value_at_signing=Decimal("15.00"), approved_by=7,
    )
    p = await workflow.preview_value_override(
        guild_id=GUILD, tier="t1", member_id=102, new_value=Decimal("25.00")
    )
    assert any("under contract" in w for w in p.warnings)


async def test_a_first_value_says_so(pg_conn_migrated, workflow_db, no_boards):
    conn = pg_conn_migrated
    season_id = await _season(conn)
    tier_id = await _tier(conn, season_id)
    await _driver(conn, season_id, tier_id, member_id=301, name="Echo")
    p = await workflow.preview_value_override(
        guild_id=GUILD, tier="t1", member_id=301, new_value=Decimal("5.00")
    )
    assert p.is_first_value
    assert p.delta == Decimal("0")


# ── the confirmation embed ──────────────────────────────────────────


def _preview(**kw):
    base = dict(
        driver_id=1, display_name="Bravo", tier_code="t1", tier_label="Tier 1",
        season_name="S9", current_value=Decimal("15.00"),
        new_value=Decimal("25.00"), carried_count=3, warnings=(),
    )
    base.update(kw)
    return workflow.ValueOverridePreview(**base)


def test_the_embed_shows_both_numbers():
    embed = admin_market._value_override_embed(_preview(), reason="why")
    assert "15.00" in embed.description
    assert "25.00" in embed.description


def test_the_embed_always_shows_the_reason():
    embed = admin_market._value_override_embed(
        _preview(), reason="bad import, corrected"
    )
    assert any(
        "bad import, corrected" in f.value for f in embed.fields
    )


def test_the_embed_surfaces_warnings():
    embed = admin_market._value_override_embed(
        _preview(warnings=("Above the maximum salary of $50.00M.",)),
        reason="why",
    )
    assert any("maximum salary" in f.value for f in embed.fields)


def test_the_embed_explains_the_carry_forward():
    # Otherwise this looks like a one-row edit rather than a new run.
    embed = admin_market._value_override_embed(_preview(), reason="why")
    body = " ".join(f.value for f in embed.fields)
    assert "keep their current values" in body


def test_a_long_reason_cannot_overflow_a_field():
    embed = admin_market._value_override_embed(
        _preview(), reason="x" * 5000
    )
    for f in embed.fields:
        assert len(f.value) <= 1024


def test_many_warnings_cannot_overflow_a_field():
    embed = admin_market._value_override_embed(
        _preview(warnings=tuple(f"warning number {i} " * 20 for i in range(30))),
        reason="why",
    )
    for f in embed.fields:
        assert len(f.value) <= 1024


# ── the Drivers panel ───────────────────────────────────────────────


def _panel_driver(**kw):
    base = dict(
        driver_id=1, member_id=2, display_name="Bravo", tier_code="t1",
        tier_label="Tier 1", status="active", active_contract_id=None,
        active_team_name=None, contract_value=None,
        market_value=Decimal("15.00"),
    )
    base.update(kw)
    return workflow.DriverForPanel(**base)


class _FakeParent:
    opener_id = 9

    async def reload(self, interaction, **kw):
        return None


def _detail_view():
    return drivers_screen._DriverDetailView(
        detail=_panel_driver(), opener_id=9, parent=_FakeParent()
    )


def test_the_panel_offers_set_value():
    # The user asked for every workflow to be reachable from a GUI
    # panel, so a command-only override would be half a feature.
    labels = [getattr(c, "label", None) for c in _detail_view().children]
    assert "Set value" in labels


def test_the_detail_view_still_fits_a_discord_row():
    # Row 0 is now full at five buttons; a sixth would raise at runtime,
    # inside a panel that is otherwise working.
    rows: dict[int, int] = {}
    for child in _detail_view().children:
        rows[child.row] = rows.get(child.row, 0) + 1
    assert all(count <= 5 for count in rows.values())


def test_the_modal_asks_for_value_and_reason():
    modal = drivers_screen._SetValueModal(_detail_view())
    assert len(modal.children) == 2
    assert len(modal.children) <= 5


def test_the_modal_shows_the_current_value_as_a_hint():
    modal = drivers_screen._SetValueModal(_detail_view())
    assert "15.00" in modal.value_m.placeholder


def test_the_modal_hints_an_example_when_there_is_no_value_yet():
    view = drivers_screen._DriverDetailView(
        detail=_panel_driver(market_value=None),
        opener_id=9,
        parent=_FakeParent(),
    )
    modal = drivers_screen._SetValueModal(view)
    assert "20.75" in modal.value_m.placeholder


def test_the_panel_embed_shows_the_move():
    embed = drivers_screen._build_set_value_embed(_preview(), reason="why")
    assert "15.00" in embed.description
    assert "25.00" in embed.description


def test_the_panel_embed_survives_hostile_lengths():
    embed = drivers_screen._build_set_value_embed(
        _preview(warnings=("w" * 3000,)), reason="r" * 3000
    )
    assert len(embed.description) <= 4096
    for field in embed.fields:
        assert len(field.value) <= 1024
