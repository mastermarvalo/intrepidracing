"""
The setup and board operations behind both `/market-admin season|tier|
config|board` and the panel's Setup and Boards screens.

`test_status_reads_real_tier_rows` is a regression test. `fetch_league_status`
read `tier.label` through the wrong attribute name, so `/league` raised
AttributeError on any server that had at least one tier — every real
server. The existing panel tests all constructed `TierStatus` by hand and
so never touched the mapping. These tests go through the database.
"""

from decimal import Decimal

import pytest

from bot import queries, workflow
from bot.presets import f1 as f1_preset

GUILD = 4242


class _FakeChannel:
    def __init__(self) -> None:
        self.deleted: list[int] = []

    async def fetch_message(self, message_id):
        return _FakeMessage(self, message_id)


class _FakeMessage:
    def __init__(self, channel, message_id) -> None:
        self._channel = channel
        self._id = message_id

    async def delete(self):
        self._channel.deleted.append(self._id)


class _FakeClient:
    """Stands in for the bot. Board rendering is stubbed separately."""

    def __init__(self) -> None:
        self.channel = _FakeChannel()

    def get_channel(self, _channel_id):
        return self.channel


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
def stub_board_render(monkeypatch):
    """Record board renders instead of posting to Discord."""
    calls = {"one": [], "all": 0}

    async def _refresh_board(client, board_row):
        calls["one"].append(board_row.id)

    async def _refresh_all(client):
        calls["all"] += 1

    monkeypatch.setattr(workflow.market_boards, "refresh_board", _refresh_board)
    monkeypatch.setattr(workflow.market_boards, "refresh_all_boards", _refresh_all)
    return calls


async def _seeded_season(conn, *, name="S1"):
    created = await workflow.create_and_activate_season(
        guild_id=GUILD, name=name, preset="f1"
    )
    return created


# ── seasons ──────────────────────────────────────────────────────────


async def test_create_season_does_not_activate_it(workflow_db):
    created = await workflow.create_season(guild_id=GUILD, name="S1")

    active = await queries.fetch_active_season(workflow_db, GUILD)
    assert active is None, (
        "create must stay separate from activate so a commissioner can "
        "build next season while this one runs"
    )
    assert created.preset_seeded is False


async def test_create_and_activate_does_both(workflow_db):
    created = await workflow.create_and_activate_season(guild_id=GUILD, name="S1")

    active = await queries.fetch_active_season(workflow_db, GUILD)
    assert active is not None
    assert active.id == created.season_id


async def test_f1_preset_seeds_the_145m_cap(workflow_db):
    created = await _seeded_season(workflow_db)

    cfg = await queries.fetch_league_config_row(workflow_db, created.season_id, None)
    assert cfg is not None
    assert cfg.salary_cap == Decimal("145.00"), (
        "the league-wide default cap is $145.00M and every tier inherits it"
    )
    assert created.preset_seeded is True


async def test_duplicate_season_name_is_rejected(workflow_db):
    await workflow.create_season(guild_id=GUILD, name="S1")

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.create_season(guild_id=GUILD, name="S1")

    assert "already exists" in str(exc.value)


async def test_blank_season_name_is_rejected(workflow_db):
    with pytest.raises(workflow.WorkflowError):
        await workflow.create_season(guild_id=GUILD, name="   ")


async def test_activating_an_unknown_season_is_a_user_error(workflow_db):
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.activate_season(guild_id=GUILD, name="nope")

    assert "nope" in str(exc.value)


# ── tiers ────────────────────────────────────────────────────────────


async def test_add_tier_requires_an_active_season(workflow_db):
    await workflow.create_season(guild_id=GUILD, name="S1")  # created, not active

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.add_tier(
            guild_id=GUILD, code="t9", label="Tier 9", rank_order=9
        )

    assert "active season" in str(exc.value).lower()


async def test_add_tier_lowercases_the_code(workflow_db):
    created = await workflow.create_and_activate_season(guild_id=GUILD, name="S1")

    await workflow.add_tier(guild_id=GUILD, code="  T9  ", label="Tier 9", rank_order=9)

    tier = await queries.fetch_tier(workflow_db, created.season_id, "t9")
    assert tier is not None
    assert tier.label == "Tier 9"


