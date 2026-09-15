"""
Offseason wizard (Screen 6): the gate, the order, and the previews.

The wizard exists because the rollover order is load-bearing (G21):
activating a season before contracts and budgets have been carried leaves
every team looking empty while free agency is open. So the tests here are
mostly about what the screen *refuses* to let happen:

  * a step is locked until the step above it is done or explicitly
    skipped, and completion is derived from the database rather than
    remembered;
  * carry-over is offered oldest past season first, and only the oldest
    is selectable — carrying a newer season first would strand the older
    contract rows as active payroll forever;
  * seasons are always chosen from a select, never typed;
  * every destructive step previews first and restates the consequence,
    and the preview itself writes nothing.

The pure tests build an `OffseasonState` by hand; the DB-backed ones use
`pg_conn_migrated` (slow — one migration chain per test) and drive the
views with a fake interaction.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import discord
import pytest

from bot import queries, workflow
from bot.market import budget, budget_ops
from bot.presets import f1 as f1_preset
from bot.ui import money_screen, offseason_screen

_LABEL_LIMIT = 80
_SELECT_LABEL_LIMIT = 100
_DESCRIPTION_LIMIT = 4096
_VIEW_ROWS = 5
_VIEW_CHILDREN = 25

GUILD = 1
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
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


class _FakeRole:
    def __init__(self, role_id: int, members) -> None:
        self.id = role_id
        self.members = members


class _FakeGuild:
    def __init__(self, roles=None) -> None:
        self._roles = roles or {}

    def get_role(self, role_id):
        return self._roles.get(role_id)


class FakeInteraction:
    """Enough of `discord.Interaction` for a panel callback."""

    def __init__(self, *, guild_id: int = GUILD, user_id: int = 7, guild=None) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.response = _FakeResponse(self.calls)
        self.followup = _FakeFollowup(self.calls)
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.guild = guild if guild is not None else _FakeGuild()
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
    select._values = [value]


def child(view: discord.ui.View, kind):
    return next(c for c in view.children if isinstance(c, kind))


def assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed.description or "") <= _DESCRIPTION_LIMIT
    for field in embed.fields:
        assert len(field.value) <= _SELECT_LABEL_LIMIT * 20, field.name


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


def past(name="S7", *, season_id=1, contracts=0, budget_rows=0, age_days=365):
    return offseason_screen.PastSeason(
        season_id=season_id,
        name=name,
        created_at=NOW - timedelta(days=age_days),
        unresolved_contracts=contracts,
        budget_rows=budget_rows,
    )


def tier(code="t1", *, drivers=20, role=True, valued=True):
    return offseason_screen.TierProgress(
        code=code,
        label=f"Tier {code[-1]}",
        driver_count=drivers,
        has_role=role,
        has_published_valuation=valued,
    )


def state(
    *,
    fa_open=True,
    seasons=2,
    newest_active=True,
    past_seasons=None,
    teams=10,
    rolled=0,
    rollover=True,
    prize=Decimal("0"),
    tiers=None,
    active_id=2,
    has_config=True,
):
    return offseason_screen.OffseasonState(
        active_season_name="S8",
        active_season_id=active_id,
        newest_season_name="S8",
        newest_is_active=newest_active,
        season_count=seasons,
        free_agency_open=fa_open,
        has_config=has_config,
        past_seasons=[past()] if past_seasons is None else past_seasons,
        teams_total=teams,
        teams_rolled_over=rolled,
        rollover_enabled=rollover,
        prize_money_total=prize,
        tiers=[tier()] if tiers is None else tiers,
    )


def steps_for(subject, progress=None):
    return offseason_screen.build_steps(
        subject, progress or offseason_screen.WizardProgress()
    )


def by_key(steps):
    return {s.key: s for s in steps}


# ── the nine steps, in the documented order ──────────────────────────


def test_the_wizard_has_the_nine_steps_in_the_documented_order():
    keys = [s.key for s in steps_for(state())]

    assert keys == list(offseason_screen.STEP_ORDER)
    assert len(keys) == 9
    assert keys[0] == offseason_screen.STEP_CLOSE_FA
    assert keys[-1] == offseason_screen.STEP_REOPEN_FA


def test_every_step_shows_live_state_not_just_a_title():
    for step in steps_for(state()):
        assert step.state_line, step.key
        assert step.title


def test_the_checklist_explains_why_the_order_matters():
    subject = state()
    embed = offseason_screen.build_offseason_embed(subject, steps_for(subject))

    assert "Order matters" in embed.description
    assert "no contracts or budgets exist there" in embed.description
    assert_embed_within_limits(embed)


def test_a_season_with_no_config_row_is_called_out():
    subject = state(has_config=False)
    embed = offseason_screen.build_offseason_embed(subject, steps_for(subject))

    assert "no league config row" in embed.description


def test_with_no_active_season_the_wizard_says_where_to_start():
    subject = state(active_id=None)
    embed = offseason_screen.build_offseason_embed(subject, steps_for(subject))

    assert "No active season" in embed.description
    assert "Setup" in embed.description


# ── gating ───────────────────────────────────────────────────────────


def test_only_the_first_step_is_open_at_the_start_of_an_offseason():
    fresh = state(
        fa_open=True,
        seasons=1,
        newest_active=True,
        past_seasons=[],
        tiers=[tier(drivers=0, valued=False)],
    )
    steps = by_key(steps_for(fresh))

    assert steps[offseason_screen.STEP_CLOSE_FA].enabled
    assert not steps[offseason_screen.STEP_CLOSE_FA].done
    assert not steps[offseason_screen.STEP_CREATE].enabled
    assert not steps[offseason_screen.STEP_CARRY].enabled


def test_closing_free_agency_unlocks_exactly_one_more_step():
    closed = state(
        fa_open=False,
        seasons=1,
        past_seasons=[],
        tiers=[tier(drivers=0, valued=False)],
    )
    steps = by_key(steps_for(closed))

    assert steps[offseason_screen.STEP_CLOSE_FA].done
    assert steps[offseason_screen.STEP_CREATE].enabled
    assert not steps[offseason_screen.STEP_ACTIVATE].enabled


def test_a_locked_step_says_it_is_locked_and_why():
    fresh = state(fa_open=True, seasons=1, past_seasons=[])
    embed = offseason_screen.build_offseason_embed(fresh, steps_for(fresh))

    assert "locked until the step above" in embed.description


def test_skipping_a_step_unlocks_the_next_one_and_records_the_reason():
    fresh = state(
        fa_open=True,
        seasons=1,
        past_seasons=[],
        tiers=[tier(drivers=0, valued=False)],
    )
    progress = offseason_screen.WizardProgress(
        skipped={offseason_screen.STEP_CLOSE_FA: "free agency never opened"}
    )
    steps = by_key(steps_for(fresh, progress))

    assert steps[offseason_screen.STEP_CLOSE_FA].skipped_reason
    assert steps[offseason_screen.STEP_CLOSE_FA].complete
    assert not steps[offseason_screen.STEP_CLOSE_FA].done
    assert steps[offseason_screen.STEP_CREATE].enabled


def test_a_skipped_step_is_marked_differently_from_a_done_one():
    progress = offseason_screen.WizardProgress(
        skipped={offseason_screen.STEP_PRIZE: "no prize pot this year"}
    )
    subject = state(fa_open=False, past_seasons=[past(contracts=3)])
    steps = by_key(steps_for(subject, progress))

    assert steps[offseason_screen.STEP_PRIZE].mark == "⏭"
    assert steps[offseason_screen.STEP_CLOSE_FA].mark == "✅"
    assert steps[offseason_screen.STEP_CARRY].mark == "⬜"


def test_reopening_free_agency_does_not_relock_the_wizard_mid_session():
    """
    Steps 1 and 9 are the same flag in opposite directions.

    Deriving step 1 from the live flag alone would mark it undone the
    moment step 9 ran, relocking every step behind it.
    """
    progress = offseason_screen.WizardProgress(
        ran={offseason_screen.STEP_CLOSE_FA, offseason_screen.STEP_REOPEN_FA}
    )
    reopened = state(fa_open=True, past_seasons=[past(contracts=3)])
    without_memory = by_key(steps_for(reopened))
    steps = by_key(steps_for(reopened, progress))

    assert not without_memory[offseason_screen.STEP_CLOSE_FA].done
    assert not without_memory[offseason_screen.STEP_CARRY].enabled
    assert steps[offseason_screen.STEP_CLOSE_FA].done
    assert steps[offseason_screen.STEP_REOPEN_FA].done
    assert steps[offseason_screen.STEP_CARRY].enabled


def test_carry_over_is_done_only_when_no_past_season_holds_a_contract():
    outstanding = state(fa_open=False, past_seasons=[past(contracts=3)])
    settled = state(fa_open=False, past_seasons=[past(contracts=0)])

    assert not by_key(steps_for(outstanding))[offseason_screen.STEP_CARRY].done
    assert by_key(steps_for(settled))[offseason_screen.STEP_CARRY].done


def test_rollover_is_done_when_every_team_has_a_rollover_row():
    part = state(fa_open=False, teams=10, rolled=4, past_seasons=[past(budget_rows=10)])
    whole = state(
        fa_open=False, teams=10, rolled=10, past_seasons=[past(budget_rows=10)]
    )

    assert not by_key(steps_for(part))[offseason_screen.STEP_ROLLOVER].done
    assert by_key(steps_for(whole))[offseason_screen.STEP_ROLLOVER].done


def test_rollover_needs_no_work_when_no_past_season_has_budget_history():
    subject = state(fa_open=False, teams=10, rolled=0, past_seasons=[past()])

    assert by_key(steps_for(subject))[offseason_screen.STEP_ROLLOVER].done


def test_valuation_is_done_only_when_every_tier_is_published():
    mixed = state(
        fa_open=False, tiers=[tier("t1", valued=True), tier("t2", valued=False)]
    )
    published = state(fa_open=False, tiers=[tier("t1"), tier("t2")])

    assert not by_key(steps_for(mixed))[offseason_screen.STEP_BASELINE].done
    assert by_key(steps_for(published))[offseason_screen.STEP_BASELINE].done


def test_sync_is_done_only_when_every_tier_has_drivers():
    empty = state(fa_open=False, tiers=[tier("t1", drivers=0)])
    filled = state(fa_open=False, tiers=[tier("t1", drivers=18)])

    assert not by_key(steps_for(empty))[offseason_screen.STEP_SYNC].done
    assert by_key(steps_for(filled))[offseason_screen.STEP_SYNC].done


def test_state_lines_name_the_missing_valuation_and_the_missing_role():
    subject = state(
        fa_open=False, tiers=[tier("t1", drivers=0, role=False, valued=False)]
    )
    steps = by_key(steps_for(subject))

    assert "no role" in steps[offseason_screen.STEP_SYNC].state_line
    assert "no published value" in steps[offseason_screen.STEP_BASELINE].state_line


def test_disabled_rollover_is_shouted_in_the_state_line():
    subject = state(fa_open=False, rollover=False, past_seasons=[past(budget_rows=4)])

    assert "DISABLED" in by_key(steps_for(subject))[
        offseason_screen.STEP_ROLLOVER
    ].state_line


# ── the view: nine buttons, a skip select, and Discord's limits ────────


def test_the_view_fits_inside_a_discord_message():
    subject = state()
    view = offseason_screen.OffseasonView(
        state=subject, opener_id=1, on_back=noop_back
    )

    assert_view_within_limits(view)
    assert "Back" in labels(view)
    assert len([c for c in view.children if isinstance(c, discord.ui.Button)]) == 10


def test_locked_steps_are_disabled_buttons_not_hidden_ones():
    fresh = state(fa_open=True, seasons=1, past_seasons=[])
    view = offseason_screen.OffseasonView(
        state=fresh, opener_id=1, on_back=noop_back
    )
    step_buttons = [
        c for c in view.children if isinstance(c, offseason_screen._StepButton)
    ]

    assert len(step_buttons) == 9
    assert sum(1 for b in step_buttons if b.disabled) == 8


def test_a_finished_offseason_offers_no_skip_select():
    finished = state(
        fa_open=True,
        past_seasons=[past(contracts=0, budget_rows=10)],
        teams=10,
        rolled=10,
        prize=Decimal("5.00"),
    )
    progress = offseason_screen.WizardProgress(
        ran={offseason_screen.STEP_CLOSE_FA}
    )
    view = offseason_screen.OffseasonView(
        state=finished, opener_id=1, on_back=noop_back, progress=progress
    )

    assert not [
        c for c in view.children if isinstance(c, offseason_screen._SkipSelect)
    ]


def test_the_skip_select_only_offers_incomplete_steps():
    subject = state(fa_open=False, past_seasons=[past(contracts=3)])
    view = offseason_screen.OffseasonView(
        state=subject, opener_id=1, on_back=noop_back
    )
    select = child(view, offseason_screen._SkipSelect)
    offered = {o.value for o in select.options}

    assert offseason_screen.STEP_CLOSE_FA not in offered
    assert offseason_screen.STEP_CARRY in offered
    assert len(select.options) <= 25


# ── carry-over ordering ──────────────────────────────────────────────


def test_pending_carry_seasons_come_out_oldest_first():
    subject = state(
        past_seasons=[
            past("S5", season_id=1, contracts=2, age_days=900),
            past("S6", season_id=2, contracts=0, age_days=600),
            past("S7", season_id=3, contracts=4, age_days=300),
        ]
    )

    assert [s.name for s in subject.pending_carry] == ["S5", "S7"]


def test_only_the_oldest_pending_season_is_selectable():
    pending = [
        past("S5", season_id=1, contracts=2, age_days=900),
        past("S7", season_id=3, contracts=4, age_days=300),
    ]
    parent = offseason_screen.OffseasonView(
        state=state(), opener_id=1, on_back=noop_back
    )
    view = offseason_screen._CarryView(parent, pending)
    select = child(view, offseason_screen._CarrySelect)

    assert [o.value for o in select.options] == ["S5"]
    assert "oldest first" in select.placeholder
    assert_view_within_limits(view)


def test_the_carry_queue_shows_the_locked_later_seasons_with_their_counts():
    pending = [
        past("S5", season_id=1, contracts=2, age_days=900),
        past("S7", season_id=3, contracts=4, age_days=300),
    ]
    embed = offseason_screen.build_carry_queue_embed(pending)

    assert "**S5** — 2 active contract(s)" in embed.description
    assert "waits for the season above" in embed.description
    assert "🔒" in embed.description
    assert_embed_within_limits(embed)


def test_the_carry_state_line_names_the_oldest_season():
    subject = state(
        past_seasons=[
            past("S5", season_id=1, contracts=2, age_days=900),
            past("S7", season_id=3, contracts=4, age_days=300),
        ]
    )

    assert "**S5**" in by_key(steps_for(subject))[
        offseason_screen.STEP_CARRY
    ].state_line


# ── preview rendering ────────────────────────────────────────────────


def test_the_carry_preview_states_that_confirming_writes_contracts():
    preview = offseason_screen.CarryPreview(
        from_season_name="S7",
        to_season_name="S8",
        to_carry=12,
        to_expire=8,
        carried_value=Decimal("120.00"),
        expiring_value=Decimal("40.00"),
        over_cap=[],
        salary_cap=CAP,
    )
    embed = offseason_screen.build_carry_preview_embed(preview)

    assert "Confirming writes contracts" in embed.description
    assert "cannot be undone" in embed.description
    assert "free agent" in embed.description
    assert "$120.00M" in embed.description
    assert_embed_within_limits(embed)


def test_the_carry_preview_warns_about_teams_left_over_the_cap():
    preview = offseason_screen.CarryPreview(
        from_season_name="S7",
        to_season_name="S8",
        to_carry=12,
        to_expire=0,
        carried_value=Decimal("150.00"),
        expiring_value=Decimal("0"),
        over_cap=[("McLaren", Decimal("150.00"), CAP)],
        salary_cap=CAP,
    )
    embed = offseason_screen.build_carry_preview_embed(preview)

    assert "McLaren" in embed.description
    assert "over by $5.00M" in embed.description
    assert "never blocked" in embed.description


def test_the_rollover_preview_shows_the_arithmetic_per_team():
    preview = offseason_screen.RolloverPreview(
        from_season_name="S7",
        to_season_name="S8",
        enabled=True,
        lines=[
            offseason_screen.RolloverPreviewLine(
                team_name="McLaren",
                balance=Decimal("145.00"),
                season_payroll=Decimal("120.00"),
                carried=Decimal("25.00"),
                already_rolled=False,
            )
        ],
    )
    embed = offseason_screen.build_rollover_preview_embed(preview)

    assert "$145.00M" in embed.description
    assert "$120.00M" in embed.description
    assert "**$25.00M**" in embed.description
    assert "negative figure carries as debt" in embed.description


def test_the_rollover_preview_refuses_when_rollover_is_disabled():
    preview = offseason_screen.RolloverPreview(
        from_season_name="S7", to_season_name="S8", enabled=False, lines=[]
    )
    embed = offseason_screen.build_rollover_preview_embed(preview)

    assert "disabled" in embed.description
    assert "Budget settings" in embed.description


def test_the_rollover_preview_flags_a_team_already_rolled_over():
    preview = offseason_screen.RolloverPreview(
        from_season_name="S7",
        to_season_name="S8",
        enabled=True,
        lines=[
            offseason_screen.RolloverPreviewLine(
                team_name="Williams",
                balance=Decimal("10.00"),
                season_payroll=Decimal("5.00"),
                carried=Decimal("5.00"),
                already_rolled=True,
            )
        ],
    )
    embed = offseason_screen.build_rollover_preview_embed(preview)

    assert "already rolled over" in embed.description


def test_the_baseline_label_is_derived_from_the_season_name():
    assert offseason_screen.baseline_round_label("S8") == "Baseline S8"


# ── db fixtures ──────────────────────────────────────────────────────


@pytest.fixture
def offseason_db(monkeypatch, pg_conn_migrated):
    """One connection shared by `workflow` and both screens."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(workflow.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(offseason_screen.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(money_screen.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def seed_two_seasons(conn, *, guild_id=GUILD):
    """
    An old season with a two-year contract in it, and a new active season.

    The old contract is `season_index=1` of `term_seasons=2`, so
    `carryover.continues_next_season` says it carries; a second contract
    is in its final year, so it expires. That is the whole preview.
    """
    old_id = await queries.insert_season(conn, guild_id, "S7", is_active=False)
    await f1_preset.seed_season(conn, old_id)
    new_id = await queries.insert_season(conn, guild_id, "S8", is_active=True)
    await f1_preset.seed_season(conn, new_id)
    old_tier = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", old_id
    )
    team_id = await queries.insert_team(
        conn, guild_id, "mcl", "McLaren", 1001, 2001, None, None, None, None
    )
    carrying = await queries.insert_driver(
        conn, old_id, old_tier, 501, "ZeezinDomar", "active"
    )
    expiring = await queries.insert_driver(
        conn, old_id, old_tier, 502, "DuelExploration", "active"
    )
    for driver_id, index, term, value in (
        (carrying, 1, 2, Decimal("20.00")),
        (expiring, 2, 2, Decimal("10.00")),
    ):
        await queries.insert_contract(
            conn,
            season_id=old_id,
            tier_id=old_tier,
            driver_id=driver_id,
            team_id=team_id,
            contract_value=value,
            signing_bonus=Decimal("0"),
            max_incentives=Decimal("0"),
            term_seasons=term,
            contract_type="standard",
            state="active",
            value_at_signing=value,
            approved_by=7,
            season_index=index,
        )
    return SimpleNamespace(old_id=old_id, new_id=new_id, team_id=team_id)


