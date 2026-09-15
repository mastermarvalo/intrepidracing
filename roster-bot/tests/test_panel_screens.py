"""
Rendering and composition of the three interactive screens.

These are pure-function tests: build the state, render the screen, assert
it stays inside Discord's limits and says the right thing. A screen that
exceeds a limit does not degrade gracefully — Discord rejects the whole
message, so the panel would simply fail to open.
"""

import asyncio
from datetime import datetime, timezone
from decimal import Decimal

import discord
import pytest

from bot import approvals, workflow
from bot.ui import (
    approvals_screen,
    boards_screen,
    drivers_screen,
    history_screen,
    setup_screen,
)
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
    values = _all_option_values(view)

    assert values == {"offer:5", "trade:5"}, (
        "an offer and a trade can share an id, so the kind must be in the value"
    )


def _all_option_values(view: discord.ui.View) -> set[str]:
    return {
        o.value
        for c in view.children
        if isinstance(c, discord.ui.Select)
        for o in c.options
    }


def test_offers_and_trades_each_get_their_own_select():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(1)], trades=[trade(1)]),
        opener_id=1,
        on_back=noop_back,
    )

    assert any(
        isinstance(c, approvals_screen._OfferSelect) for c in view.children
    )
    assert any(
        isinstance(c, approvals_screen._TradeSelect) for c in view.children
    )


def test_a_busy_offer_queue_never_hides_a_pending_trade():
    """G17: one shared select cut at 25 made trades unreachable."""
    view = approvals_screen.ApprovalsView(
        queue=queue(
            offers=[offer(i) for i in range(40)], trades=[trade(777)]
        ),
        opener_id=1,
        on_back=noop_back,
    )

    assert "trade:777" in _all_option_values(view), (
        "the embed lists the trade, so the picker has to be able to reach it"
    )
    assert_view_within_limits(view)


def test_each_queue_select_stays_inside_the_option_limit():
    view = approvals_screen.ApprovalsView(
        queue=queue(
            offers=[offer(i) for i in range(60)],
            trades=[trade(i) for i in range(60)],
        ),
        opener_id=1,
        on_back=noop_back,
    )

    for child in view.children:
        if isinstance(child, discord.ui.Select):
            assert len(child.options) <= SELECT_MAX_OPTIONS
    assert_view_within_limits(view)


def test_both_queues_get_their_own_paging_buttons():
    view = approvals_screen.ApprovalsView(
        queue=queue(
            offers=[offer(i) for i in range(30)],
            trades=[trade(i) for i in range(30)],
        ),
        opener_id=1,
        on_back=noop_back,
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert {"Prev offers", "Next offers"} <= labels
    assert {"Prev trades", "Next trades"} <= labels
    assert_view_within_limits(view), "paging must not blow the 5-row budget"


def test_a_short_queue_gets_no_paging_buttons():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(1)], trades=[trade(1)]),
        opener_id=1,
        on_back=noop_back,
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert not any("Next" in label for label in labels), (
        "paging a single page is a button that does nothing"
    )


def test_the_second_offer_page_reaches_the_rest_of_the_queue():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(i) for i in range(30)]),
        opener_id=1,
        on_back=noop_back,
        offer_page=1,
    )
    values = _all_option_values(view)

    assert "offer:29" in values, "page 2 must show what page 1 could not"
    assert "offer:0" not in values


def test_an_out_of_range_page_clamps_instead_of_rendering_empty():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(1)]),
        opener_id=1,
        on_back=noop_back,
        offer_page=9,
    )

    assert view.offer_page == 0
    assert "offer:1" in _all_option_values(view)


def test_queue_embed_states_which_slice_is_pickable():
    view = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(i) for i in range(30)], trades=[trade(1)]),
        opener_id=1,
        on_back=noop_back,
    )
    blob = "\n".join(f.value for f in view.embed().fields)

    assert "page" in blob.lower(), "silent truncation is the defect (G17)"
    assert "approve-trade" in blob, "name the typed fallback for the rest"
    assert_embed_within_limits(view.embed())


# ── approvals: offer context + confirm (G13) ──────────────────────────


