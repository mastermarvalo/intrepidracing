"""
Teams & money (Screen 5): what it says, and what it refuses to say.

Two kinds of test live here.

Composition tests build a `MoneyState` by hand and assert the render
stays inside Discord's limits and states the things the screen exists to
state — above all the budgets header, because every number below it means
something different depending on whether budgets are enforced.

Behaviour tests drive the views through their callbacks against a real
migrated database with a fake interaction. They cover the two defects
this screen was written for:

  * **G18** — a cap adjustment is read by nothing in enforcement. The
    screen must say so before the write and again afterwards, and must
    never suggest the number changes what a team can spend.
  * **G1** — a write built from the state the screen opened with reverts
    whatever moved in between. Every confirm re-reads first, so a balance
    that changed while a modal was open is the one shown and the one
    written against.

DB-backed tests are deliberately few: `pg_conn_migrated` runs the whole
migration chain per test, so the assertions that do not need a database
are kept pure.
"""

from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from bot import queries, workflow
from bot.market import budget_ops
from bot.presets import f1 as f1_preset
from bot.ui import money_screen
from bot.ui.base import EMBED_FIELD_LIMIT, SELECT_MAX_OPTIONS

_LABEL_LIMIT = 80
_SELECT_LABEL_LIMIT = 100
_DESCRIPTION_LIMIT = 4096
_VIEW_ROWS = 5
_VIEW_CHILDREN = 25

GUILD = 1
CAP = Decimal("145.00")


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

    def __init__(self, *, guild_id: int = GUILD, user_id: int = 7) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = _FakeResponse(self.calls)
        self.followup = _FakeFollowup(self.calls)
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
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

    def notes(self) -> list[str]:
        return [
            payload["content"]
            for kind, payload in self.calls
            if kind in ("followup", "send_message") and payload.get("content")
        ]

    def modals(self) -> list:
        return [p["modal"] for k, p in self.calls if k == "send_modal"]


async def noop_back(_interaction):  # pragma: no cover - navigation stub
    return None


def labels(view: discord.ui.View) -> set[str]:
    return {c.label for c in view.children if getattr(c, "label", None)}


def pick(select: discord.ui.Select, value: str) -> None:
    """Pretend the owner chose `value` — what Discord posts back."""
    select._values = [value]


def child(view: discord.ui.View, kind):
    return next(c for c in view.children if isinstance(c, kind))


def assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed.description or "") <= _DESCRIPTION_LIMIT
    for field in embed.fields:
        assert len(field.value) <= EMBED_FIELD_LIMIT, field.name


def assert_view_within_limits(view: discord.ui.View) -> None:
    assert len(view.children) <= _VIEW_CHILDREN
    for item in view.children:
        row = getattr(item, "row", None)
        if row is not None:
            assert row < _VIEW_ROWS, f"row {row} is out of range"
        label = getattr(item, "label", None)
        if label:
            assert len(label) <= _LABEL_LIMIT, label
        for option in getattr(item, "options", []) or []:
            assert len(option.label) <= _SELECT_LABEL_LIMIT, option.label
            if option.description:
                assert len(option.description) <= _SELECT_LABEL_LIMIT


# ── builders ─────────────────────────────────────────────────────────


def row(
    key="mcl",
    *,
    name="McLaren",
    payroll="120.00",
    dead="0.00",
    cap=CAP,
    slots_used=2,
    balance="30.00",
    available="25.00",
):
    payroll_d = Decimal(payroll)
    dead_d = Decimal(dead)
    return money_screen.TeamMoneyRow(
        team_id=abs(hash(key)) % 10_000,
        key=key,
        name=name,
        payroll=payroll_d,
        dead_money=dead_d,
        effective_payroll=payroll_d + dead_d,
        cap=cap,
        slots_used=slots_used,
        slots_total=2,
        balance=None if balance is None else Decimal(balance),
        available=None if available is None else Decimal(available),
    )