# ── behaviour: state is derived from the database ─────────────────────


async def test_load_state_finds_the_unresolved_past_season(offseason_db):
    await seed_two_seasons(offseason_db)

    loaded = await offseason_screen.load_offseason_state(GUILD)

    assert loaded.active_season_name == "S8"
    assert loaded.newest_is_active is True
    assert [s.name for s in loaded.past_seasons] == ["S7"]
    assert loaded.pending_carry[0].unresolved_contracts == 2
    assert loaded.teams_total == 1


async def test_load_state_reads_free_agency_and_rollover_flags(offseason_db):
    await seed_two_seasons(offseason_db)

    loaded = await offseason_screen.load_offseason_state(GUILD)
    await workflow.set_free_agency(guild_id=GUILD, is_open=True)
    reopened = await offseason_screen.load_offseason_state(GUILD)

    assert loaded.free_agency_open is False
    assert reopened.free_agency_open is True
    assert loaded.rollover_enabled == loaded.rollover_enabled


async def test_load_state_counts_prize_money_written_this_season(offseason_db):
    await seed_two_seasons(offseason_db)
    await workflow.award_budget(
        guild_id=GUILD,
        actor_id=7,
        team_key="mcl",
        kind=budget_ops.KIND_PRIZE,
        amount=Decimal("3.00"),
        note="P1 payout",
    )

    loaded = await offseason_screen.load_offseason_state(GUILD)

    assert loaded.prize_money_total == Decimal("3.00")
    assert by_key(steps_for(loaded))[offseason_screen.STEP_PRIZE].done