async def test_duplicate_tier_code_is_rejected(workflow_db):
    await _seeded_season(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.add_tier(guild_id=GUILD, code="t1", label="Dup", rank_order=1)

    assert "already exists" in str(exc.value)


async def test_set_tier_role_preserves_label_rank_and_colour(workflow_db):
    created = await workflow.create_and_activate_season(guild_id=GUILD, name="S1")
    await workflow.add_tier(
        guild_id=GUILD,
        code="t9",
        label="Tier 9",
        rank_order=7,
        accent_color=0xE10600,
    )

    await workflow.set_tier_role(guild_id=GUILD, tier_code="t9", role_id=555)

    tier = await queries.fetch_tier(workflow_db, created.season_id, "t9")
    assert tier.tier_role_id == 555
    # `update_tier` rewrites every column, so these would blank out if
    # the current values were not read back first.
    assert tier.label == "Tier 9"
    assert tier.rank_order == 7
    assert tier.accent_color == 0xE10600


async def test_list_tier_choices_is_empty_without_a_season(workflow_db):
    assert await workflow.list_tier_choices(GUILD) == []


async def test_list_tier_choices_returns_code_and_label(workflow_db):
    await _seeded_season(workflow_db)

    choices = await workflow.list_tier_choices(GUILD)

    codes = [code for code, _label in choices]
    assert codes == ["t1", "t2", "t3"], "must come back in rank order"
    assert all(code in label for code, label in choices)


# ── config, roles, channels ──────────────────────────────────────────


async def test_set_commissioner_role(workflow_db):
    created = await _seeded_season(workflow_db)

    await workflow.set_commissioner_role(guild_id=GUILD, role_id=99)

    cfg = await queries.fetch_league_config_row(workflow_db, created.season_id, None)
    assert cfg.commissioner_role_id == 99


async def test_set_channel_rejects_unknown_kinds(workflow_db):
    await _seeded_season(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.set_channel(guild_id=GUILD, kind="lounge", channel_id=1)

    assert "lounge" in str(exc.value)


@pytest.mark.parametrize("kind", workflow.CHANNEL_KINDS)
async def test_each_channel_kind_writes_its_own_column(workflow_db, kind):
    created = await _seeded_season(workflow_db)

    await workflow.set_channel(guild_id=GUILD, kind=kind, channel_id=777)

    cfg = await queries.fetch_league_config_row(workflow_db, created.season_id, None)
    assert getattr(cfg, f"{kind}_channel_id") == 777


async def test_fetch_config_for_edit_returns_the_row_to_prefill(workflow_db):
    created = await _seeded_season(workflow_db)

    season_id, tier_id, cfg = await workflow.fetch_config_for_edit(guild_id=GUILD)

    assert season_id == created.season_id
    assert tier_id is None, "the season default row carries a NULL tier"
    assert cfg.salary_cap == Decimal("145.00")


async def test_fetch_config_for_edit_refuses_when_there_is_no_row(workflow_db):
    # No preset, so no config row was ever seeded.
    await workflow.create_and_activate_season(guild_id=GUILD, name="S1")

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.fetch_config_for_edit(guild_id=GUILD)

    assert "config" in str(exc.value).lower()


# ── boards ───────────────────────────────────────────────────────────


async def test_tier_scoped_board_without_a_tier_is_rejected(
    workflow_db, stub_board_render
):
    await _seeded_season(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.add_board(
            _FakeClient(), guild_id=GUILD, kind="market", channel_id=1
        )

    assert "require a tier" in str(exc.value)
    assert stub_board_render["one"] == []


async def test_dashboard_board_with_a_tier_is_rejected(
    workflow_db, stub_board_render
):
    await _seeded_season(workflow_db)

    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.add_board(
            _FakeClient(),
            guild_id=GUILD,
            kind="dashboard",
            channel_id=1,
            tier_code="t1",
        )

    assert "cross-tier" in str(exc.value)


async def test_unknown_board_kind_is_rejected(workflow_db, stub_board_render):
    await _seeded_season(workflow_db)

    with pytest.raises(workflow.WorkflowError):
        await workflow.add_board(
            _FakeClient(), guild_id=GUILD, kind="gossip", channel_id=1
        )


async def test_add_board_renders_it_immediately(workflow_db, stub_board_render):
    await _seeded_season(workflow_db)

    board_id = await workflow.add_board(
        _FakeClient(),
        guild_id=GUILD,
        kind="market",
        channel_id=1234,
        tier_code="t1",
    )

    assert stub_board_render["one"] == [board_id], (
        "a board that is created but never drawn looks broken to admins"
    )


async def test_list_boards_reports_health_and_scope(workflow_db, stub_board_render):
    await _seeded_season(workflow_db)
    market_id = await workflow.add_board(
        _FakeClient(), guild_id=GUILD, kind="market", channel_id=1, tier_code="t2"
    )
    dash_id = await workflow.add_board(
        _FakeClient(), guild_id=GUILD, kind="dashboard", channel_id=2
    )

    boards = {b.board_id: b for b in await workflow.list_boards(GUILD)}

    assert boards[market_id].tier_code == "t2"
    assert boards[dash_id].tier_code is None
    # No message_id yet, because rendering is stubbed here.
    assert boards[market_id].healthy is False


async def test_remove_board_deletes_the_row(workflow_db, stub_board_render):
    await _seeded_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=GUILD, kind="market", channel_id=1, tier_code="t1"
    )

    await workflow.remove_board(_FakeClient(), board_id=board_id)

    assert await workflow.list_boards(GUILD) == []


async def test_remove_board_deletes_its_message_when_there_is_one(
    workflow_db, stub_board_render
):
    await _seeded_season(workflow_db)
    board_id = await workflow.add_board(
        _FakeClient(), guild_id=GUILD, kind="market", channel_id=1, tier_code="t1"
    )
    await queries.set_market_board_message_id(workflow_db, board_id, 9001)

    client = _FakeClient()
    await workflow.remove_board(client, board_id=board_id)

    assert client.channel.deleted == [9001]


async def test_removing_an_unknown_board_is_a_user_error(
    workflow_db, stub_board_render
):
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.remove_board(_FakeClient(), board_id=123456)

    assert "123456" in str(exc.value).replace("`", "")


async def test_refresh_without_an_id_refreshes_everything(
    workflow_db, stub_board_render
):
    await workflow.refresh_boards(_FakeClient(), board_id=None)

    assert stub_board_render["all"] == 1


async def test_refresh_with_an_unknown_id_is_a_user_error(
    workflow_db, stub_board_render
):
    with pytest.raises(workflow.WorkflowError):
        await workflow.refresh_boards(_FakeClient(), board_id=123456)


# ── status regression ────────────────────────────────────────────────


async def test_status_reads_real_tier_rows(workflow_db):
    """
    Regression: this raised AttributeError before, because the status
    builder read a field name the tier row does not have. Any server with
    a tier — i.e. any real server — hit it the moment it ran `/league`.
    """
    await _seeded_season(workflow_db)

    status = await workflow.fetch_league_status(GUILD)

    assert status.has_season
    assert [t.code for t in status.tiers] == ["t1", "t2", "t3"]
    assert [t.label for t in status.tiers] == ["Tier 1", "Tier 2", "Tier 3"]
    assert status.has_config
    assert status.has_drivers is False
    assert status.setup_complete is False


async def test_status_on_a_bare_server_is_all_empty(workflow_db):
    status = await workflow.fetch_league_status(GUILD)

    assert not status.has_season
    assert status.tiers == []
    assert status.board_count == 0
    assert status.pending_offers == 0


async def test_status_reports_linked_tier_roles(workflow_db):
    await _seeded_season(workflow_db)
    await workflow.set_tier_role(guild_id=GUILD, tier_code="t1", role_id=42)

    status = await workflow.fetch_league_status(GUILD)
    by_code = {t.code: t for t in status.tiers}

    assert by_code["t1"].has_role is True
    assert by_code["t2"].has_role is False


async def test_status_counts_boards(workflow_db, stub_board_render):
    await _seeded_season(workflow_db)
    await workflow.add_board(
        _FakeClient(), guild_id=GUILD, kind="dashboard", channel_id=1
    )

    status = await workflow.fetch_league_status(GUILD)

    assert status.board_count == 1


async def test_preset_seeds_three_tiers(workflow_db):
    created = await _seeded_season(workflow_db)

    tiers = await queries.fetch_all_tiers(workflow_db, created.season_id)

    assert len(tiers) == 3
    assert f1_preset is not None