def state(
    *rows,
    season="S7",
    season_id=1,
    configured=True,
    enforced=True,
    rollover=True,
):
    return money_screen.MoneyState(
        season_name=season,
        season_id=season_id,
        cap=CAP,
        budgets_configured=configured,
        budgets_enforced=enforced,
        rollover_enabled=rollover,
        rows=list(rows),
    )


def many_rows(count: int):
    return [row(key=f"t{i:02d}", name=f"Team {i:02d}") for i in range(count)]


# ── composition: the header decides how to read everything ───────────


def test_header_distinguishes_unconfigured_from_recorded_from_enforced():
    off = money_screen.budgets_header(state(row(), configured=False))
    recorded = money_screen.budgets_header(state(row(), enforced=False))
    live = money_screen.budgets_header(state(row()))

    assert "not configured" in off
    assert "NOT enforced" in recorded
    assert "enforced" in live and "NOT" not in live
    assert off != recorded != live


def test_header_states_whether_rollover_is_on():
    on = money_screen.budgets_header(state(row(), rollover=True))
    off = money_screen.budgets_header(state(row(), rollover=False))

    assert "Rollover is **on**" in on
    assert "Rollover is **off**" in off


def test_embed_carries_the_header_and_the_cap_note():
    embed = money_screen.build_money_embed(state(row()))

    assert money_screen.budgets_header(state(row())) in embed.description
    # G18 made cap adjustments real. This screen used to say they were
    # "an audit note only" and did "not change the cap", which was true
    # when written and is now the opposite of what the code does.
    assert "change what this team may spend" in embed.description
    assert "audit note only" not in embed.description
    assert_embed_within_limits(embed)


def test_embed_never_promises_future_enforcement():
    """G18: the 'lands in Phase 5' wording is not a promise this screen makes."""
    embed = money_screen.build_money_embed(state(row()))
    sheet = money_screen.build_cap_sheet_embed(row(), state(row()))
    haystack = f"{embed.description} {embed.footer.text} {sheet.description}"

    assert "Phase 5" not in haystack
    assert "lands in" not in haystack


def test_no_active_season_says_where_to_go():
    embed = money_screen.build_money_embed(state(season=None, season_id=None))

    assert "no active season" in embed.title.lower()
    assert "Setup" in embed.description


def test_no_teams_says_roles_still_own_membership():
    embed = money_screen.build_money_embed(state())

    assert "No teams yet" in embed.description
    assert "/roster create" in embed.description


# ── composition: every figure the brief asked for is on screen ────────


def test_each_row_shows_payroll_dead_cap_space_budget_available_slots():
    subject = row(payroll="100.00", dead="5.50", balance="30.00", available="12.25")
    embed = money_screen.build_money_embed(state(subject))

    assert "Payroll: $100.00M" in embed.description
    assert "Dead: $5.50M" in embed.description
    assert "Cap space: +$39.50M" in embed.description
    assert "Budget: $30.00M" in embed.description
    assert "Available: $12.25M" in embed.description
    assert "Slots: 2/2" in embed.description


def test_over_cap_team_is_flagged_and_colours_the_embed():
    embed = money_screen.build_money_embed(state(row(payroll="150.00")))

    assert "over cap" in embed.description
    assert embed.color == money_screen.COLOR_WARN


def test_cap_space_subtracts_dead_money_not_just_payroll():
    subject = row(payroll="144.00", dead="2.00")

    assert subject.effective_payroll == Decimal("146.00")
    assert subject.over_cap
    assert subject.cap_space == Decimal("-1.00")


def test_unconfigured_budgets_render_as_a_dash_never_as_zero():
    subject = row(balance=None, available=None)
    embed = money_screen.build_money_embed(state(subject, configured=False))
    sheet = money_screen.build_cap_sheet_embed(subject, state(subject, configured=False))

    assert "Budget: —" in embed.description
    assert "$0.00M" not in sheet.description.split("Budget balance")[1]
    assert "blank rather than zero" in sheet.description