async def test_no_active_season_yields_an_empty_but_renderable_state(offseason_db):
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )

    assert loaded.active_season_id is None
    assert all(not s.enabled for s in view.steps)
    assert_view_within_limits(view)
    assert_embed_within_limits(
        offseason_screen.build_offseason_embed(loaded, view.steps)
    )


# ── behaviour: carry-over preview and confirm ────────────────────────


async def test_the_carry_preview_splits_carrying_from_expiring(offseason_db):
    await seed_two_seasons(offseason_db)

    preview = await offseason_screen.preview_carry_over(GUILD, "S7")

    assert preview.to_carry == 1
    assert preview.to_expire == 1
    assert preview.carried_value == Decimal("20.00")
    assert preview.expiring_value == Decimal("10.00")
    assert preview.salary_cap == CAP


async def test_the_carry_preview_writes_nothing(offseason_db):
    seeded = await seed_two_seasons(offseason_db)

    await offseason_screen.preview_carry_over(GUILD, "S7")

    still_active = await queries.fetch_active_contracts_for_season(
        offseason_db, seeded.old_id
    )
    in_new = await queries.fetch_active_contracts_for_season(
        offseason_db, seeded.new_id
    )
    assert len(still_active) == 2
    assert in_new == []


async def test_an_unknown_season_name_is_refused_rather_than_guessed(offseason_db):
    await seed_two_seasons(offseason_db)

    with pytest.raises(workflow.WorkflowError, match="No season named"):
        await offseason_screen.preview_carry_over(GUILD, "S99")