def offer_context(
    *,
    payroll_before="34.00",
    salary="22.00",
    bonus="1.50",
    cap="60.00",
    market="20.75",
    balance="50.00",
    slots_used=1,
    slots=2,
    notes=(),
):
    before = Decimal(payroll_before)
    after = before + Decimal(salary) + Decimal(bonus)
    return approvals_screen.OfferContext(
        payroll_before=before,
        payroll_after=after,
        salary_cap=Decimal(cap) if cap else None,
        budget_balance=Decimal(balance) if balance else None,
        budget_headroom_after=(
            Decimal(balance) - after if balance else None
        ),
        budgets_enforced=True,
        market_value=Decimal(market) if market else None,
        has_published_valuation=bool(market),
        slots_used=slots_used,
        active_driver_slots=slots,
        notes=tuple(notes),
        _salary=Decimal(salary),
    )


def test_offer_context_derives_cap_space_and_market_delta():
    context = offer_context()

    assert context.payroll_after == Decimal("57.50")
    assert context.cap_space_after == Decimal("2.50")
    assert not context.over_cap
    assert context.offer_vs_market == Decimal("-1.25"), (
        "market minus salary: a negative delta is a premium paid"
    )


def test_offer_context_flags_an_offer_that_breaks_the_cap():
    context = offer_context(payroll_before="55.00")

    assert context.over_cap
    assert context.cap_space_after < 0


def test_offer_context_leaves_unknowns_unknown():
    context = approvals_screen.OfferContext()

    assert context.cap_space_after is None, (
        "a zero here reads as a real cap; unknown has to stay unknown"
    )
    assert context.offer_vs_market is None
    assert not context.over_cap
    assert not context.slots_full


def test_offer_detail_shows_payroll_cap_budget_market_and_slots():
    embed = approvals_screen._build_offer_detail(offer(1), offer_context())
    names = " ".join(f.name for f in embed.fields).lower()
    blob = " ".join(f.value for f in embed.fields)

    assert "payroll" in names
    assert "cap space" in names
    assert "budget" in names
    assert "market value" in names
    assert "slots" in names
    assert "1 of 2 used" in blob
    assert "→" in blob, "payroll before → after is the point of the panel"
    assert_embed_within_limits(embed)


def test_offer_detail_says_outright_when_there_is_no_valuation():
    """G13: approving without a valuation loses the P/L baseline for good."""
    embed = approvals_screen._build_offer_detail(
        offer(1), offer_context(market="")
    )
    blob = " ".join(f.value for f in embed.fields).lower()

    assert "no published valuation" in blob
    assert "baseline" in blob
    assert_embed_within_limits(embed)


def test_offer_detail_without_context_admits_it_rather_than_implying_zero():
    embed = approvals_screen._build_offer_detail(offer(1))
    blob = " ".join(f.value for f in embed.fields).lower()

    assert "not loaded" in blob
    assert_embed_within_limits(embed)


def test_offer_detail_surfaces_context_it_could_not_verify():
    embed = approvals_screen._build_offer_detail(
        offer(1), offer_context(notes=["Budget position unavailable: nope"])
    )
    names = " ".join(f.name for f in embed.fields).lower()

    assert "could not verify" in names, (
        "a failed context read has to be visible, not silently absent"
    )


def test_approve_consequence_names_the_terms_being_approved():
    text = approvals_screen.approve_consequence_text(
        kind="offer", item_id=1, offer=offer(1), context=offer_context()
    )

    assert "ZeezinDomar" in text
    assert "McLaren" in text
    assert "Press again" in text


def test_approve_consequence_repeats_the_irreversible_warnings():
    text = approvals_screen.approve_consequence_text(
        kind="offer",
        item_id=1,
        offer=offer(1),
        context=offer_context(market="", payroll_before="55.00", slots_used=2),
    )

    assert "over the salary cap" in text
    assert "no free active-driver slot" in text.lower()
    assert "P/L baseline" in text


def test_approve_consequence_for_a_trade_names_both_teams():
    text = approvals_screen.approve_consequence_text(
        kind="trade", item_id=4, trade=trade(4)
    )

    assert "Williams" in text
    assert "Aston Martin" in text


