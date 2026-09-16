"""
Escrow is switchable from Discord, and nothing switches it by accident.

Before this, `escrow_enabled` was read in eleven places and written by
none: `budget config` did not expose it, migration 015 set existing rows
FALSE and the column DEFAULT TRUE, and `upsert_budget_config` omitted
the column entirely. A league could neither see the setting nor change
it without SQL, and a season configured after the migration silently got
escrow on.

The tests below cover the two halves of fixing that.

**It can be changed.** The typed command takes `escrow:`, the Money
panel has a toggle, and both surfaces state the setting rather than
leaving it invisible.

**It cannot be changed by accident.** This is the part worth pinning.
`escrow_enabled=None` means "leave it alone", so a commissioner editing
the DNF penalty does not reset an escrow decision as a side effect —
the same revert-what-you-did-not-touch bug as G1. A plain `bool` default
in that signature would have reintroduced it, which is why the
parameter is `bool | None`.
"""

from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot import queries, workflow
from bot.cogs import admin_market
from bot.market import budget as budget_engine
from bot.presets import f1 as f1_preset
from bot.ui import money_screen
from tests.test_money_screen import FakeInteraction, seed_league

GUILD = 1


@pytest.fixture
def money_db(monkeypatch, pg_conn_migrated):
    """Point `workflow.db` and the screen's own reads at the test schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(money_screen.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


_RATES = {
    "earnings_per_point": Decimal("0.05"),
    "dnf_penalty": Decimal("0.5"),
    "dns_penalty": Decimal("1"),
    "penalty_per_incident_pt": Decimal("0.1"),
}


async def _write(conn, season_id, *, escrow, opening="50", tier_id=None):
    return await queries.upsert_budget_config(
        conn,
        season_id=season_id,
        tier_id=tier_id,
        enforce_budget=True,
        rollover_enabled=True,
        opening_budget=Decimal(opening),
        escrow_enabled=escrow,
        **_RATES,
    )


# ── the column default and the code default agree ────────────────────


@pytest.mark.asyncio
async def test_omitting_escrow_on_insert_matches_the_column_default(pg_conn_migrated):
    """
    `upsert_budget_config` restates the migration's DEFAULT TRUE as a
    COALESCE, because Postgres cannot take DEFAULT from a parameter. That
    duplication is only safe while the two agree, so assert against the
    schema rather than against the literal `True`.
    """
    declared = await pg_conn_migrated.fetchval(
        """
        SELECT column_default FROM information_schema.columns
         WHERE table_name = 'budget_config' AND column_name = 'escrow_enabled'
        """
    )
    season_id = await queries.insert_season(pg_conn_migrated, GUILD, "S1", is_active=True)

    cfg = await _write(pg_conn_migrated, season_id, escrow=None)

    assert declared == "true"
    assert cfg.escrow_enabled is True


@pytest.mark.asyncio
async def test_the_dataclass_default_matches_the_column_default(pg_conn_migrated):
    """A config built in memory must not disagree with one read back."""
    declared = await pg_conn_migrated.fetchval(
        """
        SELECT column_default FROM information_schema.columns
         WHERE table_name = 'budget_config' AND column_name = 'escrow_enabled'
        """
    )
    in_memory = budget_engine.BudgetConfig(
        season_id=1,
        enforce_budget=True,
        rollover_enabled=True,
        opening_budget=Decimal("50"),
        **_RATES,
    )
    assert str(in_memory.escrow_enabled).lower() == declared


# ── it can be set both ways ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_escrow_can_be_created_off_and_switched_on(pg_conn_migrated):
    season_id = await queries.insert_season(pg_conn_migrated, GUILD, "S1", is_active=True)

    created = await _write(pg_conn_migrated, season_id, escrow=False)
    assert created.escrow_enabled is False

    flipped = await _write(pg_conn_migrated, season_id, escrow=True)
    assert flipped.escrow_enabled is True
    assert flipped.id == created.id, "should update the row, not insert a second"


# ── and not by accident: the regression this file exists for ─────────


@pytest.mark.asyncio
async def test_editing_a_rate_leaves_escrow_alone(pg_conn_migrated):
    """
    The real failure mode. A commissioner turns escrow off, then later
    edits the DNF penalty. If `escrow_enabled` defaulted to True in the
    write path, that unrelated edit would switch escrow back on and
    charge every future signing against team cash without anyone asking.
    """
    season_id = await queries.insert_season(pg_conn_migrated, GUILD, "S1", is_active=True)
    await _write(pg_conn_migrated, season_id, escrow=False)

    await _write(pg_conn_migrated, season_id, escrow=None, opening="75")

    after = await queries.fetch_budget_config(pg_conn_migrated, season_id, None)
    assert after.opening_budget == Decimal("75"), "the rate edit should land"
    assert after.escrow_enabled is False, "escrow must not have moved"


@pytest.mark.asyncio
async def test_a_tier_override_does_not_disturb_the_season_default(pg_conn_migrated):
    season_id = await queries.insert_season(pg_conn_migrated, GUILD, "S1", is_active=True)
    await f1_preset.seed_season(pg_conn_migrated, season_id)
    tier = await queries.fetch_tier(pg_conn_migrated, season_id, "t1")
    await _write(pg_conn_migrated, season_id, escrow=False)

    await _write(pg_conn_migrated, season_id, escrow=True, tier_id=tier.id)

    season_default = await queries.fetch_budget_config_exact(
        pg_conn_migrated, season_id, None
    )
    override = await queries.fetch_budget_config_exact(
        pg_conn_migrated, season_id, tier.id
    )
    assert season_default.escrow_enabled is False
    assert override.escrow_enabled is True


# ── the panel surface ────────────────────────────────────────────────


def _fake_flow(tier_code=None):
    async def _render(_interaction):
        return None

    return SimpleNamespace(tier_code=tier_code, render_settings=_render)


@pytest.mark.asyncio
async def test_save_settings_from_the_rates_modal_preserves_escrow(money_db):
    """Same guarantee as above, driven through the screen's save path."""
    season_id, _ = await seed_league(money_db)
    await _write(money_db, season_id, escrow=False)

    await money_screen._save_settings(
        FakeInteraction(), _fake_flow(), dnf_penalty=Decimal("2")
    )

    after = await queries.fetch_budget_config(money_db, season_id, None)
    assert after.dnf_penalty == Decimal("2")
    assert after.escrow_enabled is False