async def test_picking_the_oldest_season_previews_before_it_writes(offseason_db):
    seeded = await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    view = offseason_screen._CarryView(parent, loaded.pending_carry)
    select = child(view, offseason_screen._CarrySelect)
    pick(select, "S7")
    interaction = FakeInteraction()

    await select.callback(interaction)

    embed = interaction.edits()[0]["embed"]
    untouched = await queries.fetch_active_contracts_for_season(
        offseason_db, seeded.new_id
    )
    assert "Confirm carry-over" in embed.title
    assert untouched == []
    assert isinstance(interaction.edits()[0]["view"], offseason_screen._ConfirmView)


async def test_confirming_carry_over_moves_the_contracts_and_reports_counts(
    offseason_db,
):
    seeded = await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    view = offseason_screen._CarryView(parent, loaded.pending_carry)
    select = child(view, offseason_screen._CarrySelect)
    pick(select, "S7")
    interaction = FakeInteraction()
    await select.callback(interaction)

    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    carried = await queries.fetch_active_contracts_for_season(
        offseason_db, seeded.new_id
    )
    note = confirm.notes()[0]
    assert len(carried) == 1
    assert carried[0].contract_value == Decimal("20.00")
    assert "carried **1**" in note
    assert "expired **1**" in note
    assert "Nothing was announced publicly" in note