class _FakeResponse:
    def __init__(self):
        self.edits = []

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)

    def is_done(self):
        return True


class _FakeFollowup:
    def __init__(self):
        self.sent = []

    async def send(self, content, **_kwargs):
        self.sent.append(content)


class _FakeInteraction:
    """Just enough interaction to exercise the arming branch."""

    def __init__(self):
        self.response = _FakeResponse()
        self.followup = _FakeFollowup()


def test_approve_arms_before_it_acts():
    """G13: approve used to fire on the first click, next to Reject."""
    parent = approvals_screen.ApprovalsView(
        queue=queue(offers=[offer(1)]), opener_id=7, on_back=noop_back
    )
    item = approvals_screen._ItemView(
        kind="offer",
        item_id=1,
        opener_id=7,
        parent=parent,
        offer=offer(1),
        context=offer_context(market=""),
    )
    button = next(
        c for c in item.children if getattr(c, "label", None) == "Approve"
    )
    interaction = _FakeInteraction()

    asyncio.run(button.callback(interaction))

    assert item._armed, "the first click must arm, not approve"
    assert button.label == "Confirm approval"
    assert button.style == discord.ButtonStyle.danger
    assert interaction.followup.sent, "say what the second click will do"
    assert "P/L baseline" in interaction.followup.sent[0]


def test_page_helpers_cover_the_whole_queue():
    rows = list(range(60))

    assert approvals_screen.page_count(0) == 1
    assert approvals_screen.page_count(60) == 3
    seen = []
    for page in range(approvals_screen.page_count(len(rows))):
        seen.extend(approvals_screen.page_slice(rows, page))

    assert seen == rows, "paging must reach every row exactly once"
    assert approvals_screen.page_slice(rows, 99) == []


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


# ── drivers screen ───────────────────────────────────────────────────


def driver_summary(code="t1", *, label=None, drivers=0, role_id=None):
    return workflow.TierDriverSummary(
        code=code,
        label=label or f"Tier {code[-1]}",
        tier_role_id=role_id,
        driver_count=drivers,
    )


def driver_detail(
    driver_id=1,
    *,
    display_name="Alonso",
    tier_code="t1",
    tier_label=None,
    status="active",
    team="Mercedes",
    contract_value="10.00",
    market_value="12.00",
):
    return workflow.DriverForPanel(
        driver_id=driver_id,
        member_id=driver_id * 100,
        display_name=display_name,
        tier_code=tier_code,
        tier_label=tier_label or f"Tier {tier_code[-1]}",
        status=status,
        active_contract_id=driver_id * 1000 if contract_value else None,
        active_team_name=team,
        contract_value=Decimal(contract_value) if contract_value else None,
        market_value=Decimal(market_value) if market_value else None,
    )


def test_drivers_empty_state_points_at_setup():
    embed = drivers_screen.build_drivers_embed([])

    assert "setup" in embed.description.lower()
    assert_embed_within_limits(embed)


def test_drivers_lists_each_tier_with_its_count():
    embed = drivers_screen.build_drivers_embed(
        [driver_summary("t1", drivers=20, role_id=100),
         driver_summary("t2", drivers=5, role_id=101)]
    )

    assert "t1" in embed.description
    assert "20 driver" in embed.description
    assert "t2" in embed.description
    assert not any("no role" in f.name.lower() for f in embed.fields)


def test_drivers_flags_tiers_missing_a_discord_role():
    embed = drivers_screen.build_drivers_embed(
        [driver_summary("t1", drivers=10, role_id=100),
         driver_summary("t2", drivers=0, role_id=None)]
    )

    warning = next(
        (f for f in embed.fields if "no Discord role" in f.name), None
    )
    assert warning is not None, "unset roles must be surfaced, not hidden"
    assert "t2" in warning.value
    assert "t1" not in warning.value


def test_a_long_tier_list_stays_inside_the_description_limit():
    embed = drivers_screen.build_drivers_embed(
        [driver_summary(f"t{i}", drivers=i, role_id=1000 + i) for i in range(60)]
    )

    assert_embed_within_limits(embed)