@pytest.mark.asyncio
async def test_save_settings_can_toggle_escrow(money_db):
    season_id, _ = await seed_league(money_db)
    await _write(money_db, season_id, escrow=True)

    await money_screen._save_settings(
        FakeInteraction(), _fake_flow(), escrow_enabled=False
    )

    after = await queries.fetch_budget_config(money_db, season_id, None)
    assert after.escrow_enabled is False


def test_the_settings_embed_states_the_escrow_setting():
    on = money_screen._build_settings_embed(
        budget_engine.BudgetConfig(
            season_id=1,
            enforce_budget=True,
            rollover_enabled=True,
            opening_budget=Decimal("50"),
            escrow_enabled=True,
            **_RATES,
        ),
        None,
    )
    off = money_screen._build_settings_embed(
        budget_engine.BudgetConfig(
            season_id=1,
            enforce_budget=True,
            rollover_enabled=True,
            opening_budget=Decimal("50"),
            escrow_enabled=False,
            **_RATES,
        ),
        None,
    )
    assert "Escrow: **on**" in on.description
    assert "race by race" in on.description
    assert "Escrow: **off**" in off.description
    assert "commitment only" in off.description


@pytest.mark.asyncio
async def test_the_toggle_is_offered_once_the_row_exists(money_db):
    season_id, _ = await seed_league(money_db)
    await _write(money_db, season_id, escrow=False)
    flow = money_screen._SettingsFlow(
        parent=SimpleNamespace(opener_id=7, tier_code=None)
    )

    await flow.render_settings(FakeInteraction())

    escrow_buttons = [
        child
        for child in flow.children
        if getattr(child, "label", "").startswith("Escrow:")
    ]
    assert len(escrow_buttons) == 1
    assert escrow_buttons[0].label == "Escrow: off \u2192 on"
    assert escrow_buttons[0].disabled is False