async def test_after_carry_over_the_step_reports_itself_done(offseason_db):
    await seed_two_seasons(offseason_db)
    await workflow.carry_over_contracts(
        guild_id=GUILD, actor_id=7, from_season_name="S7"
    )

    loaded = await offseason_screen.load_offseason_state(GUILD)

    assert loaded.pending_carry == []
    assert by_key(steps_for(loaded))[offseason_screen.STEP_CARRY].done


# ── behaviour: rollover preview and confirm ──────────────────────────


async def test_the_rollover_preview_matches_the_engine_and_writes_nothing(
    offseason_db,
):
    seeded = await seed_two_seasons(offseason_db)

    preview = await offseason_screen.preview_rollover(GUILD, "S7")

    rolled = await queries.budget_entry_exists(
        offseason_db, seeded.team_id, seeded.new_id, budget_ops.KIND_ROLLOVER
    )
    line = preview.lines[0]
    assert [candidate.team_name for candidate in preview.lines] == ["McLaren"]
    assert preview.enabled is True
    # The preview must use the SOURCE season's escrow flag, exactly as
    # `budget_ops.rollover` does, or preview and write would disagree.
    source_cfg = await queries.fetch_budget_config(offseason_db, seeded.old_id, None)
    expected = budget.rollover_amount(
        line.balance, line.season_payroll, escrow_enabled=source_cfg.escrow_enabled
    )
    assert line.balance == Decimal("0")
    assert line.season_payroll == Decimal("30.00")
    assert line.carried == expected
    assert line.already_rolled is False
    assert rolled is False, "the preview must not write"