def test_drivers_view_hides_sync_when_no_tier_has_a_role():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=None)],
        opener_id=1,
        on_back=noop_back,
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert "Sync all tiers" not in labels, "nothing to sync without a role"
    assert not any(
        isinstance(c, drivers_screen._SyncTierSelect) for c in view.children
    )
    assert "Enrol member" in labels, "enrol-by-mention still works without a role"
    assert_view_within_limits(view)


def test_drivers_view_offers_sync_once_a_tier_has_a_role():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        opener_id=1,
        on_back=noop_back,
    )
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert "Enrol member" in labels
    assert "Sync all tiers" in labels
    assert any(
        isinstance(c, drivers_screen._SyncTierSelect) for c in view.children
    )
    assert_view_within_limits(view)


def test_sync_select_only_offers_tiers_that_have_a_role():
    view = drivers_screen.DriversView(
        summaries=[
            driver_summary("t1", role_id=100),
            driver_summary("t2", role_id=None),
        ],
        opener_id=1,
        on_back=noop_back,
    )
    select = next(
        c for c in view.children if isinstance(c, drivers_screen._SyncTierSelect)
    )

    assert {o.value for o in select.options} == {"t1"}, (
        "offering a tier whose role isn't set would then fail on click"
    )


def test_sync_select_caps_its_options():
    view = drivers_screen.DriversView(
        summaries=[
            driver_summary(f"t{i}", role_id=100 + i) for i in range(40)
        ],
        opener_id=1,
        on_back=noop_back,
    )
    select = next(
        c for c in view.children if isinstance(c, drivers_screen._SyncTierSelect)
    )

    assert len(select.options) <= SELECT_MAX_OPTIONS
    assert_view_within_limits(view)


def test_enrol_flow_starts_on_the_tier_step():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        opener_id=1,
        on_back=noop_back,
    )
    flow = drivers_screen._EnrolMemberFlow(opener_id=1, parent=parent)

    assert flow.tier_code is None
    assert flow.member_id is None


def test_status_select_covers_every_workflow_status():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        opener_id=1,
        on_back=noop_back,
    )
    flow = drivers_screen._EnrolMemberFlow(opener_id=1, parent=parent)
    flow.tier_code = "t1"
    flow.member_id = 555
    flow.member_display_name = "Alonso"
    select = drivers_screen._EnrolStatusSelect(flow)

    assert {o.value for o in select.options} == {
        code for code, _ in workflow.DRIVER_STATUS_CHOICES
    }


def test_sync_line_shows_the_reason_a_tier_was_skipped():
    line = drivers_screen._format_sync_line(
        workflow.TierSyncReport(
            tier_code="t3", created=0, already_registered=0,
            skipped_reason="no Discord role set",
        )
    )

    assert "skipped" in line
    assert "no Discord role set" in line


def test_sync_line_shows_the_counts_when_a_sync_ran():
    line = drivers_screen._format_sync_line(
        workflow.TierSyncReport(
            tier_code="t1", created=3, already_registered=17,
        )
    )

    assert "3" in line
    assert "17" in line


# ── driver detail (per-driver admin actions) ─────────────────────────


def test_detail_embed_shows_value_status_and_pl_when_contracted():
    embed = drivers_screen.build_driver_detail_embed(driver_detail())
    blob = embed.description + "".join(f.value for f in embed.fields)

    assert "Mercedes" in blob
    assert "active" in blob
    assert "$12.00M" in blob  # market
    assert "$10.00M" in blob  # contract
    assert "$2.00M" in blob or "+$2.00M" in blob  # P/L
    assert_embed_within_limits(embed)


def test_detail_embed_degrades_when_driver_has_no_contract():
    embed = drivers_screen.build_driver_detail_embed(
        driver_detail(team=None, contract_value="")
    )
    blob = embed.description + "".join(f.value for f in embed.fields)

    assert "free agent" in blob.lower()
    assert "no active contract" in blob.lower()
    assert "P/L" not in "".join(f.name for f in embed.fields), (
        "showing $0.00 P/L for a driver with no contract implies a zero "
        "value where there is no value at all"
    )


