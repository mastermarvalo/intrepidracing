"""
`workflow.publish_valuation` is the single code path behind both
`/market-admin valuation publish` and the panel's Publish button, so the
guarantees asserted here hold for both callers.

The important ones: publishing twice must not move the market twice, and
a failed board refresh must not undo or hide a successful publish.
"""

import pytest

from bot import queries, workflow
from bot.presets import f1 as f1_preset


class _FakeClient:
    """Stands in for the discord bot; only passed through to the refresher."""


async def _season_and_run(conn, *, published=False):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES (1, 'S1', TRUE) RETURNING id"
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    run_id = await queries.insert_valuation_run(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        round_label="R1",
        created_by=1,
        published=published,
    )
    return season_id, tier_id, run_id


@pytest.fixture
def stub_boards(monkeypatch):
    """Record refresh calls instead of talking to Discord."""
    calls = []

    async def _fake_refresh(client, **kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(
        workflow.market_boards, "refresh_boards_for_tier", _fake_refresh
    )
    return calls


@pytest.fixture
def failing_boards(monkeypatch):
    async def _boom(client, **kwargs):
        raise RuntimeError("discord is having a day")

    monkeypatch.setattr(workflow.market_boards, "refresh_boards_for_tier", _boom)


@pytest.fixture
def workflow_db(monkeypatch, pg_conn_migrated):
    """Point workflow's `db.connect()` at the test schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


# ── Behaviour ────────────────────────────────────────────────────────


async def test_unknown_run_is_a_user_facing_error(workflow_db, stub_boards):
    with pytest.raises(workflow.WorkflowError) as exc:
        await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=999_999)

    assert "999999" in str(exc.value).replace("`", "")
    assert stub_boards == [], "must not touch boards for a run that does not exist"


async def test_publish_marks_the_run_published(workflow_db, stub_boards):
    _, _, run_id = await _season_and_run(workflow_db)

    outcome = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)

    assert not outcome.already_published
    assert outcome.boards_refreshed
    row = await queries.fetch_valuation_run(workflow_db, run_id)
    assert row["published"]


async def test_publish_refreshes_boards_for_the_right_tier(workflow_db, stub_boards):
    season_id, tier_id, run_id = await _season_and_run(workflow_db)

    await workflow.publish_valuation(_FakeClient(), guild_id=77, run_id=run_id)

    assert stub_boards == [
        {"guild_id": 77, "season_id": season_id, "tier_id": tier_id}
    ]


async def test_publishing_twice_is_a_no_op(workflow_db, stub_boards):
    """The panel and the command can both be fired at the same run."""
    _, _, run_id = await _season_and_run(workflow_db)

    first = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)
    second = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)

    assert not first.already_published
    assert second.already_published
    assert len(stub_boards) == 1, "a repeat publish must not re-announce"


async def test_already_published_run_is_reported_not_republished(
    workflow_db, stub_boards
):
    _, _, run_id = await _season_and_run(workflow_db, published=True)

    outcome = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)

    assert outcome.already_published
    assert stub_boards == []


async def test_board_failure_does_not_undo_the_publish(workflow_db, failing_boards):
    """Values going live is the durable part; boards heal on the next poll."""
    _, _, run_id = await _season_and_run(workflow_db)

    outcome = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)

    assert not outcome.already_published
    assert not outcome.boards_refreshed
    row = await queries.fetch_valuation_run(workflow_db, run_id)
    assert row["published"], "publish must survive a board-refresh failure"


async def test_outcome_carries_season_and_tier(workflow_db, stub_boards):
    season_id, tier_id, run_id = await _season_and_run(workflow_db)

    outcome = await workflow.publish_valuation(_FakeClient(), guild_id=1, run_id=run_id)

    assert (outcome.run_id, outcome.season_id, outcome.tier_id) == (
        run_id,
        season_id,
        tier_id,
    )
