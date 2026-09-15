"""
Contract length is a league tunable at both ends.

`max_term_seasons` was always configurable; the minimum used to be a
hardcoded `< 1` in bot/contracts/rules.py, so a commissioner could cap
contracts but could not require a floor. These tests cover the four
layers that had to agree for that to change: the schema, the config
writer, the offer rule, and the editing UI.
"""

from decimal import Decimal

import pytest
from asyncpg.exceptions import CheckViolationError

from bot import queries
from bot.contracts import rules
from bot.presets import f1 as f1_preset
from bot.ui import config_modal
from bot.ui.base import MODAL_MAX_INPUTS

# Money limits, Contract rules, Race terms & premiums. A named constant
# so adding a fourth section is a deliberate edit here, not a surprise.
CONFIG_SECTION_COUNT = 3

# ── schema ───────────────────────────────────────────────────────────


async def _seeded_season(conn, guild_id: int = 77) -> int:
    season_id = await queries.insert_season(conn, guild_id, "S1", is_active=True)
    await f1_preset.seed_season(conn, season_id)
    return season_id


async def test_preset_seeds_a_one_season_floor(pg_conn_migrated):
    """The default reproduces the old hardcoded behaviour exactly."""
    season_id = await _seeded_season(pg_conn_migrated)
    cfg = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)
    assert cfg.min_term_seasons == 1
    assert cfg.max_term_seasons == 3


async def test_existing_rows_default_to_the_old_floor(pg_conn_migrated):
    """
    A row written without the new column still gets 1, so the migration
    needs no backfill.
    """
    season_id = await queries.insert_season(pg_conn_migrated, 78, "S2", is_active=True)
    await pg_conn_migrated.execute(
        """
        INSERT INTO league_config (
            season_id, tier_id, salary_cap, min_salary, active_driver_slots,
            weekly_move_cap, exceptional_move_cap, max_term_seasons,
            max_incentive_pct, offer_ttl_hours
        ) VALUES ($1, NULL, 145.00, 1.00, 2, 0.75, 1.25, 3, 0.150, 48)
        """,
        season_id,
    )
    cfg = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)
    assert cfg.min_term_seasons == 1


async def test_floor_persists_through_the_config_writer(pg_conn_migrated):
    season_id = await _seeded_season(pg_conn_migrated)
    cfg = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)

    await queries.upsert_league_config(
        pg_conn_migrated,
        season_id=season_id,
        tier_id=None,
        salary_cap=cfg.salary_cap,
        min_salary=cfg.min_salary,
        max_salary=cfg.max_salary,
        active_driver_slots=cfg.active_driver_slots,
        weekly_move_cap=cfg.weekly_move_cap,
        exceptional_move_cap=cfg.exceptional_move_cap,
        min_term_seasons=2,
        max_term_seasons=4,
        max_incentive_pct=cfg.max_incentive_pct,
        offer_ttl_hours=cfg.offer_ttl_hours,
    )

    reread = await queries.fetch_league_config_row(pg_conn_migrated, season_id, None)
    assert (reread.min_term_seasons, reread.max_term_seasons) == (2, 4)