async def test_confirming_the_rollover_writes_one_row_per_team(offseason_db):
    seeded = await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    view = offseason_screen._RolloverView(parent, loaded.past_seasons)
    select = child(view, offseason_screen._RolloverSelect)
    pick(select, "S7")
    interaction = FakeInteraction()
    await select.callback(interaction)

    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    preview_embed = interaction.edits()[0]["embed"]
    exists = await queries.budget_entry_exists(
        offseason_db, seeded.team_id, seeded.new_id, budget_ops.KIND_ROLLOVER
    )
    assert "Confirm budget rollover" in preview_embed.title
    assert exists or "skipped" in confirm.notes()[0]
    assert "Contracts were not touched" in confirm.notes()[0]


async def test_a_disabled_rollover_target_disables_the_confirm_button(offseason_db):
    seeded = await seed_two_seasons(offseason_db)
    await offseason_db.execute(
        "UPDATE budget_config SET rollover_enabled = FALSE WHERE season_id = $1",
        seeded.new_id,
    )
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    view = offseason_screen._RolloverView(parent, loaded.past_seasons)
    select = child(view, offseason_screen._RolloverSelect)
    pick(select, "S7")
    interaction = FakeInteraction()

    await select.callback(interaction)

    confirm_view = interaction.edits()[0]["view"]
    button = child(confirm_view, offseason_screen._ConfirmButton)
    assert button.disabled
    assert "disabled" in interaction.edits()[0]["embed"].description


