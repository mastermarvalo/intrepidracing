"""
The panel's value is entirely in `_next_step_text`: it must name exactly
one blocking action, and it must pick the *right* one when several things
are incomplete. These tests pin the precedence order, because a wrong
ordering here sends an admin to publish a run before they have drivers.

`build_status_embed` is tested only for Discord-limit safety and for not
leaking admin-only information to non-admins.
"""

import discord
import pytest

from bot import workflow
from bot.cogs import panel
from bot.cogs.panel import _next_step_text as next_step
from bot.cogs.panel import build_status_embed
from bot.ui.setup_screen import build_setup_embed

EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_TOTAL_LIMIT = 6000
MAX_FIELDS = 25


def tier(
    code="t1",
    *,
    drivers=2,
    round_label="R1",
    round_order=1,
    unpublished=None,
    published=True,
):
    return workflow.TierStatus(
        code=code,
        label=f"Tier {code}",
        driver_count=drivers,
        has_role=False,
        latest_round_label=round_label,
        latest_round_order=round_order,
        unpublished_run_id=unpublished,
        has_published_valuation=published,
    )


def status(
    *,
    season="S1",
    season_id=1,
    tiers=None,
    has_config=True,
    boards=1,
    offers=0,
    trades=0,
):
    return workflow.LeagueStatus(
        season_name=season,
        season_id=season_id,
        tiers=[tier()] if tiers is None else tiers,
        has_config=has_config,
        commissioner_role_id=None,
        board_count=boards,
        pending_offers=offers,
        pending_trades=trades,
    )


# ── LeagueStatus properties ──────────────────────────────────────────


def test_no_season_is_not_set_up():
    s = status(season=None, season_id=None, tiers=[])
    assert not s.has_season
    assert not s.setup_complete


def test_tiers_without_drivers_is_not_complete():
    s = status(tiers=[tier(drivers=0)])
    assert s.has_tiers
    assert not s.has_drivers
    assert not s.setup_complete


def test_drivers_in_any_tier_counts():
    s = status(tiers=[tier("t1", drivers=0), tier("t2", drivers=3)])
    assert s.has_drivers


def test_config_is_required_for_completeness():
    assert not status(has_config=False).setup_complete
    assert status().setup_complete


# ── Next-step precedence ─────────────────────────────────────────────


def test_missing_season_beats_everything():
    s = status(season=None, season_id=None, tiers=[], has_config=False)
    assert "season" in next_step(s).lower()


def test_missing_tiers_comes_before_config():
    s = status(tiers=[], has_config=False)
    assert "tier" in next_step(s).lower()


def test_missing_config_comes_before_drivers():
    s = status(tiers=[tier(drivers=0)], has_config=False)
    assert "cap" in next_step(s).lower()


def test_missing_drivers_comes_before_valuation_work():
    s = status(tiers=[tier(drivers=0, published=False)])
    assert "driver" in next_step(s).lower()


def test_unpublished_run_is_surfaced_with_its_id():
    s = status(tiers=[tier(unpublished=42)])
    text = next_step(s)
    assert "42" in text
    assert "t1" in text


def test_unpublished_run_beats_an_unpriced_sibling_tier():
    """A half-finished action outranks starting a new one."""
    s = status(tiers=[tier("t1", published=False), tier("t2", unpublished=7)])
    assert "7" in next_step(s)


def test_tier_without_a_published_market_is_named():
    s = status(tiers=[tier("t1"), tier("t3", published=False)])
    text = next_step(s)
    assert "t3" in text
    assert "t1" not in text


def test_approvals_come_before_boards():
    s = status(offers=2, boards=0)
    assert "approval" in next_step(s).lower()


def test_missing_board_is_the_last_setup_nudge():
    assert "board" in next_step(status(boards=0)).lower()


def test_fully_configured_league_is_told_it_is_up_to_date():
    assert "up to date" in next_step(status()).lower()


# ── Embed safety ─────────────────────────────────────────────────────


def _assert_within_limits(embed: discord.Embed) -> None:
    assert len(embed.fields) <= MAX_FIELDS
    for f in embed.fields:
        assert len(f.value) <= EMBED_FIELD_VALUE_LIMIT, f.name
    assert len(embed) <= EMBED_TOTAL_LIMIT