def test_detail_embed_stays_useful_before_any_valuation_run():
    embed = drivers_screen.build_driver_detail_embed(
        driver_detail(market_value="", contract_value="")
    )

    assert any("no published run" in f.value for f in embed.fields)


def test_detail_embed_handles_contract_without_a_published_valuation():
    """
    Regression: the P/L field used to `assert detail.pl is not None`
    inside the `contract_value is not None` branch, but `pl` needs BOTH
    sides. A league that signed its rosters before publishing a
    valuation run has contracted drivers with no market value, and every
    click on the Drivers picker raised AssertionError.
    """
    embed = drivers_screen.build_driver_detail_embed(
        driver_detail(market_value="", contract_value="10.00")
    )
    values = "".join(f.value for f in embed.fields)
    names = [f.name for f in embed.fields]

    assert "$10.00M" in values, "the contract value is known and must show"
    assert "P/L" in names, "the field stays, so the layout does not shift"
    assert "no published run" in values
    assert_embed_within_limits(embed)


def test_detail_embed_pl_field_never_asserts_on_any_value_combination():
    """
    The panel must render for all four combinations of (market value,
    contract value) present/absent. Three of them are reachable states
    on a live server.
    """
    for market, contract in (
        ("12.00", "10.00"), ("", "10.00"), ("12.00", ""), ("", ""),
    ):
        embed = drivers_screen.build_driver_detail_embed(
            driver_detail(
                market_value=market,
                contract_value=contract,
                team="Mercedes" if contract else None,
            )
        )
        assert_embed_within_limits(embed)


def test_picker_labels_carry_tier_and_team():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail(display_name="Verstappen", team="Red Bull")],
        opener_id=1,
        on_back=noop_back,
    )
    picker = next(
        c for c in view.children
        if isinstance(c, drivers_screen._DriverPickerSelect)
    )

    assert picker.options[0].label == "Verstappen · t1 · Red Bull"


def test_picker_caps_and_reports_overflow():
    many = [driver_detail(driver_id=i, display_name=f"D{i}") for i in range(40)]
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=many,
        opener_id=1,
        on_back=noop_back,
    )
    picker = next(
        c for c in view.children
        if isinstance(c, drivers_screen._DriverPickerSelect)
    )

    assert len(picker.options) <= SELECT_MAX_OPTIONS
    assert "40" in picker.placeholder, (
        "if the picker is showing only the top 25 the count must say so"
    )


def test_drivers_view_without_drivers_shows_no_picker():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[],
        opener_id=1,
        on_back=noop_back,
    )

    assert not any(
        isinstance(c, drivers_screen._DriverPickerSelect) for c in view.children
    )
    assert_view_within_limits(view)


def tier_row(code="t1", *, rank=1, label=None, role_id=None):
    from bot.models import Tier

    return Tier(
        id=rank,
        season_id=1,
        code=code,
        label=label or f"Tier {code[-1]}",
        rank_order=rank,
        tier_role_id=role_id,
    )


def test_picker_pages_reach_every_driver():
    """G16: a silent "top 25 of N" made the rest of the league unreachable."""
    many = [driver_detail(driver_id=i, display_name=f"D{i}") for i in range(40)]
    seen: set[str] = set()
    for page in range(drivers_screen.page_count(len(many))):
        view = drivers_screen.DriversView(
            summaries=[driver_summary("t1", role_id=100)],
            drivers=many,
            opener_id=1,
            on_back=noop_back,
            page=page,
        )
        picker = next(
            c for c in view.children
            if isinstance(c, drivers_screen._DriverPickerSelect)
        )
        assert len(picker.options) <= SELECT_MAX_OPTIONS
        seen.update(o.value for o in picker.options)

    assert seen == {str(d.driver_id) for d in many}


def test_picker_gets_paging_buttons_only_when_it_needs_them():
    one_page = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )
    two_pages = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail(driver_id=i) for i in range(30)],
        opener_id=1,
        on_back=noop_back,
    )

    def labels(view):
        return {c.label for c in view.children if getattr(c, "label", None)}

    assert not any("Next drivers" in label for label in labels(one_page))
    assert "Next drivers" in labels(two_pages)
    assert "Prev drivers" in labels(two_pages)
    assert_view_within_limits(two_pages)