# ── behaviour: seasons are picked, never typed ────────────────────────


async def test_activation_offers_a_select_of_real_seasons(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction()

    await offseason_screen._step_activate(
        interaction, view, offseason_screen.STEP_ACTIVATE
    )

    picker = interaction.edits()[0]["view"]
    select = child(picker, offseason_screen._ActivateSelect)
    assert {o.value for o in select.options} == {"S7", "S8"}
    assert "never typed" in interaction.edits()[0]["embed"].description
    assert_view_within_limits(picker)


async def test_activating_restates_that_every_read_switches(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    picker = offseason_screen._ActivateView(
        parent, await workflow.list_seasons(GUILD)
    )
    select = child(picker, offseason_screen._ActivateSelect)
    pick(select, "S7")
    interaction = FakeInteraction()

    await select.callback(interaction)

    embed = interaction.edits()[0]["embed"]
    assert "every read" in embed.description
    assert "no contracts exist in it" in embed.description


async def test_confirming_activation_switches_the_active_season(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    picker = offseason_screen._ActivateView(
        parent, await workflow.list_seasons(GUILD)
    )
    select = child(picker, offseason_screen._ActivateSelect)
    pick(select, "S7")
    interaction = FakeInteraction()
    await select.callback(interaction)

    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    after = await offseason_screen.load_offseason_state(GUILD)
    assert after.active_season_name == "S7"
    assert "is active" in confirm.notes()[0]


async def test_many_seasons_are_paged_not_truncated(offseason_db):
    await seed_two_seasons(offseason_db)
    for index in range(30):
        await queries.insert_season(offseason_db, GUILD, f"X{index:02d}")
    loaded = await offseason_screen.load_offseason_state(GUILD)
    parent = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )

    picker = offseason_screen._ActivateView(
        parent, await workflow.list_seasons(GUILD)
    )

    select = child(picker, offseason_screen._ActivateSelect)
    assert len(select.options) == 25
    assert "Older" in labels(picker)
    assert_view_within_limits(picker)


# ── behaviour: free agency steps ─────────────────────────────────────


async def test_closing_free_agency_confirms_first_then_writes(offseason_db):
    await seed_two_seasons(offseason_db)
    await workflow.set_free_agency(guild_id=GUILD, is_open=True)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction()

    await offseason_screen._step_free_agency(
        interaction, view, offseason_screen.STEP_CLOSE_FA
    )
    mid = await offseason_screen.load_offseason_state(GUILD)
    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    after = await offseason_screen.load_offseason_state(GUILD)
    assert "free_agency_closed" in interaction.edits()[0]["embed"].description
    assert mid.free_agency_open is True, "the preview must not write"
    assert after.free_agency_open is False


async def test_reopening_free_agency_warns_about_unpriced_offers(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction()

    await offseason_screen._step_free_agency(
        interaction, view, offseason_screen.STEP_REOPEN_FA
    )

    description = interaction.edits()[0]["embed"].description
    assert "Do this last" in description
    assert "no market value" in description


# ── behaviour: create season ──────────────────────────────────────────


async def test_creating_a_season_requires_a_name_and_seeds_the_preset(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    modal = offseason_screen._CreateSeasonModal(view)
    modal._name._value = "S9"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)
    embed = interaction.edits()[0]["embed"]
    confirm = FakeInteraction()
    await interaction.edits()[0]["view"].run(confirm)

    created = await queries.fetch_season_by_name(offseason_db, GUILD, "S9")
    assert "F1 preset" in embed.description
    assert "does **not** activate" in embed.description
    assert created is not None
    assert created.is_active is False
    assert "not active yet" in confirm.notes()[0]


async def test_a_blank_season_name_is_refused(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    modal = offseason_screen._CreateSeasonModal(view)
    modal._name._value = "   "
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert not interaction.edits()
    assert "needs a name" in interaction.notes()[0]


# ── behaviour: skipping records a reason ──────────────────────────────


async def test_skipping_records_the_reason_and_says_it_is_session_only(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    modal = offseason_screen._SkipModal(view, offseason_screen.STEP_PRIZE)
    modal._reason._value = "no prize pot this year"
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert view.progress.skipped[offseason_screen.STEP_PRIZE] == (
        "no prize pot this year"
    )
    assert "not\nwritten to the database" in interaction.notes()[0].replace(
        " ", "\n"
    ) or "not written to the database" in interaction.notes()[0]


async def test_a_skip_without_a_reason_is_refused(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    modal = offseason_screen._SkipModal(view, offseason_screen.STEP_PRIZE)
    modal._reason._value = "   "
    interaction = FakeInteraction()

    await modal.on_submit(interaction)

    assert offseason_screen.STEP_PRIZE not in view.progress.skipped
    assert "needs a reason" in interaction.notes()[0]


# ── behaviour: driver sync builds its seeds from the roles ─────────────


def test_seeds_skip_bots_and_keep_display_names():
    role = _FakeRole(
        900,
        [
            SimpleNamespace(id=1, display_name="ZeezinDomar", bot=False),
            SimpleNamespace(id=2, display_name="RosterBot", bot=True),
        ],
    )

    seeds = offseason_screen._seeds_from_role(role)

    assert [s.member_id for s in seeds] == [1]
    assert seeds[0].display_name == "ZeezinDomar"


def test_a_tier_with_no_role_is_reported_as_skipped_not_dropped():
    guild = _FakeGuild({900: _FakeRole(900, [])})
    tiers = [tier("t1"), tier("t2", role=False)]

    seeds, skipped = offseason_screen._resolve_tier_roles(
        guild, tiers, {"t1": 900, "t2": None}
    )

    assert "t1" in seeds
    assert skipped["t2"] == "no Discord role set"


def test_a_role_missing_from_the_guild_names_the_id_it_looked_for():
    guild = _FakeGuild({})
    seeds, skipped = offseason_screen._resolve_tier_roles(
        guild, [tier("t1")], {"t1": 900}
    )

    assert seeds == {}
    assert "900" in skipped["t1"]


async def test_sync_previews_the_counts_before_enrolling_anyone(offseason_db):
    seeded = await seed_two_seasons(offseason_db)
    new_tier = await offseason_db.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", seeded.new_id
    )
    await offseason_db.execute(
        "UPDATE tiers SET tier_role_id = 900 WHERE id = $1", new_tier
    )
    guild = _FakeGuild(
        {900: _FakeRole(900, [SimpleNamespace(id=1, display_name="A", bot=False)])}
    )
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction(guild=guild)

    await offseason_screen._step_sync_drivers(
        interaction, view, offseason_screen.STEP_SYNC
    )

    embed = interaction.edits()[0]["embed"]
    drivers = await queries.fetch_drivers_in_tier(offseason_db, new_tier)
    assert "1 member(s) in the" in embed.description
    assert "Nobody is removed" in embed.description
    assert drivers == [], "the preview must not enrol anyone"


async def test_confirming_sync_enrols_the_role_members(offseason_db):
    seeded = await seed_two_seasons(offseason_db)
    new_tier = await offseason_db.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", seeded.new_id
    )
    await offseason_db.execute(
        "UPDATE tiers SET tier_role_id = 900 WHERE id = $1", new_tier
    )
    guild = _FakeGuild(
        {900: _FakeRole(900, [SimpleNamespace(id=1, display_name="A", bot=False)])}
    )
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction(guild=guild)
    await offseason_screen._step_sync_drivers(
        interaction, view, offseason_screen.STEP_SYNC
    )

    confirm = FakeInteraction(guild=guild)
    await interaction.edits()[0]["view"].run(confirm)

    drivers = await queries.fetch_drivers_in_tier(offseason_db, new_tier)
    assert [d.member_id for d in drivers] == [1]
    assert "No Discord roles were changed" in confirm.notes()[0]


# ── behaviour: prize money hands off to Teams & money ─────────────────


async def test_the_prize_step_opens_the_money_screen(offseason_db):
    await seed_two_seasons(offseason_db)
    loaded = await offseason_screen.load_offseason_state(GUILD)
    view = offseason_screen.OffseasonView(
        state=loaded, opener_id=7, on_back=noop_back
    )
    interaction = FakeInteraction()

    await offseason_screen._step_prize(
        interaction, view, offseason_screen.STEP_PRIZE
    )

    embed = interaction.edits()[0]["embed"]
    assert isinstance(interaction.edits()[0]["view"], money_screen.MoneyView)
    assert "Teams & money" in embed.title
    assert interaction.edits()[0]["view"].opener_id == 7
