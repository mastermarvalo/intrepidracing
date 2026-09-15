"""
Rendering and composition of the three interactive screens.

These are pure-function tests: build the state, render the screen, assert
it stays inside Discord's limits and says the right thing. A screen that
exceeds a limit does not degrade gracefully — Discord rejects the whole
message, so the panel would simply fail to open.
"""

from datetime import datetime, timezone
from decimal import Decimal

import discord
import pytest

from bot import approvals, workflow
from bot.ui import approvals_screen, boards_screen, setup_screen
from bot.ui.base import EMBED_FIELD_LIMIT, SELECT_MAX_OPTIONS

# Discord platform limits, asserted rather than assumed.
_LABEL_LIMIT = 80
_SELECT_LABEL_LIMIT = 100
_DESCRIPTION_LIMIT = 4096
_VIEW_ROWS = 5
_VIEW_CHILDREN = 25

NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


# ── builders ─────────────────────────────────────────────────────────


def offer(offer_id=1, *, driver="ZeezinDomar", team="McLaren", tier="t1"):
    return approvals.PendingOffer(
        offer_id=offer_id,
        driver_name=driver,
        team_name=team,
        tier_code=tier,
        salary=Decimal("22.00"),
        term_seasons=2,
        contract_type="standard",
        signing_bonus=Decimal("1.50"),
        offer_kind="new",
        created_at=NOW,
    )


def trade(trade_id=1, *, a="Williams", b="Aston Martin", items=2):
    return approvals.PendingTrade(
        trade_id=trade_id,
        proposing_team_name=a,
        other_team_name=b,
        item_count=items,
        created_at=NOW,
    )


def queue(*, offers=(), trades=(), season_id=1, season_name="Season 7"):
    return approvals.PendingQueue(
        season_id=season_id,
        season_name=season_name,
        offers=list(offers),
        trades=list(trades),
    )


def tier_status(code="t1", *, drivers=20, role=False):
    return workflow.TierStatus(
        code=code,
        label=f"Tier {code[-1]}",
        driver_count=drivers,
        has_role=role,
        latest_round_label="R14 Abu Dhabi",
        latest_round_order=14,
        unpublished_run_id=None,
        has_published_valuation=True,
    )


def league_status(
    *,
    season="Season 7",
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
        tiers=[tier_status()] if tiers is None else tiers,
        has_config=has_config,
        commissioner_role_id=None,
        board_count=boards,
        pending_offers=offers,
        pending_trades=trades,
    )


def board(board_id=1, *, kind="market", tier="t1", channel=999, healthy=True):
    return workflow.BoardInfo(
        board_id=board_id,
        kind=kind,
        tier_code=tier,
        channel_id=channel,
        healthy=healthy,
    )


def assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed.description or "") <= _DESCRIPTION_LIMIT
    for field in embed.fields:
        assert len(field.value) <= EMBED_FIELD_LIMIT, field.name


def assert_view_within_limits(view: discord.ui.View) -> None:
    assert len(view.children) <= _VIEW_CHILDREN
    for child in view.children:
        row = getattr(child, "row", None)
        if row is not None:
            assert row < _VIEW_ROWS, f"row {row} is out of range"
        label = getattr(child, "label", None)
        if label:
            assert len(label) <= _LABEL_LIMIT, label
        for option in getattr(child, "options", []) or []:
            assert len(option.label) <= _SELECT_LABEL_LIMIT, option.label
            if option.description:
                assert len(option.description) <= _SELECT_LABEL_LIMIT


async def noop_back(_interaction):  # pragma: no cover - navigation stub
    return None


# ── approvals screen ─────────────────────────────────────────────────


def test_empty_queue_says_so_plainly():
    embed = approvals_screen.build_approvals_embed(queue())

    assert "clear" in embed.description.lower()
    assert_embed_within_limits(embed)


def test_queue_without_a_season_explains_why_it_is_empty():
    embed = approvals_screen.build_approvals_embed(
        queue(season_id=None, season_name=None)
    )

    assert "no active season" in embed.description.lower()


def test_queue_lists_ids_so_nothing_has_to_be_looked_up():
    embed = approvals_screen.build_approvals_embed(
        queue(offers=[offer(17)], trades=[trade(42)])
    )
    blob = embed.description + "".join(f.value for f in embed.fields)

    assert "17" in blob, "the offer id must be visible to act on it"
    assert "42" in blob
    assert_embed_within_limits(embed)


