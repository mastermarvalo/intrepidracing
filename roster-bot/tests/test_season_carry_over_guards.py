"""
G21 — activating a season was a dead end, and carry-over was a
free-text, irreversible, out-of-order-capable command.

`/market-admin season activate` replied "✅ X is now the active season"
and said nothing about the two jobs that must follow it. `carry-over`
took a season name as free text with no autocomplete, no way to look
before writing, and no objection to carrying the newest past season
first — which strands older contracts in a season nobody is racing.

The Offseason panel already previewed and only ever offered the oldest
pending season. These tests pin the typed command to that behaviour.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest

from bot.cogs import admin_market
from bot.ui import offseason_screen

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc)
_GUILD = 42


def _past(name: str, *, unresolved: int, age_days: int) -> offseason_screen.PastSeason:
    return offseason_screen.PastSeason(
        season_id=abs(hash(name)) % 1000,
        name=name,
        created_at=NOW - timedelta(days=age_days),
        unresolved_contracts=unresolved,
        budget_rows=0,
    )


def _state(past: list[offseason_screen.PastSeason]) -> offseason_screen.OffseasonState:
    return offseason_screen.OffseasonState(
        active_season_name="S8",
        active_season_id=8,
        newest_season_name="S8",
        newest_is_active=True,
        season_count=len(past) + 1,
        free_agency_open=True,
        has_config=True,
        past_seasons=past,
        teams_total=10,
        teams_rolled_over=0,
        rollover_enabled=True,
        prize_money_total=Decimal("0"),
        tiers=[],
    )


@pytest.fixture
def stub_state(monkeypatch):
    """Install an offseason state for the ordering check to read."""
    def _install(past: list[offseason_screen.PastSeason]) -> None:
        async def _load(_guild_id):
            return _state(past)

        monkeypatch.setattr(
            admin_market.offseason_screen, "load_offseason_state", _load
        )

    return _install


# ── oldest-first ordering ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_carrying_the_oldest_pending_season_is_allowed(stub_state):
    stub_state([
        _past("S6", unresolved=4, age_days=400),
        _past("S7", unresolved=3, age_days=200),
    ])
    assert await admin_market._carry_over_order_blocker(_GUILD, "S6") is None


@pytest.mark.asyncio
async def test_skipping_an_older_pending_season_is_refused(stub_state):
    stub_state([
        _past("S6", unresolved=4, age_days=400),
        _past("S7", unresolved=3, age_days=200),
    ])
    blocker = await admin_market._carry_over_order_blocker(_GUILD, "S7")
    assert blocker is not None
    assert "S6" in blocker
    # The refusal has to hand back the command that unblocks them.
    assert "carry-over from_season:S6" in blocker
    assert "stranded" in blocker


@pytest.mark.asyncio
async def test_the_ordering_refusal_is_case_insensitive(stub_state):
    stub_state([_past("S6", unresolved=4, age_days=400)])
    assert await admin_market._carry_over_order_blocker(_GUILD, "s6") is None


@pytest.mark.asyncio
async def test_nothing_pending_means_nothing_to_block(stub_state):
    stub_state([_past("S6", unresolved=0, age_days=400)])
    assert await admin_market._carry_over_order_blocker(_GUILD, "S6") is None


@pytest.mark.asyncio
async def test_an_unknown_season_is_left_to_the_workflow_to_explain(
    stub_state,
):
    """
    Two different errors need two different messages. "No season named
    X" belongs to the workflow, which already says it well.
    """
    stub_state([_past("S6", unresolved=4, age_days=400)])
    assert await admin_market._carry_over_order_blocker(_GUILD, "S99") is None


@pytest.mark.asyncio
async def test_a_read_failure_does_not_block_a_legitimate_carry_over(
    monkeypatch,
):
    async def _boom(_guild_id):
        raise RuntimeError("database is down")

    monkeypatch.setattr(
        admin_market.offseason_screen, "load_offseason_state", _boom
    )
    assert await admin_market._carry_over_order_blocker(_GUILD, "S6") is None


# ── the dry run ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class _Preview:
    from_season_name: str = "S7"
    to_season_name: str = "S8"
    to_carry: int = 12
    to_expire: int = 5
    carried_value: Decimal = Decimal("240.00")
    expiring_value: Decimal = Decimal("60.00")
    over_cap: tuple = ()
    salary_cap: Decimal = Decimal("145.00")


def test_the_preview_says_nothing_was_written():
    note = admin_market._render_carry_preview(_Preview())
    assert "Nothing was written" in note
    assert "12" in note
    assert "5" in note


def test_the_preview_names_teams_left_over_the_cap():
    note = admin_market._render_carry_preview(
        _Preview(over_cap=(("McLaren", Decimal("150.00"), Decimal("145.00")),))
    )
    assert "McLaren" in note
    assert "⚠" in note
    # Carry-over does not block on cap, and the preview must not imply it
    # will.
    assert "does not block" in note


def test_the_preview_is_explicit_when_there_is_nothing_to_do():
    note = admin_market._render_carry_preview(
        _Preview(to_carry=0, to_expire=0, carried_value=Decimal("0"),
                 expiring_value=Decimal("0"))
    )
    assert "Nothing to do" in note


def test_the_preview_fits_in_one_discord_message():
    many = tuple(
        (f"Team {i}", Decimal("150.00"), Decimal("145.00")) for i in range(40)
    )
    note = admin_market._render_carry_preview(_Preview(over_cap=many))
    assert len(note) <= admin_market._DISCORD_MSG_LIMIT
    assert "more." in note


# ── the command surface ──────────────────────────────────────────────


def test_carry_over_offers_a_preview_switch():
    params = {p.name for p in admin_market.AdminMarketCog.season_carry_over.parameters}
    assert "preview" in params
    assert "from_season" in params


def test_carry_over_defaults_to_not_writing_blindly():
    """`preview` must default to off so existing scripts behave, but it
    must exist so an admin can look first."""
    preview = next(
        p for p in admin_market.AdminMarketCog.season_carry_over.parameters
        if p.name == "preview"
    )
    assert preview.required is False
    assert preview.default is False


def test_from_season_has_autocomplete():
    """
    The whole point: an admin should never type a season name.

    Asserted on the parameter's `autocomplete` flag, not on calling
    `Command.autocomplete("from_season")` — that is a decorator factory
    and returns a truthy decorator whether or not one is registered.
    """
    params = {
        p.name: p
        for p in admin_market.AdminMarketCog.season_carry_over.parameters
    }
    assert params["from_season"].autocomplete is True
    assert params["preview"].autocomplete is False


@pytest.mark.asyncio
async def test_the_autocomplete_offers_pending_seasons_oldest_first(
    stub_state,
):
    stub_state([
        _past("S6", unresolved=4, age_days=400),
        _past("S7", unresolved=3, age_days=200),
        _past("S5", unresolved=0, age_days=600),
    ])
    cog = admin_market.AdminMarketCog.__new__(admin_market.AdminMarketCog)
    interaction = SimpleNamespace(guild_id=_GUILD)
    choices = await admin_market.AdminMarketCog._carry_over_autocomplete(
        cog, interaction, ""
    )
    # S5 has nothing left to resolve, so it is not offered at all.
    assert [c.value for c in choices] == ["S6", "S7"]
    assert "4 contract(s)" in choices[0].name


@pytest.mark.asyncio
async def test_the_autocomplete_filters_on_what_was_typed(stub_state):
    stub_state([
        _past("S6", unresolved=4, age_days=400),
        _past("S7", unresolved=3, age_days=200),
    ])
    cog = admin_market.AdminMarketCog.__new__(admin_market.AdminMarketCog)
    choices = await admin_market.AdminMarketCog._carry_over_autocomplete(
        cog, SimpleNamespace(guild_id=_GUILD), "s7"
    )
    assert [c.value for c in choices] == ["S7"]


@pytest.mark.asyncio
async def test_the_autocomplete_never_raises_at_the_user(monkeypatch):
    async def _boom(_guild_id):
        raise RuntimeError("database is down")

    monkeypatch.setattr(
        admin_market.offseason_screen, "load_offseason_state", _boom
    )
    cog = admin_market.AdminMarketCog.__new__(admin_market.AdminMarketCog)
    choices = await admin_market.AdminMarketCog._carry_over_autocomplete(
        cog, SimpleNamespace(guild_id=_GUILD), ""
    )
    assert choices == []


@pytest.mark.asyncio
async def test_the_autocomplete_respects_the_discord_choice_ceiling(
    stub_state,
):
    stub_state([
        _past(f"S{i}", unresolved=1, age_days=1000 - i)
        for i in range(40)
    ])
    cog = admin_market.AdminMarketCog.__new__(admin_market.AdminMarketCog)
    choices = await admin_market.AdminMarketCog._carry_over_autocomplete(
        cog, SimpleNamespace(guild_id=_GUILD), ""
    )
    assert len(choices) == admin_market._AUTOCOMPLETE_MAX
    assert all(len(c.name) <= 100 for c in choices)