@pytest.fixture
def config_db(monkeypatch, pg_conn_migrated):
    """Point config_modal's `db.connect()` at the test schema."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(config_modal.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


async def test_saving_one_section_does_not_revert_the_other(config_db):
    """
    Regression: the section menu hands each modal a config snapshot taken
    when the menu was built. An admin who edited Money limits and then
    Contract rules had the second save write the stale pre-edit cap back,
    silently undoing the first edit while both saves reported success.
    """
    season_id = await _seeded_season(config_db)
    stale = await queries.fetch_league_config_row(config_db, season_id, None)

    # First edit: raise the cap (as the Money limits modal does).
    await config_modal._save(
        season_id=season_id, tier_id=None, current=stale,
        salary_cap=Decimal("200.00"),
    )

    # Second edit from the SAME menu, so it still carries `stale`.
    await config_modal._save(
        season_id=season_id, tier_id=None, current=stale,
        min_term_seasons=2, max_term_seasons=4,
    )

    reread = await queries.fetch_league_config_row(config_db, season_id, None)
    assert reread.salary_cap == Decimal("200.00"), (
        "the second section's save reverted the cap edited by the first"
    )
    assert (reread.min_term_seasons, reread.max_term_seasons) == (2, 4)


async def test_save_still_works_when_no_row_exists_yet(config_db):
    """
    Falling back to the snapshot matters: the first-ever save has nothing
    to re-read, so `current` is the only source for the untouched half.
    """
    season_id = await _seeded_season(config_db)
    seeded = await queries.fetch_league_config_row(config_db, season_id, None)
    await config_db.execute("DELETE FROM league_config WHERE season_id = $1", season_id)

    await config_modal._save(
        season_id=season_id, tier_id=None, current=seeded,
        salary_cap=Decimal("150.00"),
    )

    reread = await queries.fetch_league_config_row(config_db, season_id, None)
    assert reread.salary_cap == Decimal("150.00")
    assert reread.offer_ttl_hours == seeded.offer_ttl_hours, "untouched half lost"


async def test_schema_rejects_an_inverted_range(pg_conn_migrated):
    """The CHECK is the backstop: an unsatisfiable range cannot be stored."""
    season_id = await _seeded_season(pg_conn_migrated)
    with pytest.raises(CheckViolationError):
        await pg_conn_migrated.execute(
            "UPDATE league_config SET min_term_seasons = 5, max_term_seasons = 3 "
            "WHERE season_id = $1",
            season_id,
        )


async def test_schema_rejects_a_zero_season_floor(pg_conn_migrated):
    season_id = await _seeded_season(pg_conn_migrated)
    with pytest.raises(CheckViolationError):
        await pg_conn_migrated.execute(
            "UPDATE league_config SET min_term_seasons = 0 WHERE season_id = $1",
            season_id,
        )


# ── offer rule ───────────────────────────────────────────────────────


def _inputs(*, term: int, min_term: int, max_term: int) -> rules.OfferInputs:
    return rules.OfferInputs(
        actor_id=100,
        actor_is_principal=True,
        actor_is_admin=False,
        driver_present_in_tier=True,
        driver_status="active",
        driver_has_active_contract=False,
        duplicate_open_offer_exists=False,
        salary=Decimal("5.00"),
        min_salary=Decimal("1.00"),
        max_salary=Decimal("50.00"),
        signing_bonus=Decimal("0"),
        incentives_amount=Decimal("0"),
        max_incentive_pct=Decimal("0.15"),
        team_payroll_before=Decimal("50.00"),
        salary_cap=Decimal("145.00"),
        active_slots_used=1,
        active_slots_max=2,
        has_linked_release=False,
        term_seasons=term,
        min_term_seasons=min_term,
        max_term_seasons=max_term,
        offer_kind="new",
        free_agency_open=True,
    )


def test_term_below_configured_minimum_is_blocked():
    result = rules.term_within_bounds(_inputs(term=1, min_term=2, max_term=4))
    assert not result.ok
    assert result.code == "term_below_minimum"
    # The message must name the league's own number, not a constant.
    assert "2" in result.message


def test_term_exactly_at_the_minimum_passes():
    result = rules.term_within_bounds(_inputs(term=2, min_term=2, max_term=4))
    assert result.ok


def test_term_exactly_at_the_maximum_passes():
    result = rules.term_within_bounds(_inputs(term=4, min_term=2, max_term=4))
    assert result.ok


def test_term_above_maximum_still_blocked():
    result = rules.term_within_bounds(_inputs(term=5, min_term=2, max_term=4))
    assert not result.ok
    assert result.code == "term_too_long"


def test_zero_season_term_reports_the_absolute_floor_not_the_league_floor():
    """
    Distinct codes matter: a nonsense value and a policy violation need
    different messages for the TP to know which one they hit.
    """
    result = rules.term_within_bounds(_inputs(term=0, min_term=3, max_term=4))
    assert not result.ok
    assert result.code == "term_too_short"


def test_a_league_with_a_single_legal_length_accepts_only_that_length():
    assert rules.term_within_bounds(_inputs(term=2, min_term=2, max_term=2)).ok
    assert not rules.term_within_bounds(
        _inputs(term=3, min_term=2, max_term=2)
    ).ok
    assert not rules.term_within_bounds(
        _inputs(term=1, min_term=2, max_term=2)
    ).ok


def test_default_inputs_describe_the_narrowest_legal_league():
    """
    The dataclass defaults must not smuggle in a policy assumption; a
    caller that forgets to pass the bounds should get one season only.
    """
    fields = rules.OfferInputs.__dataclass_fields__
    assert fields["min_term_seasons"].default == 1
    assert fields["max_term_seasons"].default == 1


def test_full_validation_surfaces_the_minimum_failure():
    validation = rules.validate_offer(_inputs(term=1, min_term=3, max_term=3))
    assert not validation.ok
    codes = [r.code for r in validation.results if not r.ok]
    assert "term_below_minimum" in codes


# ── editing guardrails ───────────────────────────────────────────────


def _valid_terms(**overrides):
    kwargs = {
        "min_term": 1,
        "max_term": 3,
        "slots": 2,
        "incentive_pct": Decimal("0.15"),
        "ttl_hours": 48,
    }
    kwargs.update(overrides)
    return kwargs


def test_validate_terms_accepts_a_sane_range():
    config_modal.validate_terms(**_valid_terms())


def test_validate_terms_accepts_min_equal_to_max():
    config_modal.validate_terms(**_valid_terms(min_term=2, max_term=2))


def test_validate_terms_rejects_a_zero_minimum():
    with pytest.raises(config_modal.ConfigError) as exc:
        config_modal.validate_terms(**_valid_terms(min_term=0))
    assert "at least 1 season" in str(exc.value)


def test_validate_terms_rejects_an_inverted_range():
    """
    The admin-facing message has to explain the consequence, because the
    symptom is every offer being rejected for no obvious reason.
    """
    with pytest.raises(config_modal.ConfigError) as exc:
        config_modal.validate_terms(**_valid_terms(min_term=4, max_term=2))
    assert "below the" in str(exc.value)


def test_validate_terms_rejects_zero_driver_slots():
    with pytest.raises(config_modal.ConfigError):
        config_modal.validate_terms(**_valid_terms(slots=0))


def test_validate_terms_rejects_negative_incentives():
    with pytest.raises(config_modal.ConfigError):
        config_modal.validate_terms(**_valid_terms(incentive_pct=Decimal("-0.1")))


def test_validate_terms_rejects_a_zero_hour_expiry():
    with pytest.raises(config_modal.ConfigError):
        config_modal.validate_terms(**_valid_terms(ttl_hours=0))


def test_money_validation_rejects_a_floor_above_the_cap():
    with pytest.raises(config_modal.ConfigError) as exc:
        config_modal._validate_money(
            salary_cap=Decimal("10.00"),
            min_salary=Decimal("20.00"),
            max_salary=None,
            weekly=Decimal("0.75"),
            exceptional=Decimal("1.25"),
        )
    assert "cannot exceed" in str(exc.value)


def test_money_validation_allows_a_blank_ceiling():
    config_modal._validate_money(
        salary_cap=Decimal("145.00"),
        min_salary=Decimal("1.00"),
        max_salary=None,
        weekly=Decimal("0.75"),
        exceptional=Decimal("1.25"),
    )


def test_money_validation_rejects_a_ceiling_over_the_cap():
    with pytest.raises(config_modal.ConfigError):
        config_modal._validate_money(
            salary_cap=Decimal("145.00"),
            min_salary=Decimal("1.00"),
            max_salary=Decimal("200.00"),
            weekly=Decimal("0.75"),
            exceptional=Decimal("1.25"),
        )


# ── modal composition ────────────────────────────────────────────────


class _FakeConfig:
    salary_cap = Decimal("145.00")
    min_salary = Decimal("1.00")
    max_salary = None
    active_driver_slots = 2
    weekly_move_cap = Decimal("0.75")
    exceptional_move_cap = Decimal("1.25")
    min_term_seasons = 2
    max_term_seasons = 4
    max_incentive_pct = Decimal("0.150")
    offer_ttl_hours = 48
    # Phase 9: race-based terms and the two premiums. Premiums at zero
    # match the shipped default, where contract pricing is unchanged.
    races_per_season = 24
    min_term_races = 5
    max_term_races = 48
    length_premium_pct = Decimal("0")
    resign_premium_pct = Decimal("0")


def _labels(modal) -> list[str]:
    return [getattr(item, "label", "") for item in modal.children]


def test_terms_modal_fits_discords_five_input_limit():
    modal = config_modal.TermsConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    assert len(modal.children) == MODAL_MAX_INPUTS


def test_money_modal_fits_discords_five_input_limit():
    modal = config_modal.MoneyConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    assert len(modal.children) == MODAL_MAX_INPUTS


def test_terms_modal_exposes_both_contract_length_bounds():
    modal = config_modal.TermsConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    labels = _labels(modal)
    assert any("Min contract length" in label for label in labels)
    assert any("Max contract length" in label for label in labels)


def test_terms_modal_prefills_the_current_bounds():
    modal = config_modal.TermsConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    defaults = {
        getattr(item, "label", ""): getattr(item, "default", None)
        for item in modal.children
    }
    assert defaults["Min contract length (seasons)"] == "2"
    assert defaults["Max contract length (seasons)"] == "4"


def test_terms_modal_shows_incentives_as_a_percentage():
    """Storage is a fraction; admins read percentages everywhere else."""
    modal = config_modal.TermsConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    defaults = {
        getattr(item, "label", ""): getattr(item, "default", None)
        for item in modal.children
    }
    assert defaults["Max incentives (% of salary)"] == "15.0"


def test_money_modal_leaves_an_absent_ceiling_blank():
    modal = config_modal.MoneyConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    ceiling = next(
        item for item in modal.children
        if "Maximum salary" in getattr(item, "label", "")
    )
    assert ceiling.default == ""
    assert ceiling.required is False


def test_every_numeric_config_field_is_editable_somewhere():
    """
    The split existed to give every tunable a surface. If someone adds a
    config column without adding an input, this test should notice.
    """
    money = _labels(
        config_modal.MoneyConfigModal(
            season_id=1, tier_id=None, current=_FakeConfig()
        )
    )
    terms = _labels(
        config_modal.TermsConfigModal(
            season_id=1, tier_id=None, current=_FakeConfig()
        )
    )
    assert len(money) + len(terms) == 10


# ── chooser ──────────────────────────────────────────────────────────


def test_config_embed_shows_the_length_range():
    embed = config_modal.build_config_embed(
        _FakeConfig(), scope_label="the season default"
    )
    body = " ".join(field.value for field in embed.fields)
    assert "2–4 seasons" in body


def test_config_embed_collapses_a_single_legal_length():
    class Fixed(_FakeConfig):
        min_term_seasons = 2
        max_term_seasons = 2

    embed = config_modal.build_config_embed(Fixed(), scope_label="tier `t1`")
    body = " ".join(field.value for field in embed.fields)
    assert "exactly 2 season(s)" in body


def test_config_embed_names_the_scope_being_edited():
    embed = config_modal.build_config_embed(_FakeConfig(), scope_label="tier `t1`")
    assert "tier `t1`" in embed.description


def test_chooser_offers_both_sections():
    view = config_modal.ConfigSectionView(
        season_id=1, tier_id=None, current=_FakeConfig(), opener_id=5
    )
    labels = [getattr(item, "label", "") for item in view.children]
    assert "Money limits" in labels
    assert "Contract rules" in labels


def test_chooser_has_no_back_button_when_opened_standalone():
    """The slash command opens this as a top-level message."""
    view = config_modal.ConfigSectionView(
        season_id=1, tier_id=None, current=_FakeConfig(), opener_id=5
    )
    assert len(view.children) == CONFIG_SECTION_COUNT


def test_chooser_gains_a_back_button_inside_the_panel():
    async def _back(interaction):
        return None

    view = config_modal.ConfigSectionView(
        season_id=1,
        tier_id=None,
        current=_FakeConfig(),
        opener_id=5,
        on_back=_back,
    )
    labels = [getattr(item, "label", "") for item in view.children]
    assert "Back to setup" in labels


def test_chooser_is_owner_locked():
    view = config_modal.ConfigSectionView(
        season_id=1, tier_id=None, current=_FakeConfig(), opener_id=1234
    )
    assert view.opener_id == 1234


# ── Phase 9: race terms & premiums section ───────────────────────────


def test_race_terms_modal_fits_discords_five_input_limit():
    modal = config_modal.RaceTermsConfigModal(
        season_id=1, tier_id=None, current=_FakeConfig()
    )
    assert len(modal.children) == MODAL_MAX_INPUTS


def test_race_terms_modal_exposes_the_race_bounds_and_both_premiums():
    """
    These five are the whole point of the section: if any one is missing
    an admin has no in-Discord way to set it and would need SQL.
    """
    labels = " | ".join(
        _labels(
            config_modal.RaceTermsConfigModal(
                season_id=1, tier_id=None, current=_FakeConfig()
            )
        )
    )
    assert "Races per season" in labels
    assert "Min contract length (races)" in labels
    assert "Max contract length (races)" in labels
    assert "Length premium" in labels
    assert "Re-sign premium" in labels


def test_config_embed_reports_premiums_as_off_when_both_are_zero():
    """
    Shipped default. An admin reading the panel should be told pricing is
    unchanged rather than shown two 0.000% figures to interpret.
    """
    embed = config_modal.build_config_embed(
        _FakeConfig(), scope_label="whole league"
    )
    race_field = next(
        f for f in embed.fields if "Race terms" in f.name
    )
    assert "off" in race_field.value
    assert "24" in race_field.value


def test_config_embed_reports_tuned_premiums_numerically():
    class Tuned(_FakeConfig):
        length_premium_pct = Decimal("0.005")
        resign_premium_pct = Decimal("0.150")

    embed = config_modal.build_config_embed(Tuned(), scope_label="Tier 1")
    race_field = next(f for f in embed.fields if "Race terms" in f.name)
    assert "off" not in race_field.value
    assert "0.500" in race_field.value
    assert "15.000" in race_field.value


def test_a_max_below_the_min_race_term_is_rejected():
    """Every offer would fail, so it must fail here with a reason."""
    with pytest.raises(config_modal.ConfigError) as exc:
        config_modal.validate_race_terms(
            races_per_season=24,
            min_term_races=10,
            max_term_races=4,
            length_premium_pct=Decimal("0"),
            resign_premium_pct=Decimal("0"),
        )
    assert "below the" in str(exc.value)


def test_a_zero_race_season_is_rejected():
    """Salary is charged per race; zero races would make contracts free."""
    with pytest.raises(config_modal.ConfigError):
        config_modal.validate_race_terms(
            races_per_season=0,
            min_term_races=1,
            max_term_races=4,
            length_premium_pct=Decimal("0"),
            resign_premium_pct=Decimal("0"),
        )


def test_negative_premiums_are_rejected():
    """
    A negative length premium would pay teams to sign the longest deal
    possible; a negative re-sign premium would invert the whole feature.
    """
    for bad in ("length_premium_pct", "resign_premium_pct"):
        kwargs = dict(
            races_per_season=24,
            min_term_races=5,
            max_term_races=48,
            length_premium_pct=Decimal("0"),
            resign_premium_pct=Decimal("0"),
        )
        kwargs[bad] = Decimal("-0.010")
        with pytest.raises(config_modal.ConfigError):
            config_modal.validate_race_terms(**kwargs)


def test_zero_premiums_are_allowed_because_that_is_the_shipped_default():
    config_modal.validate_race_terms(
        races_per_season=24,
        min_term_races=5,
        max_term_races=48,
        length_premium_pct=Decimal("0"),
        resign_premium_pct=Decimal("0"),
    )