def test_queue_shows_the_offer_terms():
    embed = approvals_screen.build_approvals_embed(queue(offers=[offer()]))
    blob = "".join(f.value for f in embed.fields)

    assert "22.00" in blob, "salary is the number a commissioner is judging"
    assert "ZeezinDomar" in blob
    assert "McLaren" in blob


def test_a_full_queue_stays_inside_the_field_limit():
    embed = approvals_screen.build_approvals_embed(
        queue(
            offers=[
                offer(i, driver=f"DriverWithAVeryLongName{i}", team=f"Team{i}")
                for i in range(SELECT_MAX_OPTIONS)
            ],
            trades=[trade(i) for i in range(SELECT_MAX_OPTIONS)],
        )
    )

    assert_embed_within_limits(embed)


def test_queue_select_never_exceeds_the_option_limit():
    view = approvals_screen.ApprovalsView(
        queue=queue(
            offers=[offer(i) for i in range(20)],
            trades=[trade(i) for i in range(20)],
        ),
        opener_id=1,
        on_back=noop_back,
    )
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))

    assert len(select.options) <= SELECT_MAX_OPTIONS
    assert_view_within_limits(view)


def test_queue_select_is_disabled_when_there_is_nothing_to_pick():
    view = approvals_screen.ApprovalsView(
        queue=queue(), opener_id=1, on_back=noop_back
    )
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))

    assert select.disabled, "an enabled but empty select is a dead end"


def test_offer_and_trade_options_are_distinguishable():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(5)], trades=[trade(5)]),
        opener_id=1,
        on_back=noop_back,
    )
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))
    values = {o.value for o in select.options}

    assert values == {"offer:5", "trade:5"}, (
        "an offer and a trade can share an id, so the kind must be in the value"
    )


def test_item_view_offers_approve_reject_and_back():
    parent = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(1)]), opener_id=7, on_back=noop_back
    )
    item = approvals_screen._ItemView(
        kind="offer", item_id=1, opener_id=7, parent=parent
    )
    labels = {c.label for c in item.children if getattr(c, "label", None)}

    assert "Approve" in labels
    assert "Reject" in labels
    assert any("Back" in label for label in labels)
    assert_view_within_limits(item)


def test_offer_detail_shows_what_approval_will_do():
    embed = approvals_screen._build_offer_detail(offer(1))
    blob = embed.footer.text or ""

    assert "role" in blob.lower(), (
        "approving assigns a Discord role; that side effect should be stated"
    )
    assert_embed_within_limits(embed)


def test_trade_detail_shows_the_contract_count():
    embed = approvals_screen._build_trade_detail(trade(1, items=3))

    assert any(f.value == "3" for f in embed.fields)
    assert_embed_within_limits(embed)


# ── boards screen ────────────────────────────────────────────────────


def test_no_boards_explains_what_a_board_is():
    embed = boards_screen.build_boards_embed([])

    assert "automatically" in embed.description.lower()
    assert_embed_within_limits(embed)


def test_boards_list_shows_id_kind_scope_and_channel():
    embed = boards_screen.build_boards_embed([board(3, kind="movers", tier="t2")])

    assert "3" in embed.description
    assert "t2" in embed.description
    assert "<#999>" in embed.description


def test_cross_tier_boards_are_labelled_cross_tier():
    embed = boards_screen.build_boards_embed(
        [board(1, kind="dashboard", tier=None)]
    )

    assert "cross-tier" in embed.description


def test_unhealthy_boards_get_a_permissions_hint():
    embed = boards_screen.build_boards_embed([board(1, healthy=False)])

    assert any("Needs attention" in f.name for f in embed.fields)
    assert any("Send Messages" in f.value for f in embed.fields)


def test_a_long_board_list_stays_inside_the_description_limit():
    embed = boards_screen.build_boards_embed(
        [board(i, kind="market", tier="t1", channel=10**17 + i) for i in range(60)]
    )

    assert_embed_within_limits(embed)


def test_boards_view_hides_remove_and_refresh_when_there_are_none():
    view = boards_screen.BoardsView(boards=[], opener_id=1, on_back=noop_back)
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert "Add board" in labels
    assert "Refresh all" not in labels, "nothing to refresh"
    assert not any(isinstance(c, discord.ui.Select) for c in view.children)
    assert_view_within_limits(view)