@pytest.mark.asyncio
async def test_the_toggle_is_disabled_before_any_budget_row_exists(money_db):
    """
    With no config row the toggle has nothing to write one field into, so
    it is disabled and the embed sends you to Edit rates instead. An
    enabled button here would have to invent an opening budget.
    """
    await queries.insert_season(money_db, GUILD, "S1", is_active=True)
    flow = money_screen._SettingsFlow(
        parent=SimpleNamespace(opener_id=7, tier_code=None)
    )

    await flow.render_settings(FakeInteraction())

    escrow_buttons = [
        child
        for child in flow.children
        if getattr(child, "label", "").startswith("Escrow:")
    ]
    assert len(escrow_buttons) == 1
    assert escrow_buttons[0].disabled is True


# ── the typed command's receipt ──────────────────────────────────────


@pytest.mark.parametrize(
    ("escrow", "expected"),
    [(True, "yes"), (False, "no \u2014 commitment only")],
)
def test_the_command_receipt_states_escrow(escrow, expected):
    cfg = budget_engine.BudgetConfig(
        season_id=1,
        enforce_budget=True,
        rollover_enabled=True,
        opening_budget=Decimal("50"),
        escrow_enabled=escrow,
        **_RATES,
    )

    embed = admin_market._render_budget_config(cfg, None)

    rules = next(f.value for f in embed.fields if f.name == "Rules")
    assert f"Escrow (salary charged race by race): {expected}" in rules


@pytest.mark.asyncio
async def test_escrow_alone_is_a_change_not_a_read(money_db, monkeypatch):
    """
    `budget config` prints the current settings when given no arguments
    and writes when given any. Escrow has to count as an argument, or
    `escrow: false` on its own would print the config and change nothing
    — the silent no-op that would make the control impossible to trust.

    Driven through the real command callback, with only the Manage Server
    check stubbed; the permission gate is covered elsewhere.
    """
    season_id, _ = await seed_league(money_db)
    await _write(money_db, season_id, escrow=True)
    monkeypatch.setattr(admin_market, "_is_admin", lambda _interaction: True)
    interaction = FakeInteraction()

    await admin_market.AdminMarketCog.budget_config.callback(
        SimpleNamespace(), interaction, escrow=False
    )

    after = await queries.fetch_budget_config(money_db, season_id, None)
    assert after.escrow_enabled is False, "escrow alone must write"
    sent = [payload for kind, payload in interaction.calls if kind == "send_message"]
    assert sent and "saved" in (sent[-1].get("content") or "")
    rules = next(f.value for f in sent[-1]["embed"].fields if f.name == "Rules")
    assert "commitment only" in rules


@pytest.mark.asyncio
async def test_escrow_omitted_still_shows_rather_than_writes(money_db, monkeypatch):
    """The other half: no arguments at all is still a read-only show."""
    season_id, _ = await seed_league(money_db)
    await _write(money_db, season_id, escrow=False)
    monkeypatch.setattr(admin_market, "_is_admin", lambda _interaction: True)
    interaction = FakeInteraction()

    await admin_market.AdminMarketCog.budget_config.callback(
        SimpleNamespace(), interaction
    )

    sent = [payload for kind, payload in interaction.calls if kind == "send_message"]
    assert sent[-1].get("content") is None, "a bare show must not claim it saved"
    after = await queries.fetch_budget_config(money_db, season_id, None)
    assert after.escrow_enabled is False