def test_tier_filter_narrows_the_picker_to_one_tier():
    view = drivers_screen.DriversView(
        summaries=[
            driver_summary("t1", role_id=100),
            driver_summary("t2", role_id=101),
        ],
        drivers=[
            driver_detail(driver_id=1, tier_code="t1"),
            driver_detail(driver_id=2, tier_code="t2"),
        ],
        opener_id=1,
        on_back=noop_back,
        tier_filter="t2",
    )
    picker = next(
        c for c in view.children
        if isinstance(c, drivers_screen._DriverPickerSelect)
    )

    assert [o.value for o in picker.options] == ["2"]
    assert any(
        isinstance(c, drivers_screen._DriverTierFilterSelect)
        for c in view.children
    )
    assert_view_within_limits(view)


def test_a_filter_naming_a_missing_tier_falls_back_to_all():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail(driver_id=1, tier_code="t1")],
        opener_id=1,
        on_back=noop_back,
        tier_filter="deleted",
    )

    assert view.tier_filter is None, (
        "an empty screen with no way to clear the filter is a dead end"
    )
    assert len(view.filtered) == 1


def test_drivers_embed_states_the_picker_scope_and_typed_fallback():
    embed = drivers_screen.build_drivers_embed(
        [driver_summary("t1", drivers=40, role_id=100)],
        picker_total=40,
        tier_filter="t1",
        page=1,
        pages=2,
    )
    blob = "\n".join(f.value for f in embed.fields)

    assert "t1" in blob
    assert "40" in blob
    assert "market-admin" in blob, "name where the rest of the league lives"
    assert_embed_within_limits(embed)


def test_promote_only_offers_tiers_above_and_relegate_only_below():
    """G14: one shared select let Promote relegate a driver."""
    tiers = [tier_row("t1", rank=1), tier_row("t2", rank=2), tier_row("t3", rank=3)]

    up = drivers_screen.move_targets(tiers, current_rank=2, direction="promote")
    down = drivers_screen.move_targets(
        tiers, current_rank=2, direction="relegate"
    )

    assert [t.code for t in up] == ["t1"]
    assert [t.code for t in down] == ["t3"]


def test_the_top_tier_has_nothing_to_promote_into():
    tiers = [tier_row("t1", rank=1), tier_row("t2", rank=2)]

    assert (
        drivers_screen.move_targets(
            tiers, current_rank=1, direction="promote"
        )
        == []
    )


def test_equal_ranked_tiers_are_not_a_direction():
    """rank_order is not unique (G25); a sideways move has no direction."""
    tiers = [tier_row("t2a", rank=2), tier_row("t2b", rank=2)]

    for direction in ("promote", "relegate"):
        assert (
            drivers_screen.move_targets(
                tiers, current_rank=2, direction=direction
            )
            == []
        )

    note = drivers_screen.no_move_target_note(
        tiers, current=tiers[0], direction="promote"
    )
    assert "t2b" in note, "name the tie so it can be fixed"
    assert "Setup" in note


def test_move_confirm_embed_states_the_direction():
    embed = drivers_screen.build_move_confirm_embed(
        driver_detail(),
        direction="relegate",
        current=tier_row("t1", rank=1),
        target=tier_row("t2", rank=2),
    )
    blob = f"{embed.title} {embed.description}"

    assert "relegate" in blob.lower()
    assert "down" in blob.lower()
    assert "t1" in blob and "t2" in blob
    assert "contract" in blob.lower(), (
        "the contract moving with the driver is the surprising part"
    )


def test_move_picker_pages_and_stays_in_the_row_budget():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )
    detail = drivers_screen._DriverDetailView(
        detail=driver_detail(), opener_id=1, parent=parent
    )
    targets = [tier_row(f"t{i}", rank=i + 2) for i in range(30)]
    picker = drivers_screen._MoveTierPickerView(
        parent=detail,
        direction="relegate",
        tiers=targets,
        current=tier_row("t1", rank=1),
    )
    select = next(
        c for c in picker.children if isinstance(c, discord.ui.Select)
    )
    labels = {c.label for c in picker.children if getattr(c, "label", None)}

    assert len(select.options) <= SELECT_MAX_OPTIONS
    assert "Next tiers" in labels
    assert_view_within_limits(picker)