@pytest.mark.parametrize("is_admin", [True, False])
def test_status_embed_within_limits_for_a_large_league(is_admin):
    tiers = [tier(f"t{i}", unpublished=i) for i in range(40)]
    _assert_within_limits(
        build_status_embed(status(tiers=tiers, offers=9, trades=4), is_admin=is_admin)
    )


def test_empty_league_embed_within_limits():
    s = status(season=None, season_id=None, tiers=[])
    _assert_within_limits(build_status_embed(s, is_admin=True))


def test_approvals_queue_is_admin_only():
    s = status(offers=3, trades=1)
    admin_view = build_status_embed(s, is_admin=True)
    member_view = build_status_embed(s, is_admin=False)

    assert any("Waiting on you" in f.name for f in admin_view.fields)
    assert not any("Waiting on you" in f.name for f in member_view.fields)


def test_setup_embed_within_limits_at_both_extremes():
    _assert_within_limits(
        build_setup_embed(status(season=None, season_id=None, tiers=[], has_config=False))
    )
    _assert_within_limits(build_setup_embed(status()))


# ── every screen is reachable from home ──────────────────────────────
#
# The consolidation work is only real if a league owner can get to each
# screen by clicking. Five screens shipped fully built but unreachable
# because nothing on the home panel opened them, which is the exact
# failure these tests exist to prevent.


# The twelve consolidated screens and the button label that opens each.
# Setup, Race Night and All commands are the pre-existing three.
ADMIN_SCREEN_LABELS = {
    "Setup",
    "Race Night",
    "Drivers",
    "Approvals",
    "Boards",
    "Money",
    "Off-season",
    "Market",
    "Contracts",
    "Trades",
    "All commands",
}

# Non-admins get the three read/act desks plus help. They must NOT get
# Setup, Money or Off-season: those rewrite league-wide state.
MEMBER_SCREEN_LABELS = {"Market", "Contracts", "Trades", "All commands"}

DISCORD_MAX_COMPONENTS_PER_VIEW = 25


def _labels(view) -> set[str]:
    return {
        getattr(item, "label", "").split(" (")[0]
        for item in view.children
    }


def test_an_admin_can_reach_every_screen_from_home():
    view = panel.HomeView(status=status(), opener_id=1, is_admin=True)
    missing = ADMIN_SCREEN_LABELS - _labels(view)
    assert not missing, f"unreachable from home: {sorted(missing)}"


def test_a_member_gets_the_desks_but_not_the_commissioners_books():
    """
    A Team Principal must be able to read the market and work their own
    contracts without a commissioner. They must not be handed Setup,
    Money or Off-season, which change the whole league.
    """
    view = panel.HomeView(status=status(), opener_id=1, is_admin=False)
    labels = _labels(view)
    assert MEMBER_SCREEN_LABELS <= labels
    assert not ({"Setup", "Money", "Off-season"} & labels)


def test_every_home_button_actually_opens_something():
    """
    A button with no callback is worse than a missing one: it looks like
    the feature exists and then does nothing.
    """
    view = panel.HomeView(status=status(), opener_id=1, is_admin=True)
    for item in view.children:
        assert callable(getattr(item, "callback", None)), (
            f"{getattr(item, 'label', item)} has no callback"
        )


def test_home_stays_inside_discords_component_limit():
    """
    Discord rejects a view with more than 25 components outright, so the
    panel would fail to render at all rather than degrade.
    """
    view = panel.HomeView(status=status(), opener_id=1, is_admin=True)
    assert len(view.children) <= DISCORD_MAX_COMPONENTS_PER_VIEW


def test_the_desks_are_hidden_until_there_are_tiers():
    """
    Market, Contracts and Trades are all tier-scoped. Before any tier
    exists they would open onto nothing, so Setup comes first.
    """
    view = panel.HomeView(
        status=status(season_id=1, tiers=[]), opener_id=1, is_admin=True
    )
    labels = _labels(view)
    assert not ({"Market", "Contracts", "Trades"} & labels)
    assert "Setup" in labels


def test_money_and_offseason_need_a_season():
    """Both read season-scoped money; with no season there is nothing."""
    view = panel.HomeView(
        status=status(season=None, season_id=None, tiers=[]),
        opener_id=1,
        is_admin=True,
    )
    assert not ({"Money", "Off-season"} & _labels(view))