def test_cap_sheet_shows_the_arithmetic_and_the_slot_count():
    subject = row(payroll="100.00", dead="5.00", slots_used=1)
    sheet = money_screen.build_cap_sheet_embed(subject, state(subject))

    assert "Payroll (active contract value): $100.00M" in sheet.description
    assert "Dead money this season: $5.00M" in sheet.description
    assert "Charged against the cap: $105.00M" in sheet.description
    assert "Active slots used: 1 of 2" in sheet.description
    assert_embed_within_limits(sheet)


def test_cap_sheet_says_when_budgets_are_recorded_but_not_enforced():
    subject = row()
    sheet = money_screen.build_cap_sheet_embed(
        subject, state(subject, enforced=False)
    )

    assert "not enforced" in sheet.description


# ── composition: filtering and paging, never silent truncation ────────


def test_filters_narrow_to_over_cap_and_to_negative_headroom():
    rows = [
        row(key="a", payroll="150.00"),
        row(key="b", available="-3.00"),
        row(key="c"),
    ]

    over = money_screen.filter_rows(rows, money_screen._FILTER_OVER_CAP)
    negative = money_screen.filter_rows(rows, money_screen._FILTER_NEGATIVE)

    assert [r.key for r in over] == ["a"]
    assert [r.key for r in negative] == ["b"]


def test_an_empty_filter_result_says_so_instead_of_looking_broken():
    embed = money_screen.build_money_embed(
        state(row()), team_filter=money_screen._FILTER_OVER_CAP
    )

    assert "No team matches this filter" in embed.description


def test_thirty_teams_page_rather_than_disappear():
    big = state(*many_rows(30))

    embed = money_screen.build_money_embed(big)
    view = money_screen.MoneyView(state=big, opener_id=1, on_back=noop_back)
    select = child(view, money_screen._TeamSelect)

    assert view.total_pages == 4
    assert len(select.options) <= SELECT_MAX_OPTIONS
    assert "page 1/4" in embed.description
    assert "30 team(s)" in embed.description
    assert_embed_within_limits(embed)
    assert_view_within_limits(view)


def test_paging_clamps_instead_of_rendering_an_empty_page():
    big = state(*many_rows(30))

    window, page, pages = money_screen.paginate_rows(big.rows, 99)

    assert page == pages - 1
    assert window


def test_the_last_page_of_a_ragged_list_is_not_empty():
    rows = many_rows(money_screen.TEAMS_PER_PAGE + 1)

    window, page, pages = money_screen.paginate_rows(rows, 1)

    assert pages == 2
    assert page == 1
    assert len(window) == 1


def test_view_stays_inside_discord_limits_with_long_team_names():
    long_name = "Scuderia " + "Ferrari " * 12
    big = state(*[row(key=f"k{i}", name=long_name) for i in range(9)])

    view = money_screen.MoneyView(state=big, opener_id=1, on_back=noop_back)

    assert_view_within_limits(view)
    assert_embed_within_limits(money_screen.build_money_embed(big))


def test_actions_are_disabled_when_there_is_nothing_to_act_on():
    view = money_screen.MoneyView(
        state=state(season=None, season_id=None), opener_id=1, on_back=noop_back
    )
    disabled = {c.label for c in view.children if getattr(c, "disabled", False)}

    assert "Award prize money" in disabled
    assert "Manual budget adjustment" in disabled
    assert "Cap adjustment (audit note)" in disabled
    assert "Back" in labels(view)


# ── composition: amount parsing ──────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2.5", Decimal("2.5")),
        (" $2.50M ", Decimal("2.50")),
        ("1,250.75", Decimal("1250.75")),
        ("-2.5", Decimal("-2.5")),
        ("+2.5", Decimal("2.5")),
    ],
)
def test_amounts_are_read_the_way_a_commissioner_types_them(raw, expected):
    assert money_screen._parse_amount(raw) == expected


@pytest.mark.parametrize("raw", ["", "two", "2.5.5", "M"])
def test_unreadable_amounts_raise_user_facing_text(raw):
    with pytest.raises(ValueError, match="amount in \\$M"):
        money_screen._parse_amount(raw)