def test_drivers_view_stays_inside_row_limit_with_picker_and_sync():
    view = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )

    assert_view_within_limits(view)


def test_detail_view_offers_the_four_actions_and_back():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )
    detail = drivers_screen._DriverDetailView(
        detail=driver_detail(), opener_id=1, parent=parent
    )
    labels = {c.label for c in detail.children if getattr(c, "label", None)}

    assert {"Void contract", "Set status", "Promote", "Relegate"} <= labels
    assert any("Back" in label for label in labels)
    assert_view_within_limits(detail)


def test_detail_view_disables_void_when_there_is_no_active_contract():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )
    free_agent = driver_detail(team=None, contract_value="")
    detail = drivers_screen._DriverDetailView(
        detail=free_agent, opener_id=1, parent=parent
    )
    void = next(
        c for c in detail.children if getattr(c, "label", "") == "Void contract"
    )

    assert void.disabled, (
        "voiding a nonexistent contract can only fail; disable at the "
        "button rather than raise on submit"
    )


def test_status_select_marks_the_current_status_as_default():
    parent = drivers_screen.DriversView(
        summaries=[driver_summary("t1", role_id=100)],
        drivers=[driver_detail()],
        opener_id=1,
        on_back=noop_back,
    )
    detail = drivers_screen._DriverDetailView(
        detail=driver_detail(status="reserve"), opener_id=1, parent=parent
    )
    select = drivers_screen._StatusSelect(detail)

    defaults = [o for o in select.options if o.default]
    assert len(defaults) == 1
    assert defaults[0].value == "reserve"


# ── history screen ───────────────────────────────────────────────────


def _run_summary(run_id=1, *, tier="t1", label="R1", published=False):
    return workflow.ValuationRunSummary(
        run_id=run_id,
        tier_code=tier,
        round_label=label,
        published=published,
        created_at=datetime(2026, 9, 14, tzinfo=timezone.utc),
    )


def _round_summary(*, tier="t1", order=1, label="R1", held=None, count=20):
    from datetime import date

    return workflow.RaceRoundSummary(
        tier_code=tier,
        round_order=order,
        round_label=label,
        held_on=held if held is not None else date(2026, 3, 15),
        result_count=count,
    )


def test_history_landing_offers_both_branches_and_back():
    view = history_screen.HistoryView(opener_id=1, on_back=noop_back)
    labels = {c.label for c in view.children if getattr(c, "label", None)}

    assert "Valuation runs" in labels
    assert "Race rounds" in labels
    assert any("Back" in label for label in labels)


def test_valuation_list_embed_shows_published_state():
    embed = history_screen.build_valuation_list_embed(
        [_run_summary(1, published=True), _run_summary(2, published=False)],
        tier_filter=None,
    )

    assert "published" in embed.description
    assert "dry-run" in embed.description


def test_valuation_list_empty_tells_the_admin_what_to_do():
    embed = history_screen.build_valuation_list_embed([], tier_filter="t1")

    assert "no runs" in embed.description.lower() or "no runs recorded" in embed.description.lower()
    assert "run" in embed.description.lower()


def test_rounds_list_shows_tier_and_result_count():
    embed = history_screen.build_rounds_list_embed(
        [_round_summary(tier="t1", order=1, count=18)],
        tier_filter=None,
    )

    assert "t1" in embed.description
    assert "18" in embed.description