def test_boards_view_offers_remove_and_refresh_once_boards_exist():
    view = boards_screen.BoardsView(
        boards=[board(1)], opener_id=1, on_back=noop_back
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert "Refresh all" in labels
    assert any(isinstance(c, discord.ui.Select) for c in view.children)
    assert_view_within_limits(view)


def test_remove_select_caps_its_options():
    view = boards_screen.BoardsView(
        boards=[board(i) for i in range(40)], opener_id=1, on_back=noop_back
    )
    select = next(c for c in view.children if isinstance(c, discord.ui.Select))

    assert len(select.options) <= SELECT_MAX_OPTIONS
    assert_view_within_limits(view)


def test_every_board_kind_has_a_human_label():
    for kind in workflow.BOARD_KINDS:
        assert kind in workflow.BOARD_KIND_LABELS, kind


def test_board_kind_scoping_is_partitioned():
    overlap = set(workflow.TIER_SCOPED_BOARD_KINDS) & set(
        workflow.CROSS_TIER_BOARD_KINDS
    )

    assert not overlap, "a kind cannot be both tier-scoped and cross-tier"
    assert set(workflow.BOARD_KINDS) == set(
        workflow.TIER_SCOPED_BOARD_KINDS
    ) | set(workflow.CROSS_TIER_BOARD_KINDS)


def test_add_board_flow_starts_on_the_kind_step():
    parent = boards_screen.BoardsView(boards=[], opener_id=1, on_back=noop_back)
    flow = boards_screen._AddBoardFlow(opener_id=1, parent=parent)
    select = next(c for c in flow.children if isinstance(c, discord.ui.Select))

    assert {o.value for o in select.options} == set(workflow.BOARD_KINDS)
    assert_view_within_limits(flow)


def test_kind_options_say_whether_a_tier_is_needed():
    parent = boards_screen.BoardsView(boards=[], opener_id=1, on_back=noop_back)
    flow = boards_screen._AddBoardFlow(opener_id=1, parent=parent)
    select = next(c for c in flow.children if isinstance(c, discord.ui.Select))
    by_value = {o.value: o.description for o in select.options}

    assert "tier" in by_value["market"].lower()
    assert "every tier" in by_value["dashboard"].lower()


# ── setup screen ─────────────────────────────────────────────────────


def test_setup_on_a_bare_server_points_at_the_season_button():
    embed = setup_screen.build_setup_embed(
        league_status(season=None, season_id=None, tiers=[], has_config=False)
    )

    assert any("Next step" in f.name for f in embed.fields)
    assert "Season" in "".join(f.value for f in embed.fields)
    assert_embed_within_limits(embed)


def test_setup_mentions_the_145m_cap_for_the_preset():
    embed = setup_screen.build_setup_embed(
        league_status(season=None, season_id=None, tiers=[], has_config=False)
    )

    assert "$145.00M" in "".join(f.value for f in embed.fields), (
        "the preset cap is the number admins most want confirmed up front"
    )


def test_setup_ticks_off_what_is_done():
    embed = setup_screen.build_setup_embed(league_status())

    assert embed.description.count("✅") == 3
    assert_embed_within_limits(embed)


def test_setup_shows_which_tiers_have_a_linked_role():
    embed = setup_screen.build_setup_embed(
        league_status(tiers=[tier_status("t1", role=True), tier_status("t2")])
    )
    tiers_field = next(f for f in embed.fields if f.name == "Tiers")

    assert tiers_field.value.count("role linked") == 1


def test_setup_with_many_tiers_stays_inside_the_field_limit():
    embed = setup_screen.build_setup_embed(
        league_status(tiers=[tier_status(f"t{i}") for i in range(40)])
    )

    assert_embed_within_limits(embed)


def test_tier_button_is_disabled_until_a_season_exists():
    view = setup_screen.SetupView(
        status=league_status(season=None, season_id=None, tiers=[], has_config=False),
        opener_id=1,
        on_back=noop_back,
    )
    by_label = {c.label: c for c in view.children if getattr(c, "label", None)}

    assert by_label["Season"].disabled is False
    assert by_label["Tier"].disabled is True, (
        "offering an action that cannot work is worse than hiding it"
    )
    assert by_label["Boards"].disabled is True
    assert_view_within_limits(view)


def test_everything_unlocks_once_tiers_exist():
    view = setup_screen.SetupView(
        status=league_status(), opener_id=1, on_back=noop_back
    )

    assert not any(getattr(c, "disabled", False) for c in view.children)
    assert_view_within_limits(view)


def test_setup_exposes_every_configuration_area():
    view = setup_screen.SetupView(
        status=league_status(), opener_id=1, on_back=noop_back
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    # Each of these maps to a /market-admin command; none may be dropped.
    assert {
        "Season",
        "Tier",
        "Cap & rules",
        "Tier role",
        "Commissioner role",
        "Channels",
        "Boards",
    } <= labels


def test_channel_kind_options_match_the_workflow_list():
    parent = setup_screen.SetupView(
        status=league_status(), opener_id=1, on_back=noop_back
    )
    flow = setup_screen._ChannelFlow(opener_id=1, parent=parent)
    select = next(c for c in flow.children if isinstance(c, discord.ui.Select))

    assert {o.value for o in select.options} == set(workflow.CHANNEL_KINDS)
    assert all(o.description for o in select.options), (
        "each channel needs a one-line explanation of what lands in it"
    )
    assert_view_within_limits(flow)


def test_tier_modal_suggests_the_next_rank_order():
    parent = setup_screen.SetupView(
        status=league_status(tiers=[tier_status("t1"), tier_status("t2")]),
        opener_id=1,
        on_back=noop_back,
    )
    modal = setup_screen._TierModal(parent, suggested_rank=3)
    ranks = [
        item for item in modal.children if "Rank" in getattr(item, "label", "")
    ]

    assert ranks and ranks[0].default == "3"


@pytest.mark.parametrize(
    "screen_view",
    [
        lambda: approvals_screen.ApprovalsView(
            queue=queue(), opener_id=1, on_back=noop_back
        ),
        lambda: boards_screen.BoardsView(
            boards=[board(1)], opener_id=1, on_back=noop_back
        ),
        lambda: setup_screen.SetupView(
            status=league_status(), opener_id=1, on_back=noop_back
        ),
    ],
)
def test_every_screen_is_admin_gated(screen_view):
    view = screen_view()

    from bot.ui.base import AdminOwnedView

    assert isinstance(view, AdminOwnedView), (
        "the panel must not be a softer door into admin actions than the "
        "slash commands are"
    )


# ── navigation integrity ─────────────────────────────────────────────


def _home_labels(status):
    """Every button label an admin actually sees on the home panel."""
    from bot.cogs.panel import HomeView

    view = HomeView(status=status, opener_id=1, is_admin=True)
    return {c.label for c in view.children if getattr(c, "label", None)}


# Each entry is a league state that drives `_next_step_text` down one of
# its branches, so the parametrisation covers every branch that names a
# destination.
_NEXT_STEP_STATES = [
    league_status(season=None, season_id=None, tiers=[], has_config=False),
    league_status(tiers=[], has_config=False),
    league_status(has_config=False),
    league_status(tiers=[tier_status(drivers=0)]),
    league_status(offers=2),
    league_status(boards=0),
    league_status(),
]


@pytest.mark.parametrize("status", _NEXT_STEP_STATES)
def test_next_step_only_names_buttons_that_exist(status):
    """
    Regression: the home panel's next-step line used to point at a
    **Boards** button that had never been built, and at **Setup** and
    **Approvals** screens that were only static text. Any destination the
    panel names in bold must be a button that is really on the screen.
    """
    import re

    from bot.cogs.panel import _next_step_text

    text = _next_step_text(status)
    named = re.findall(r"\*\*([^*]+)\*\*", text)
    labels = _home_labels(status)

    for target in named:
        # Tier codes are also bolded in the publish-pending branch; only
        # destinations are checked, and those are the capitalised words.
        if target.islower():
            continue
        assert any(label.startswith(target) for label in labels), (
            f"next step names {target!r} but the home panel only offers {labels}"
        )


def test_home_offers_boards_once_tiers_exist():
    assert any(
        label.startswith("Boards") for label in _home_labels(league_status())
    )


def test_home_offers_approvals_even_when_the_queue_is_empty():
    labels = _home_labels(league_status(offers=0, trades=0))

    assert any(label.startswith("Approvals") for label in labels), (
        "a button that appears only when there is work is harder to learn "
        "than one that is always in the same place"
    )


def test_approvals_button_shows_the_count():
    labels = _home_labels(league_status(offers=2, trades=1))

    assert "Approvals (3)" in labels


def test_home_hides_race_night_before_there_are_tiers():
    labels = _home_labels(league_status(tiers=[], has_config=False))

    assert not any("Race Night" in label for label in labels)