def test_award_note_records_the_round_when_there_is_one():
    assert "R14" in money_screen._award_note("R14 Abu Dhabi", "P1 payout")
    assert money_screen._award_note(None, "P1 payout") == "Prize money: P1 payout"


# ── db fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def money_db(monkeypatch, pg_conn_migrated):
    """Point both `workflow.db` and the screen's own reads at the test schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(money_screen.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def seed_league(conn, *, guild_id=GUILD, teams=("mcl", "wil")):
    season_id = await queries.insert_season(conn, guild_id, "S7", is_active=True)
    await f1_preset.seed_season(conn, season_id)
    team_ids = {}
    for index, key in enumerate(teams):
        team_ids[key] = await queries.insert_team(
            conn,
            guild_id,
            key,
            key.upper(),
            1000 + index,
            2000 + index,
            None,
            None,
            None,
            None,
        )
    return season_id, team_ids


# ── behaviour: the state is read live, not from a snapshot ────────────


async def test_load_money_state_reports_the_seeded_cap_and_budget_config(money_db):
    await seed_league(money_db)

    loaded = await money_screen.load_money_state(GUILD)

    assert loaded.season_name == "S7"
    assert loaded.cap == CAP
    assert loaded.budgets_configured is True
    assert [r.key for r in loaded.rows] == ["mcl", "wil"]
    assert all(r.cap == CAP for r in loaded.rows)


async def test_load_money_state_does_not_write_an_opening_balance(money_db):
    """
    Reading the screen must not move money.

    `budget_ops.snapshot` credits an opening balance as a side effect of
    being asked; a read-only panel that used it would create ledger rows
    for every team the first time anyone looked at the screen.
    """
    season_id, _teams = await seed_league(money_db)

    await money_screen.load_money_state(GUILD)

    rows = await money_db.fetchval(
        "SELECT COUNT(*) FROM team_budget_ledger WHERE season_id = $1", season_id
    )
    assert rows == 0


async def test_a_write_by_someone_else_shows_up_on_the_next_read(money_db):
    """G1: the screen re-reads, so it cannot render a stale balance."""
    await seed_league(money_db)
    before = await money_screen.load_money_state(GUILD)

    await workflow.award_budget(
        guild_id=GUILD,
        actor_id=7,
        team_key="mcl",
        kind=budget_ops.KIND_PRIZE,
        amount=Decimal("5.00"),
        note="P1 payout",
    )
    after = await money_screen.load_money_state(GUILD)

    # The first write also seeds the opening balance the read deliberately
    # never creates, so the jump is the opening budget plus the prize.
    cfg = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    opening = cfg.opening_budget
    assert before.row("mcl").balance == Decimal("0")
    assert after.row("mcl").balance == opening + Decimal("5.00")


async def test_reload_edits_the_message_and_reports_the_note(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    view = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    interaction = FakeInteraction()

    await view.reload(interaction, note="✅ done")

    assert interaction.kinds()[0] == "edit_message"
    assert interaction.edits()[0]["view"].children
    assert interaction.notes() == ["✅ done"]


# ── behaviour: prize money previews before it writes ─────────────────


async def test_award_previews_the_balance_change_before_writing(money_db):
    season_id, _teams = await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    interaction = FakeInteraction()

    await money_screen._preview_budget_write(
        interaction,
        flow_parent=parent,
        on_cancel=noop_back,
        team_key="mcl",
        amount=Decimal("2.50"),
        kind=budget_ops.KIND_PRIZE,
        note="Prize money: P1 payout",
        title="🏆 Confirm prize money",
    )

    preview = interaction.edits()[0]["embed"]
    written = await money_db.fetchval(
        "SELECT COUNT(*) FROM team_budget_ledger WHERE season_id = $1", season_id
    )
    assert written == 0, "the preview must not write"
    assert "→" in preview.description
    assert "append-only" in preview.description
    assert isinstance(interaction.edits()[0]["view"], money_screen._ConfirmView)


async def test_confirming_the_award_writes_once_and_says_what_changed(money_db):
    season_id, teams = await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    interaction = FakeInteraction()
    await money_screen._preview_budget_write(
        interaction,
        flow_parent=parent,
        on_cancel=noop_back,
        team_key="mcl",
        amount=Decimal("2.50"),
        kind=budget_ops.KIND_PRIZE,
        note="Prize money: P1 payout",
        title="🏆 Confirm prize money",
    )
    confirm_view = interaction.edits()[0]["view"]

    confirm = FakeInteraction()
    await confirm_view.run(confirm)

    kinds = await queries.fetch_budget_totals_by_kind(
        money_db, teams["mcl"], season_id
    )
    assert kinds[budget_ops.KIND_PRIZE] == Decimal("2.50")
    note = confirm.notes()[0]
    assert "prize_money" in note
    assert "no contract changed" in note


async def test_prize_money_refuses_a_negative_amount_before_the_preview(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._AwardFlow(parent=parent)
    flow.team_key = "mcl"
    modal = money_screen._AwardModal(flow)
    modal._amount._value = "-2.5"
    modal._note._value = "clawback"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert not interaction.edits()
    assert "Manual budget adjustment" in interaction.notes()[0]


async def test_award_rounds_are_listed_newest_first(money_db):
    """
    CLAUDE.md §12: `list_race_rounds` is ascending, so the default round
    is the last element. A screen that trusted the raw order would offer
    round 1 as the obvious choice all season.
    """
    season_id, _teams = await seed_league(money_db)
    tier_id = await money_db.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    for order, label in ((1, "R1 Bahrain"), (2, "R2 Jeddah")):
        await money_db.execute(
            "INSERT INTO race_rounds (season_id, tier_id, round_order, round_label) "
            "VALUES ($1, $2, $3, $4)",
            season_id, tier_id, order, label,
        )
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._AwardFlow(parent=parent)
    interaction = FakeInteraction()

    await flow.start(interaction)

    select = child(flow, money_screen._AwardRoundSelect)
    round_labels = [o.label for o in select.options]
    assert round_labels[0] == "Not tied to a round"
    assert "R2 Jeddah" in round_labels[1]
    assert_view_within_limits(flow)


# ── behaviour: manual adjustment owns the sign ────────────────────────


async def test_a_debit_is_written_negative_even_though_the_modal_took_no_sign(
    money_db,
):
    season_id, teams = await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._AdjustFlow(parent=parent)
    flow.team_key = "mcl"
    flow.direction = money_screen._DEBIT
    modal = money_screen._AdjustModal(flow)
    modal._amount._value = "2.5"
    modal._note._value = "stewards' fine"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)
    preview = interaction.edits()[0]["embed"]
    await interaction.edits()[0]["view"].run(FakeInteraction())

    kinds = await queries.fetch_budget_totals_by_kind(
        money_db, teams["mcl"], season_id
    )
    assert "debit" in preview.title
    assert kinds[budget_ops.KIND_ADJUSTMENT] == Decimal("-2.5")


async def test_a_credit_and_a_debit_of_the_same_magnitude_are_opposites(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    seen = []
    for direction in (money_screen._CREDIT, money_screen._DEBIT):
        flow = money_screen._AdjustFlow(parent=parent)
        flow.team_key = "mcl"
        flow.direction = direction
        modal = money_screen._AdjustModal(flow)
        modal._amount._value = "2.5"
        modal._note._value = "test"
        interaction = FakeInteraction()
        await modal.on_submit(interaction)
        seen.append(interaction.edits()[0]["embed"].description)

    assert "+$2.50M" in seen[0]
    assert "−$2.50M" in seen[1]


async def test_a_zero_adjustment_is_refused_rather_than_written(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._AdjustFlow(parent=parent)
    flow.team_key = "mcl"
    flow.direction = money_screen._CREDIT
    modal = money_screen._AdjustModal(flow)
    modal._amount._value = "0"
    modal._note._value = "nothing"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert not interaction.edits()
    assert "zero adjustment" in interaction.notes()[0]


# ── behaviour: G18, a cap adjustment changes nothing ─────────────────


async def test_cap_adjustment_step_says_it_changes_spending(money_db):
    """
    G18: this step used to promise the adjustment changed nothing, and
    it now changes the ceiling every signing is validated against.
    """
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._CapAdjustFlow(parent=parent)
    interaction = FakeInteraction()

    await flow.render(interaction)

    description = interaction.edits()[0]["embed"].description
    assert "change what this team may spend" in description
    assert "validated against the adjusted ceiling" in description
    assert "audit note only" not in description
    assert "will **not** let a team sign" not in description
    assert "Phase 5" not in description
    # It must still distinguish itself from the two things it is not.
    assert "Cap & rules" in description
    assert "Manual budget" in description


async def test_cap_adjustment_confirm_shows_the_new_effective_cap(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._CapAdjustFlow(parent=parent)
    modal = money_screen._CapAdjustModal(flow, "mcl")
    modal._delta._value = "-2.5"
    modal._note._value = "stewards' sanction"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    embed = interaction.edits()[0]["embed"]
    # The confirm step used to say the cap stayed put and nothing about
    # spending changed — directly contradicting the note beneath it once
    # G18 made adjustments real.
    assert "Nothing about what this team can spend changes" not in embed.description
    assert "League cap $145.00M" in embed.description
    assert "Adjustments already applied" in embed.description
    assert "Effective cap after this change $142.50M" in embed.description


async def test_confirming_a_cap_adjustment_moves_the_effective_cap(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._CapAdjustFlow(parent=parent)
    modal = money_screen._CapAdjustModal(flow, "mcl")
    modal._delta._value = "-2.5"
    modal._note._value = "stewards' sanction"
    interaction = FakeInteraction()
    await modal.on_submit(interaction)

    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    after = await money_screen.load_money_state(GUILD)
    # The league cap itself is untouched — adjustments are per-team
    # ledger rows, not an edit to the season's cap.
    assert after.cap == CAP
    assert after.row("mcl").cap == CAP
    # But the team's own ceiling and cap space must both move, which is
    # exactly what this screen used to get wrong: the offer check
    # enforced the adjustment (G18) while the screen kept showing the
    # unadjusted figure.
    assert after.row("mcl").cap_adjustment == Decimal("-2.50")
    assert after.row("mcl").effective_cap == CAP - Decimal("2.50")
    assert after.row("mcl").cap_space == (
        loaded.row("mcl").cap_space - Decimal("2.50")
    )
    note = confirm.notes()[0]
    assert "read by nothing that enforces anything" not in note
    assert "Effective cap is now $142.50M" in note


async def test_a_zero_cap_note_records_nothing(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    modal = money_screen._CapAdjustModal(
        money_screen._CapAdjustFlow(parent=parent), "mcl"
    )
    modal._delta._value = "0"
    modal._note._value = "nothing"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert not interaction.edits()
    assert "records nothing" in interaction.notes()[0]


# ── behaviour: budget settings save the whole row ─────────────────────


async def test_toggling_enforcement_keeps_the_rates_it_was_not_asked_about(
    money_db,
):
    """
    G1 again: the toggle knows nothing about the five rates.

    A save assembled from the toggle alone would reset every rate to a
    default, silently changing what each race pays.
    """
    await seed_league(money_db)
    before = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    flow = money_screen._SettingsFlow(parent=parent)

    await money_screen._save_settings(
        FakeInteraction(), flow, enforce_budget=not before.enforce_budget
    )

    after = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    assert after.enforce_budget is (not before.enforce_budget)
    assert after.earnings_per_point == before.earnings_per_point
    assert after.opening_budget == before.opening_budget
    assert after.dnf_penalty == before.dnf_penalty
    assert after.rollover_enabled == before.rollover_enabled


async def test_editing_rates_keeps_the_enforcement_toggle(money_db):
    await seed_league(money_db)
    await money_screen._save_settings(
        FakeInteraction(),
        money_screen._SettingsFlow(
            parent=money_screen.MoneyView(
                state=await money_screen.load_money_state(GUILD),
                opener_id=7,
                on_back=noop_back,
            )
        ),
        enforce_budget=True,
    )

    loaded = await money_screen.load_money_state(GUILD)
    flow = money_screen._SettingsFlow(
        parent=money_screen.MoneyView(
            state=loaded, opener_id=7, on_back=noop_back
        )
    )
    await money_screen._save_settings(
        FakeInteraction(), flow, earnings_per_point=Decimal("0.75")
    )

    after = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    assert after.enforce_budget is True
    assert after.earnings_per_point == Decimal("0.75")


async def test_rates_modal_holds_exactly_five_inputs(money_db):
    await seed_league(money_db)
    cfg = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    loaded = await money_screen.load_money_state(GUILD)
    flow = money_screen._SettingsFlow(
        parent=money_screen.MoneyView(
            state=loaded, opener_id=7, on_back=noop_back
        )
    )

    modal = money_screen._RatesModal(flow, cfg, default_opening=loaded.cap)

    assert len(modal.children) == 5


async def test_negative_rates_are_refused_with_the_magnitude_rule(money_db):
    await seed_league(money_db)
    cfg = await workflow.get_budget_config(guild_id=GUILD, tier=None)
    loaded = await money_screen.load_money_state(GUILD)
    flow = money_screen._SettingsFlow(
        parent=money_screen.MoneyView(
            state=loaded, opener_id=7, on_back=noop_back
        )
    )
    modal = money_screen._RatesModal(flow, cfg, default_opening=loaded.cap)
    modal._dnf._value = "-1.0"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert "positive magnitudes" in interaction.notes()[0]


async def test_settings_scope_warns_that_tier_overrides_are_partial(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    flow = money_screen._SettingsFlow(
        parent=money_screen.MoneyView(
            state=loaded, opener_id=7, on_back=noop_back
        )
    )
    interaction = FakeInteraction()

    await flow.render_scope_step(interaction)

    description = interaction.edits()[0]["embed"].description
    option_labels = [
        o.label for o in child(flow, money_screen._ScopeSelect).options
    ]
    assert "partially wired" in description
    assert option_labels[0].startswith("Season default")
    assert any(label.startswith("t1") for label in option_labels[1:])


# ── behaviour: writes are refused when there is no ledger ─────────────


async def test_a_budget_write_is_refused_when_budgets_are_unconfigured(money_db):
    season_id, _teams = await seed_league(money_db)
    await money_db.execute(
        "DELETE FROM budget_config WHERE season_id = $1", season_id
    )
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    interaction = FakeInteraction()

    await money_screen._preview_budget_write(
        interaction,
        flow_parent=parent,
        on_cancel=noop_back,
        team_key="mcl",
        amount=Decimal("2.50"),
        kind=budget_ops.KIND_PRIZE,
        note="Prize money: P1 payout",
        title="🏆 Confirm prize money",
    )

    assert loaded.budgets_configured is False
    assert not interaction.edits()
    assert "not configured" in interaction.notes()[0]


async def test_a_write_against_a_deleted_team_is_refused_not_guessed(money_db):
    await seed_league(money_db)
    loaded = await money_screen.load_money_state(GUILD)
    parent = money_screen.MoneyView(state=loaded, opener_id=7, on_back=noop_back)
    interaction = FakeInteraction()

    await money_screen._preview_budget_write(
        interaction,
        flow_parent=parent,
        on_cancel=noop_back,
        team_key="ghost",
        amount=Decimal("2.50"),
        kind=budget_ops.KIND_PRIZE,
        note="Prize money",
        title="🏆 Confirm prize money",
    )

    assert not interaction.edits()
    assert "ghost" in interaction.notes()[0]