def test_round_detail_marks_dnf_and_shows_exceptional_flag():
    detail = workflow.RaceRoundDetail(
        tier_code="t1", round_label="R1", round_order=1, held_on=None,
        results=[
            workflow.RoundResultRow(
                driver_name="Alonso", finish_position=1, grid_position=1,
                dnf=False, dns=False, fastest_lap=True, driver_of_day=False,
                incident_points=Decimal("0"), factor_values={"wins": Decimal("1.0")},
                exceptional=True,
            ),
            workflow.RoundResultRow(
                driver_name="Sainz", finish_position=None, grid_position=3,
                dnf=True, dns=False, fastest_lap=False, driver_of_day=False,
                incident_points=Decimal("2"), factor_values={},
                exceptional=False,
            ),
        ],
    )
    embed = history_screen.build_round_detail_embed(detail)

    assert "DNF" in embed.description
    assert "Alonso" in embed.description
    assert "exceptional" in embed.description.lower()
    assert "FL" in embed.description


def test_valuation_preview_embed_uses_the_published_color():
    preview = workflow.ValuationRunPreview(
        run_id=1, tier_code="t1", round_label="R1", published=True, rows=[],
    )
    embed = history_screen.build_valuation_preview_embed(preview)

    # Published runs use the OK color; dry-runs use INFO.
    assert embed.color.value == history_screen.COLOR_OK.value


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
        "Free agency",
    } <= labels


def test_seasons_embed_marks_the_active_season():
    from bot.models import Season

    now = datetime(2026, 9, 14, tzinfo=timezone.utc)
    embed = setup_screen._build_seasons_embed([
        Season(id=1, guild_id=1, name="S1", is_active=True, created_at=now),
        Season(id=2, guild_id=1, name="S2", is_active=False, created_at=now),
    ])

    assert "active" in embed.description
    assert "S1" in embed.description
    assert "S2" in embed.description


def test_tiers_embed_shows_role_link_status():
    from bot.models import Tier

    embed = setup_screen._build_tiers_embed([
        Tier(id=1, season_id=1, code="t1", label="Elite", rank_order=1,
             tier_role_id=999),
        Tier(id=2, season_id=1, code="t2", label="Pro", rank_order=2,
             tier_role_id=None),
    ])

    assert "t1" in embed.description
    assert "t2" in embed.description
    assert "role linked" in embed.description
    assert "no role" in embed.description


def test_free_agency_button_is_disabled_without_a_season():
    view = setup_screen.SetupView(
        status=league_status(season=None, season_id=None, tiers=[], has_config=False),
        opener_id=1,
        on_back=noop_back,
    )
    by_label = {c.label: c for c in view.children if getattr(c, "label", None)}

    assert by_label["Free agency"].disabled, (
        "toggling a flag on a config row that does not exist can only fail"
    )


def test_teams_button_appears_and_is_disabled_without_a_season():
    view = setup_screen.SetupView(
        status=league_status(season=None, season_id=None, tiers=[], has_config=False),
        opener_id=1,
        on_back=noop_back,
    )
    by_label = {c.label: c for c in view.children if getattr(c, "label", None)}

    assert "Teams" in by_label
    assert by_label["Teams"].disabled


def test_teams_embed_lists_names_and_payrolls():
    teams = [
        workflow.TeamForPanel(team_id=1, key="a", name="Alpha", payroll=Decimal("15.00")),
        workflow.TeamForPanel(team_id=2, key="b", name="Beta", payroll=Decimal("5.00")),
    ]
    embed = setup_screen._build_teams_embed(teams)

    assert "Alpha" in embed.description
    assert "$15.00M" in embed.description
    assert "Beta" in embed.description


def test_boards_view_exposes_single_board_refresh_when_boards_exist():
    view = boards_screen.BoardsView(
        boards=[board(1), board(2)], opener_id=1, on_back=noop_back
    )

    selects = [c for c in view.children if isinstance(c, discord.ui.Select)]
    assert any(
        isinstance(s, boards_screen._RefreshOneSelect) for s in selects
    )
    assert_view_within_limits(view)


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
        lambda: drivers_screen.DriversView(
            summaries=[driver_summary("t1", role_id=100)],
            opener_id=1,
            on_back=noop_back,
        ),
        lambda: drivers_screen._DriverDetailView(
            detail=driver_detail(),
            opener_id=1,
            parent=drivers_screen.DriversView(
                summaries=[driver_summary("t1", role_id=100)],
                drivers=[driver_detail()],
                opener_id=1,
                on_back=noop_back,
            ),
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
