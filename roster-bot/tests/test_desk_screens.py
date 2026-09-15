"""
Market (Screen 7), Contracts (Screen 10) and Trades (Screen 11).

Two kinds of test, following `tests/test_money_screen.py`.

Composition tests build the state objects by hand and assert what the
screens must *say*: a missing market value reads "— unknown" and never
0, every list states its page and its total, an arm/confirm note
restates the consequence before the route appears, and every outcome
note also says what did **not** change.

Behaviour tests drive the views through their callbacks against a real
migrated database with a fake interaction. They cover the three things
that would silently break:

  * the desks are **read-only** because `bot/workflow.py` exposes no
    offer, release or trade operation (gap G1). A confirm must therefore
    leave the database exactly as it was — asserted by re-reading the
    contract and the trade after the second click, not by trusting that
    nothing raised.
  * every render **re-reads** state, so a contract or trade that moved
    between the select being drawn and clicked is reported as gone
    rather than rendered from the stale snapshot.
  * `market_render.render_driver_card` cannot be used at all: it indexes
    `latest["rank_in_tier"]`, which `fetch_driver_valuation_history`
    does not select (defect D1). The card built here is asserted to show
    the rank, which is what that defect costs `/market driver`.

DB-backed tests are kept few on purpose: `pg_conn_migrated` replays the
whole migration chain per test.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from bot import queries, workflow
from bot.presets import f1 as f1_preset
from bot.ui import contracts_screen, market_screen, trades_screen
from bot.ui.base import EMBED_FIELD_LIMIT, SELECT_MAX_OPTIONS

GUILD = 1
CAP = Decimal("145.00")
NOW = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

_DESCRIPTION_LIMIT = 4096
_EMBED_TOTAL_LIMIT = 6000
_SELECT_LABEL_LIMIT = 100
_VIEW_ROWS = 5
_VIEW_CHILDREN = 25


# ── fakes ────────────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, calls: list) -> None:
        self._calls = calls
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **kwargs) -> None:
        self._done = True
        self._calls.append(("defer", kwargs))

    async def edit_message(self, **kwargs) -> None:
        self._done = True
        self._calls.append(("edit_message", kwargs))

    async def send_message(self, content=None, **kwargs) -> None:
        self._done = True
        self._calls.append(("send_message", {"content": content, **kwargs}))

    async def send_modal(self, modal) -> None:
        self._done = True
        self._calls.append(("send_modal", {"modal": modal}))


class _FakeFollowup:
    def __init__(self, calls: list) -> None:
        self._calls = calls

    async def send(self, content=None, **kwargs) -> None:
        self._calls.append(("followup", {"content": content, **kwargs}))


class FakeInteraction:
    """Enough of `discord.Interaction` for a panel callback."""

    def __init__(
        self, *, guild_id: int = GUILD, user_id: int = 7, role_ids: tuple = ()
    ) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = _FakeResponse(self.calls)
        self.followup = _FakeFollowup(self.calls)
        self.guild_id = guild_id
        self.user = SimpleNamespace(
            id=user_id, roles=[SimpleNamespace(id=r) for r in role_ids]
        )
        self.client = object()

    async def edit_original_response(self, **kwargs):
        self.calls.append(("edit_original_response", kwargs))
        return SimpleNamespace(id=1)

    async def original_response(self):
        return SimpleNamespace(id=1)

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.calls]

    def edits(self) -> list[dict]:
        return [
            payload
            for kind, payload in self.calls
            if kind in ("edit_message", "edit_original_response")
        ]

    def embeds(self) -> list[discord.Embed]:
        return [p["embed"] for p in self.edits() if p.get("embed") is not None]

    def views(self) -> list[discord.ui.View]:
        return [p["view"] for p in self.edits() if p.get("view") is not None]

    def notes(self) -> list[str]:
        return [
            payload["content"]
            for kind, payload in self.calls
            if kind in ("followup", "send_message") and payload.get("content")
        ]


async def noop_back(_interaction):  # pragma: no cover - navigation stub
    return None


def labels(view: discord.ui.View) -> set[str]:
    return {c.label for c in view.children if getattr(c, "label", None)}


def pick(select: discord.ui.Select, value: str) -> None:
    """Pretend the owner chose `value` — what Discord posts back."""
    select._values = [value]


def child(view: discord.ui.View, kind, *, index: int = 0):
    return [c for c in view.children if isinstance(c, kind)][index]


def text_of(embed: discord.Embed) -> str:
    parts = [embed.title or "", embed.description or ""]
    parts += [f.name + "\n" + f.value for f in embed.fields]
    if embed.footer and embed.footer.text:
        parts.append(embed.footer.text)
    return "\n".join(parts)


def assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed.description or "") <= _DESCRIPTION_LIMIT
    for field in embed.fields:
        assert len(field.value) <= EMBED_FIELD_LIMIT, field.name
    assert len(embed) <= _EMBED_TOTAL_LIMIT


def assert_view_within_limits(view: discord.ui.View) -> None:
    assert len(view.children) <= _VIEW_CHILDREN
    for item in view.children:
        row = getattr(item, "row", None)
        if row is not None:
            assert row < _VIEW_ROWS, f"row {row} is out of range"
        options = getattr(item, "options", None) or []
        assert len(options) <= SELECT_MAX_OPTIONS
        for option in options:
            assert len(option.label) <= _SELECT_LABEL_LIMIT, option.label
            if option.description:
                assert len(option.description) <= _SELECT_LABEL_LIMIT


# ── pure builders ────────────────────────────────────────────────────


def tier_ref(code="t1", *, tier_id=11, label="Tier 1", rank=1, role_id=None):
    return market_screen.TierRef(
        tier_id=tier_id,
        code=code,
        label=label,
        rank_order=rank,
        role_id=role_id,
        accent_color=None,
    )


def market_state(*tiers, teams=(), season="S7", season_id=1):
    return market_screen.MarketState(
        season_name=season,
        season_id=season_id,
        tiers=list(tiers),
        teams=list(teams),
    )


def market_row(driver_id, name, *, value="20.00", delta="1.00", rank=1):
    return {
        "driver_id": driver_id,
        "display_name": name,
        "market_value": None if value is None else Decimal(value),
        "previous_value": None,
        "delta": None if delta is None else Decimal(delta),
        "rank_in_tier": rank,
        "capped": False,
    }


def tier_market(tier, rows, *, published=True, round_label="Round 3", driver_count=None):
    return market_screen.TierMarket(
        tier=tier,
        round_label=round_label if published else None,
        published=published,
        rows=rows,
        risers=[],
        fallers=[],
        pl_rows=[],
        driver_count=driver_count if driver_count is not None else len(rows),
    )


def desk_team(key="mcl", name="McLaren", team_id=101, principal_role_id=None):
    return contracts_screen.DeskTeam(
        team_id=team_id,
        key=key,
        name=name,
        color=None,
        principal_role_id=principal_role_id,
    )


def desk_state(
    *,
    team=None,
    payroll="120.00",
    dead="0.00",
    cap=CAP,
    slots_used=2,
    slots_total=2,
    balance="30.00",
    enforced=True,
    escrow=False,
    fa_open=True,
    offers=(),
    contracts=(),
    season_id=1,
):
    return contracts_screen.DeskState(
        season_name="S7",
        season_id=season_id,
        team=team or desk_team(),
        payroll=Decimal(payroll),
        dead_money=Decimal(dead),
        dead_rows=[],
        cap=cap,
        slots_used=slots_used,
        slots_total=slots_total,
        min_salary=Decimal("1.00"),
        max_salary=Decimal("40.00"),
        min_term_races=1,
        max_term_races=24,
        offer_ttl_hours=48,
        free_agency_open=fa_open,
        balance=None if balance is None else Decimal(balance),
        budgets_enforced=enforced,
        escrow_enabled=escrow,
        offers=list(offers),
        contracts=list(contracts),
    )


def contract_row(
    contract_id=501,
    *,
    driver_id=301,
    name="ZeezinDomar",
    value="12.50",
    market="20.75",
    tier_code="t1",
):
    return contracts_screen.ContractRow(
        contract_id=contract_id,
        driver_id=driver_id,
        driver_name=name,
        member_id=900 + driver_id,
        contract_value=Decimal(value),
        term_seasons=2,
        season_index=1,
        term_races=48,
        races_served_before=0,
        contract_type="standard",
        market_value=None if market is None else Decimal(market),
        tier_code=tier_code,
    )


def offer_row(offer_id=601, *, name="DuelExploration", salary="6.25", market="5.75"):
    return contracts_screen.OfferRow(
        offer_id=offer_id,
        driver_id=302,
        driver_name=name,
        member_id=902,
        state="pending_driver",
        kind="new",
        salary=Decimal(salary),
        term_seasons=1,
        term_races=24,
        expires_at=NOW + timedelta(hours=48),
        market_value=None if market is None else Decimal(market),
        validation={},
    )


def driver_pick(*, contracted_to=None, market="5.75", tier_code="t1"):
    return contracts_screen.DriverPick(
        driver_id=302,
        display_name="DuelExploration",
        member_id=902,
        status="active",
        tier_code=tier_code,
        tier_label="Tier 1",
        market_value=None if market is None else Decimal(market),
        contracted_to=contracted_to,
    )


def trade_row(*, trade_id=701, mine=101, theirs=102, proposer=102, state="pending_other"):
    return trades_screen.TradeRow(
        trade_id=trade_id,
        state=state,
        proposing_team_id=proposer,
        proposing_team_name="Williams" if proposer == theirs else "McLaren",
        other_team_id=theirs if proposer == mine else mine,
        other_team_name="McLaren" if proposer == theirs else "Williams",
        message="Straight swap.",
        expires_at=NOW + timedelta(hours=48),
        items=[
            trades_screen.TradeSide(
                contract_id=501,
                driver_name="ZeezinDomar",
                contract_value=Decimal("12.50"),
                market_value=Decimal("20.75"),
                term_seasons=2,
                season_index=1,
                from_team_id=mine,
                from_team_name="McLaren",
            ),
            trades_screen.TradeSide(
                contract_id=502,
                driver_name="Kaveman",
                contract_value=Decimal("9.00"),
                market_value=None,
                term_seasons=1,
                season_index=1,
                from_team_id=theirs,
                from_team_name="Williams",
            ),
        ],
    )


# ── paging (the G16 guarantee) ───────────────────────────────────────


def test_page_count_never_reports_zero_pages():
    assert market_screen.page_count(0) == 1
    assert market_screen.page_count(25) == 1
    assert market_screen.page_count(26) == 2


def test_page_slice_clamps_and_keeps_every_item_reachable():
    items = list(range(60))
    seen: list[int] = []
    for page in range(market_screen.page_count(len(items))):
        window, clamped, pages = market_screen.page_slice(items, page)
        assert clamped == page
        assert pages == 3
        seen.extend(window)
    assert seen == items
    # A page past the end clamps back rather than rendering empty.
    window, clamped, _ = market_screen.page_slice(items, 99)
    assert clamped == 2 and window == items[50:]


def test_page_field_states_window_page_and_total():
    embed = discord.Embed(title="t")
    market_screen.page_field(
        embed, shown=25, page=1, pages=3, total=60, noun="drivers"
    )
    value = embed.fields[0].value
    assert embed.fields[0].name == "Page"
    assert "26–50 of 60" in value
    assert "page 2 of 3" in value
    assert "Nothing is hidden" in value


def test_page_field_empty_list_says_so_without_claiming_a_window():
    embed = discord.Embed(title="t")
    market_screen.page_field(
        embed, shown=0, page=0, pages=1, total=0, noun="offers"
    )
    assert "No offers to show" in embed.fields[0].value
    assert "0 total" in embed.fields[0].value


# ── unknown vs zero ──────────────────────────────────────────────────


def test_missing_money_reads_unknown_and_never_zero():
    assert market_screen.money_or_unknown(None) == market_screen.UNKNOWN
    assert "0" not in market_screen.money_or_unknown(None)
    assert market_screen.money_or_unknown(Decimal("1.50")) == "$1.50M"
    assert market_screen.pl_or_unknown(None, Decimal("1")) == market_screen.UNKNOWN
    assert market_screen.pl_or_unknown(Decimal("1"), None) == market_screen.UNKNOWN


def test_default_tier_prefers_the_viewers_own_tier():
    tiers = [
        tier_ref("t1", tier_id=11, label="Tier 1", rank=1, role_id=None),
        tier_ref("t2", tier_id=12, label="Tier 2", rank=2, role_id=555),
    ]
    assert market_screen.default_tier_code(tiers, {555}) == "t2"
    assert market_screen.default_tier_code(tiers, set()) == "t1"
    assert market_screen.default_tier_code([], {555}) is None


# ── Screen 7 rendering ───────────────────────────────────────────────


def test_market_table_states_total_and_flags_an_unpublished_tier():
    tier = tier_ref()
    rows: list = []
    state = market_state(tier)
    embed = market_screen.build_table_embed(
        state, tier_market(tier, rows, published=False, driver_count=18), page=0
    )
    body = text_of(embed)
    assert "needs a published run" in body
    assert "18 enrolled" in body
    assert "Nothing here is zero" in body
    assert_embed_within_limits(embed)


def test_market_table_pages_at_the_house_size_and_never_truncates_silently():
    tier = tier_ref()
    rows = [market_row(i, f"Driver {i}", rank=i) for i in range(1, 31)]
    state = market_state(tier)
    embed = market_screen.build_table_embed(state, tier_market(tier, rows), page=1)
    body = text_of(embed)
    assert "page 2 of 3" in body
    assert "of 30" in body
    assert_embed_within_limits(embed)


def test_market_table_reports_drivers_absent_from_the_published_run():
    tier = tier_ref()
    rows = [market_row(1, "Only One")]
    embed = market_screen.build_table_embed(
        market_state(tier), tier_market(tier, rows, driver_count=4), page=0
    )
    assert "3 driver(s)" in text_of(embed)


def test_driver_card_shows_rank_and_pl_the_existing_renderer_cannot():
    card = market_screen.DriverCard(
        display_name="ZeezinDomar",
        tier_label="Tier 1",
        accent_color=None,
        status="active",
        round_label="Round 3",
        market_value=Decimal("20.75"),
        delta=Decimal("1.25"),
        rank_in_tier=2,
        capped=False,
        contract_value=Decimal("12.50"),
        contract_team="Williams",
        contract_term="season 1 of 2",
        open_offers=1,
        history=[
            {
                "round_label": "Round 2",
                "market_value": Decimal("19.50"),
                "delta": Decimal("0.75"),
            }
        ],
    )
    body = text_of(market_screen.build_driver_card_embed(card))
    assert "Rank in tier: 2" in body
    assert "$20.75M" in body and "$12.50M" in body
    assert "P/L: +$8.25M" in body
    assert "Open offers: 1" in body


def test_driver_card_without_a_valuation_says_unknown_not_zero():
    card = market_screen.DriverCard(
        display_name="Rookie",
        tier_label="Tier 3",
        accent_color=None,
        status="reserve",
        round_label=None,
        market_value=None,
        delta=None,
        rank_in_tier=None,
        capped=False,
        contract_value=None,
        contract_team=None,
        contract_term=None,
        open_offers=0,
        history=[],
    )
    body = text_of(market_screen.build_driver_card_embed(card))
    assert "Market: — unknown (needs a published run)" in body
    assert f"P/L: {market_screen.UNKNOWN}" in body
    assert "not zero" in body


def test_cap_sheet_without_config_refuses_to_invent_a_cap():
    sheet = market_screen.TeamCapSheet(
        team_name="McLaren",
        color=None,
        rows=[],
        payroll=Decimal("34.00"),
        cap=None,
        slots_used=2,
        slots_total=None,
        dead_money=Decimal("0"),
        dead_rows=[],
        balance=None,
        budgets_enforced=False,
        escrow_enabled=False,
    )
    body = text_of(market_screen.build_cap_sheet_embed(sheet))
    assert "no `league_config` row" in body
    assert market_screen.UNKNOWN in body
    assert "Cap space: $" not in body


# ── Screen 10 rendering ──────────────────────────────────────────────


def test_desk_header_distinguishes_missing_config_from_zero():
    embed = contracts_screen.build_desk_embed(
        desk_state(cap=None, slots_total=None, balance=None, fa_open=None)
    )
    body = text_of(embed)
    assert "Cap space: — unknown" in body
    assert "not the same as a balance of 0" in body
    assert "free agency — unknown" in body
    assert_embed_within_limits(embed)


def test_desk_header_states_whether_budgets_bind():
    enforced = text_of(contracts_screen.build_desk_embed(desk_state(enforced=True)))
    assert "must clear the budget too" in enforced
    lax = text_of(contracts_screen.build_desk_embed(desk_state(enforced=False)))
    assert "only the cap blocks a signing" in lax


def test_offers_list_survives_a_null_deadline_and_states_the_total():
    offer = offer_row()
    no_deadline = contracts_screen.OfferRow(
        **{**offer.__dict__, "offer_id": 602, "expires_at": None}
    )
    state = desk_state(offers=[offer, no_deadline])
    embed = contracts_screen.build_offers_embed(state, page=0)
    body = text_of(embed)
    assert "of 2" in body
    assert market_screen.UNKNOWN in body
    assert "pending_driver" in body
    assert_embed_within_limits(embed)


def test_offer_detail_prints_the_withdraw_route_and_the_cap_effect():
    state = desk_state(offers=[offer_row()])
    body = text_of(contracts_screen.build_offer_detail_embed(state, offer_row()))
    assert "/contract withdraw offer_id: 601" in body
    assert "$126.25M" in body  # payroll after signing
    assert "Nothing was written" in body
    assert "cannot accept on their behalf" in body


def test_prepared_offer_blocks_on_closed_free_agency_and_full_seats():
    state = desk_state(fa_open=False, slots_used=2, slots_total=2)
    body = text_of(
        contracts_screen.build_offer_route_embed(state, driver_pick())
    )
    assert "Free agency is **closed**" in body
    assert "No free seat" in body
    assert "/contract offer" in body
    assert "team: mcl" in body and "tier: t1" in body and "<@902>" in body
    assert "Nothing was written" in body


def test_prepared_offer_warns_when_the_driver_is_already_signed():
    body = text_of(
        contracts_screen.build_offer_route_embed(
            desk_state(), driver_pick(contracted_to="Williams")
        )
    )
    assert "under contract to **Williams**" in body
    assert "trade, release or buyout" in body


def test_release_arm_note_restates_the_consequences_only_once_armed():
    state = desk_state(contracts=[contract_row()])
    unarmed = text_of(
        contracts_screen.build_release_preview_embed(
            state, contract_row(), armed=False
        )
    )
    assert "Confirm what you are about to do" not in unarmed
    assert "Nothing was written" in unarmed
    armed = text_of(
        contracts_screen.build_release_preview_embed(
            state, contract_row(), armed=True
        )
    )
    assert "cannot be undone" in armed
    assert "dead money" in armed
    assert "open offers to that driver are **not** cancelled" in armed
    assert "non-fatal" in armed
    assert "tier and enrolment are untouched" in armed


def test_release_route_offers_both_commands_and_says_what_did_not_change():
    body = text_of(
        contracts_screen.build_release_route_embed(
            desk_state(contracts=[contract_row()]), contract_row()
        )
    )
    assert "/contract release contract_id: 501" in body
    assert "/contract buyout contract_id: 501" in body
    assert "Payroll is still $120.00M" in body
    assert "dead money still $0.00M" in body


def test_no_team_screen_explains_the_driver_route():
    body = text_of(
        contracts_screen.build_no_team_embed(admin=False, teams_exist=True)
    )
    assert "not a Team Principal" in body
    assert "/contract accept" in body
    empty = text_of(
        contracts_screen.build_no_team_embed(admin=False, teams_exist=False)
    )
    assert "no teams yet" in empty


# ── Screen 11 rendering and cap maths ────────────────────────────────


def test_swap_effect_moves_payroll_by_the_difference():
    effect = trades_screen.evaluate_swap(
        desk_state(payroll="120.00"),
        out_value=Decimal("12.50"),
        in_value=Decimal("9.00"),
    )
    assert effect.payroll_after == Decimal("116.50")
    assert effect.cap_space_after == CAP - Decimal("116.50")
    assert effect.over_cap is False
    assert effect.budget_blocks is True  # 30.00 budget, 116.50 payroll


def test_swap_effect_without_a_cap_is_unknown_not_a_pass():
    effect = trades_screen.evaluate_swap(
        desk_state(cap=None, balance=None),
        out_value=Decimal("5.00"),
        in_value=Decimal("40.00"),
    )
    assert effect.over_cap is None
    assert effect.cap_space_after is None
    lines = "\n".join(trades_screen.swap_lines(effect))
    assert "no `league_config` row" in lines
    assert "not the same as passing" in lines


def test_swap_effect_flags_going_over_the_cap():
    effect = trades_screen.evaluate_swap(
        desk_state(payroll="140.00", balance=None),
        out_value=Decimal("1.00"),
        in_value=Decimal("20.00"),
    )
    assert effect.over_cap is True
    assert "⛔ over the cap" in "\n".join(trades_screen.swap_lines(effect))


def test_trade_detail_shows_both_sides_and_arms_before_the_route():
    state = desk_state(team=desk_team(), contracts=[contract_row()])
    trade = trade_row()
    mine = trades_screen.evaluate_swap(
        state, out_value=Decimal("12.50"), in_value=Decimal("9.00")
    )
    embed = trades_screen.build_trade_detail_embed(
        state, trade, mine, None, armed=False
    )
    body = text_of(embed)
    assert "Leaving your team" in body and "ZeezinDomar" in body
    assert "Arriving at your team" in body and "Kaveman" in body
    assert market_screen.UNKNOWN in body  # their cap sheet unread
    assert "Confirm what you are about to do" not in body
    assert_embed_within_limits(embed)

    armed = text_of(
        trades_screen.build_trade_detail_embed(state, trade, mine, None, armed=True)
    )
    assert "cannot be undone" in armed
    assert "does not answer this trade" in armed
    assert "Nothing was written" in armed


def test_trade_route_matches_who_may_answer():
    state = desk_state()
    receiving = text_of(
        trades_screen.build_trade_route_embed(state, trade_row(proposer=102))
    )
    assert "/trade accept my_team: mcl trade_id: 701" in receiving
    assert "/trade decline" in receiving
    assert "/trade withdraw" not in receiving

    proposed = text_of(
        trades_screen.build_trade_route_embed(state, trade_row(proposer=101))
    )
    assert "/trade withdraw my_team: mcl trade_id: 701" in proposed
    assert "/trade accept" not in proposed


def test_prepared_trade_prints_both_ids_and_warns_across_tiers():
    mine = desk_state(team=desk_team())
    theirs = desk_state(team=desk_team("wil", "Williams", 102), payroll="90.00")
    my_row = contract_row(501, tier_code="t1")
    their_row = contract_row(
        502, driver_id=302, name="Kaveman", value="9.00", market=None, tier_code="t2"
    )
    embed = trades_screen.build_propose_review_embed(
        mine_state=mine,
        theirs_state=theirs,
        my_row=my_row,
        their_row=their_row,
        mine=trades_screen.evaluate_swap(
            mine, out_value=my_row.contract_value, in_value=their_row.contract_value
        ),
        theirs=trades_screen.evaluate_swap(
            theirs, out_value=their_row.contract_value, in_value=my_row.contract_value
        ),
    )
    body = text_of(embed)
    assert "my_contract_id: 501" in body
    assert "their_contract_id: 502" in body
    assert "not a like-for-like comparison" in body
    assert market_screen.UNKNOWN in body  # their driver has no market value
    assert "No trade row exists yet" in body
    assert_embed_within_limits(embed)


def test_trades_desk_header_names_the_open_states_and_the_one_for_one_shape():
    body = text_of(
        trades_screen.build_desk_embed(desk_state(), [trade_row()])
    )
    for state_name in queries.OPEN_TRADE_STATES:
        assert f"`{state_name}`" in body
    assert "exactly 1 contract each way" in body
    assert "History screen" in body


# ── database-backed behaviour ────────────────────────────────────────


@pytest.fixture
def desk_db(monkeypatch, pg_conn_migrated):
    """One connection shared by the screens, workflow and the test."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    for module in (workflow, market_screen, contracts_screen, trades_screen):
        monkeypatch.setattr(module.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def seed(conn, *, drivers: int = 2):
    """Season with the F1 preset, two teams, contracts, an offer, a trade."""
    season_id = await queries.insert_season(conn, GUILD, "S7", is_active=True)
    await f1_preset.seed_season(conn, season_id)
    tiers = {t.code: t for t in await queries.fetch_all_tiers(conn, season_id)}
    tier = tiers["t1"]
    mcl = await queries.insert_team(
        conn, GUILD, "mcl", "McLaren", 201, 301, None, None, None, 401
    )
    wil = await queries.insert_team(
        conn, GUILD, "wil", "Williams", 202, 302, None, None, None, 402
    )
    made = []
    for index in range(drivers):
        driver_id = await queries.insert_driver(
            conn, season_id, tier.id, 1000 + index, f"Driver {index}", "active"
        )
        made.append(driver_id)
    contract_ids = []
    for index, (driver_id, team_id) in enumerate(zip(made, (mcl, wil))):
        contract_ids.append(
            await queries.insert_contract(
                conn,
                season_id=season_id,
                tier_id=tier.id,
                driver_id=driver_id,
                team_id=team_id,
                contract_value=Decimal("12.50") + index,
                signing_bonus=Decimal("0"),
                max_incentives=Decimal("0"),
                term_seasons=1,
                contract_type="standard",
                state="active",
                value_at_signing=None,
                approved_by=None,
            )
        )
    offer_id = await queries.insert_offer(
        conn,
        season_id=season_id,
        tier_id=tier.id,
        driver_id=made[1],
        team_id=mcl,
        offered_by=7,
        offer_kind="new",
        salary=Decimal("14.00"),
        term_seasons=1,
        contract_type="standard",
        state="pending_driver",
        expires_at=NOW + timedelta(hours=48),
    )
    trade_id = await queries.insert_trade(
        conn,
        season_id=season_id,
        proposing_team_id=wil,
        other_team_id=mcl,
        proposed_by=9,
        state="pending_other",
        message="Straight swap.",
        expires_at=NOW + timedelta(hours=48),
    )
    await queries.insert_trade_item(
        conn, trade_id, from_team_id=wil, contract_id=contract_ids[1]
    )
    await queries.insert_trade_item(
        conn, trade_id, from_team_id=mcl, contract_id=contract_ids[0]
    )
    return SimpleNamespace(
        season_id=season_id,
        tier=tier,
        tiers=tiers,
        mcl=mcl,
        wil=wil,
        drivers=made,
        contracts=contract_ids,
        offer_id=offer_id,
        trade_id=trade_id,
    )


async def test_open_market_lands_on_a_tier_and_pages_without_typing(desk_db):
    await seed(desk_db, drivers=30)
    interaction = FakeInteraction()
    await market_screen.open_market(interaction, on_back=noop_back)
    assert interaction.kinds() == ["edit_message"]
    view = interaction.views()[-1]
    assert_view_within_limits(view)
    assert view.tier_code == "t1"
    body = text_of(interaction.embeds()[-1])
    assert "Tier 1" in body
    assert "no published run" in body.lower()

    # Drill into the driver browser: 30 drivers, 25 to a page, total stated.
    await child(view, market_screen._DriversButton).callback(interaction)
    browser = interaction.views()[-1]
    assert_view_within_limits(browser)
    select = child(browser, market_screen._DriverSelect)
    assert len(select.options) == SELECT_MAX_OPTIONS
    assert "of 30" in text_of(interaction.embeds()[-1])

    pick(select, select.options[0].value)
    await select.callback(interaction)
    card = text_of(interaction.embeds()[-1])
    assert "needs a published run" in card
    assert market_screen.UNKNOWN in card


async def test_contracts_desk_opens_for_a_principal_only(desk_db):
    await seed(desk_db)
    stranger = FakeInteraction(role_ids=())
    await contracts_screen.open_contracts(stranger, on_back=noop_back)
    assert "not a Team Principal" in text_of(stranger.embeds()[-1])

    principal = FakeInteraction(role_ids=(401,))
    await contracts_screen.open_contracts(principal, on_back=noop_back)
    body = text_of(principal.embeds()[-1])
    assert "McLaren" in body
    assert "1 open offer(s)" in body
    assert "$145.00M" in body  # cap from the F1 preset
    assert_view_within_limits(principal.views()[-1])


async def test_release_confirm_reveals_the_route_and_writes_nothing(desk_db):
    seeded = await seed(desk_db)
    interaction = FakeInteraction(role_ids=(401,))
    await contracts_screen.open_contracts(interaction, on_back=noop_back)
    desk = interaction.views()[-1]

    await child(desk, contracts_screen._ReleaseButton).callback(interaction)
    picker = interaction.views()[-1]
    select = child(picker, contracts_screen._ContractSelect)
    pick(select, str(seeded.contracts[0]))
    await select.callback(interaction)
    confirm = interaction.views()[-1]
    assert isinstance(confirm, contracts_screen._ReleaseConfirmView)
    assert not confirm.armed

    arm = child(confirm, contracts_screen._ReleaseArmButton)
    await arm.callback(interaction)
    assert confirm.armed
    assert arm.label == "Show route"
    assert "Confirm what you are about to do" in text_of(interaction.embeds()[-1])
    assert any("nothing has been written yet" in n.lower() for n in interaction.notes())

    await arm.callback(interaction)
    route = text_of(interaction.embeds()[-1])
    assert f"/contract release contract_id: {seeded.contracts[0]}" in route
    assert f"/contract buyout contract_id: {seeded.contracts[0]}" in route
    assert any("Still unchanged" in n for n in interaction.notes())

    # The outcome note is only honest if the row really is untouched.
    contract = await queries.fetch_contract_by_id(desk_db, seeded.contracts[0])
    assert contract is not None and contract.state == "active"
    assert await queries.fetch_team_payroll(desk_db, seeded.mcl) == Decimal("12.50")
    assert (
        await queries.fetch_dead_money_total(desk_db, seeded.mcl, seeded.season_id)
        == Decimal("0")
    )


async def test_release_picker_reports_a_contract_that_moved_meanwhile(desk_db):
    seeded = await seed(desk_db)
    interaction = FakeInteraction(role_ids=(401,))
    await contracts_screen.open_contracts(interaction, on_back=noop_back)
    desk = interaction.views()[-1]
    await child(desk, contracts_screen._ReleaseButton).callback(interaction)
    picker = interaction.views()[-1]
    select = child(picker, contracts_screen._ContractSelect)

    # Someone else ends the contract between the list and the click.
    # `contract_states` is a data table (ADR-001), so the state written
    # here is read from it rather than assumed: `released` is not a code,
    # and `bot/contracts/service.py` uses `terminated` for a release.
    codes = {
        r["code"] for r in await desk_db.fetch("SELECT code FROM contract_states")
    }
    assert "released" not in codes
    assert "terminated" in codes
    await desk_db.execute(
        "UPDATE contracts SET state = $2 WHERE id = $1",
        seeded.contracts[0],
        "terminated",
    )
    pick(select, str(seeded.contracts[0]))
    await select.callback(interaction)
    assert any("no longer active" in note for note in interaction.notes())


async def test_trades_desk_answers_a_trade_with_a_route_and_no_write(desk_db):
    seeded = await seed(desk_db)
    interaction = FakeInteraction(role_ids=(401,))
    await trades_screen.open_trades(interaction, on_back=noop_back)
    desk = interaction.views()[-1]
    assert "1 awaiting your answer" in text_of(interaction.embeds()[-1])

    await child(desk, trades_screen._OpenTradesButton).callback(interaction)
    listing = interaction.views()[-1]
    select = child(listing, trades_screen._TradeSelect)
    pick(select, str(seeded.trade_id))
    await select.callback(interaction)
    detail = text_of(interaction.embeds()[-1])
    assert "Leaving your team" in detail and "Arriving at your team" in detail
    assert "Your cap after the swap" in detail
    assert "Their cap after the swap" in detail

    view = interaction.views()[-1]
    arm = child(view, trades_screen._TradeArmButton)
    await arm.callback(interaction)
    assert "does not answer this trade" in text_of(interaction.embeds()[-1])
    await arm.callback(interaction)
    route = text_of(interaction.embeds()[-1])
    assert f"/trade accept my_team: mcl trade_id: {seeded.trade_id}" in route
    assert f"/trade decline my_team: mcl trade_id: {seeded.trade_id}" in route

    trade = await queries.fetch_trade_by_id(desk_db, seeded.trade_id)
    assert trade is not None and trade.state == "pending_other"
    assert len(await queries.fetch_trade_items(desk_db, seeded.trade_id)) == 2


async def test_propose_flow_builds_a_route_from_three_selects(desk_db):
    seeded = await seed(desk_db)
    interaction = FakeInteraction(role_ids=(401,))
    await trades_screen.open_trades(interaction, on_back=noop_back)
    desk = interaction.views()[-1]

    await child(desk, trades_screen._ProposeButton).callback(interaction)
    partner = interaction.views()[-1]
    partner_select = child(partner, trades_screen._PickerSelect)
    assert [o.label for o in partner_select.options] == ["Williams"]
    pick(partner_select, str(seeded.wil))
    await partner_select.callback(interaction)

    mine = interaction.views()[-1]
    my_select = child(mine, trades_screen._PickerSelect)
    pick(my_select, str(seeded.contracts[0]))
    await my_select.callback(interaction)

    theirs = interaction.views()[-1]
    their_select = child(theirs, trades_screen._PickerSelect)
    pick(their_select, str(seeded.contracts[1]))
    await their_select.callback(interaction)

    review = text_of(interaction.embeds()[-1])
    assert "/trade propose" in review
    assert f"my_contract_id: {seeded.contracts[0]}" in review
    assert f"their_contract_id: {seeded.contracts[1]}" in review
    assert "my_team: mcl" in review and "other_team: wil" in review
    assert "No trade row exists yet" in review
    assert await queries.fetch_open_trades_for_team(desk_db, seeded.mcl) != []
    # The seeded trade is the only one; proposing wrote nothing new.
    assert len(await queries.fetch_open_trades_for_team(desk_db, seeded.mcl)) == 1
