"""
The league-config editors, shared by `/market-admin config edit` and the
control panel's Setup screen.

Discord caps a modal at five text inputs, and league_config has ten
numeric tunables, so the editor is split into two modals behind a
chooser: money limits, and contract rules. The split is what made both
contract-length bounds editable — the old single modal was already full,
and `min_term_seasons`, `max_salary`, `max_incentive_pct`,
`active_driver_slots` and `offer_ttl_hours` had no editable surface at
all as a result.

These live outside both cogs so there is exactly one definition. A second
copy would drift the moment someone added a config field to one of them.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import discord

from bot import db, queries
from bot.ui import base
from bot.ui.base import COLOR_INFO, BackButton, BackCallback, OwnedView, report_error

# `max_incentive_pct` is stored as a fraction (0.150) but shown to admins
# as a percentage (15.0%) everywhere else, including `/market-admin
# config show`. The modal matches what they already read rather than
# exposing the storage format.
_PCT_SCALE = Decimal("100")

# Blank means "no maximum", which is a real and distinct setting from
# zero, so an empty field has to be preserved as NULL.
_BLANK_MEANS_NONE = ""


class ConfigError(ValueError):
    """Raised with text meant to be shown directly to the admin."""


def _parse_decimal(raw: str, *, field: str) -> Decimal:
    try:
        return Decimal(raw.strip())
    except (InvalidOperation, ValueError) as exc:
        raise ConfigError(f"**{field}** is not a number: `{raw.strip()}`") from exc


def _parse_optional_decimal(raw: str, *, field: str) -> Decimal | None:
    if raw.strip() == _BLANK_MEANS_NONE:
        return None
    return _parse_decimal(raw, field=field)


def _parse_int(raw: str, *, field: str) -> int:
    try:
        return int(raw.strip())
    except ValueError as exc:
        raise ConfigError(
            f"**{field}** must be a whole number: `{raw.strip()}`"
        ) from exc


def _carried_fields(config) -> dict:
    """The full column set, so an upsert can rewrite the untouched half."""
    return {
        "salary_cap": config.salary_cap,
        "min_salary": config.min_salary,
        "max_salary": config.max_salary,
        "active_driver_slots": config.active_driver_slots,
        "weekly_move_cap": config.weekly_move_cap,
        "exceptional_move_cap": config.exceptional_move_cap,
        "min_term_seasons": config.min_term_seasons,
        "max_term_seasons": config.max_term_seasons,
        "max_incentive_pct": config.max_incentive_pct,
        "offer_ttl_hours": config.offer_ttl_hours,
        # Phase 9 columns. Carried like the rest so editing Money limits
        # cannot silently reset a tuned premium.
        "races_per_season": config.races_per_season,
        "min_term_races": config.min_term_races,
        "max_term_races": config.max_term_races,
        "length_premium_pct": config.length_premium_pct,
        "resign_premium_pct": config.resign_premium_pct,
    }


async def _save(*, season_id: int, tier_id: int | None, current, **overrides) -> None:
    """
    Write the row, carrying over whichever half this modal did not edit.

    `upsert_league_config` rewrites every column, so the untouched values
    must be passed back explicitly -- and they are re-read here rather
    than taken from `current`. `current` is a snapshot from when the
    section menu was built, so one admin editing Money limits and then
    Contract rules in the same sitting would otherwise have the second
    save silently revert the first: the cap would go back to its
    pre-edit value while both saves reported success.

    Re-reading narrows last-writer-wins to the fields a modal actually
    submitted, which is also what two admins editing different sections
    at the same time should get.
    """
    async with db.connect() as conn:
        fresh = await queries.fetch_league_config_row(
            conn, season_id=season_id, tier_id=tier_id
        )
        fields = _carried_fields(fresh if fresh is not None else current)
        fields.update(overrides)

        await queries.upsert_league_config(
            conn, season_id=season_id, tier_id=tier_id, **fields
        )


# ── money ────────────────────────────────────────────────────────────


class MoneyConfigModal(base.PanelModal, title="Edit money limits"):
    """Salary cap, salary floor and ceiling, and the weekly movement caps."""

    def __init__(self, *, season_id: int, tier_id: int | None, current) -> None:
        super().__init__()
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current

        self._salary_cap = discord.ui.TextInput(
            label="Salary cap ($M)", default=str(current.salary_cap)
        )
        self._min_salary = discord.ui.TextInput(
            label="Minimum salary ($M)", default=str(current.min_salary)
        )
        self._max_salary = discord.ui.TextInput(
            label="Maximum salary ($M) — blank for none",
            default=(
                _BLANK_MEANS_NONE
                if current.max_salary is None
                else str(current.max_salary)
            ),
            required=False,
        )
        self._weekly_cap = discord.ui.TextInput(
            label="Weekly move cap ($M)", default=str(current.weekly_move_cap)
        )
        self._exceptional_cap = discord.ui.TextInput(
            label="Exceptional move cap ($M)",
            default=str(current.exceptional_move_cap),
        )
        for widget in (
            self._salary_cap,
            self._min_salary,
            self._max_salary,
            self._weekly_cap,
            self._exceptional_cap,
        ):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            salary_cap = _parse_decimal(self._salary_cap.value, field="Salary cap")
            min_salary = _parse_decimal(self._min_salary.value, field="Minimum salary")
            max_salary = _parse_optional_decimal(
                self._max_salary.value, field="Maximum salary"
            )
            weekly = _parse_decimal(self._weekly_cap.value, field="Weekly move cap")
            exceptional = _parse_decimal(
                self._exceptional_cap.value, field="Exceptional move cap"
            )
            _validate_money(
                salary_cap=salary_cap,
                min_salary=min_salary,
                max_salary=max_salary,
                weekly=weekly,
                exceptional=exceptional,
            )
        except ConfigError as exc:
            await report_error(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        await _save(
            season_id=self._season_id,
            tier_id=self._tier_id,
            current=self._current,
            salary_cap=salary_cap,
            min_salary=min_salary,
            max_salary=max_salary,
            weekly_move_cap=weekly,
            exceptional_move_cap=exceptional,
        )
        ceiling = "none" if max_salary is None else f"${max_salary}M"
        await interaction.followup.send(
            f"✅ Money limits saved.\nCap **${salary_cap}M** · floor "
            f"**${min_salary}M** · ceiling **{ceiling}**.",
            ephemeral=True,
        )


def _validate_money(
    *,
    salary_cap: Decimal,
    min_salary: Decimal,
    max_salary: Decimal | None,
    weekly: Decimal,
    exceptional: Decimal,
) -> None:
    """
    Reject ranges no offer could ever satisfy.

    Caught here rather than at offer time because a TP hitting an
    impossible limit gets a rejection that looks like their mistake.
    """
    if salary_cap <= 0:
        raise ConfigError("**Salary cap** must be greater than zero.")
    if min_salary < 0:
        raise ConfigError("**Minimum salary** cannot be negative.")
    if min_salary > salary_cap:
        raise ConfigError(
            f"**Minimum salary** (${min_salary}M) cannot exceed the "
            f"**salary cap** (${salary_cap}M) — no signing would be legal."
        )
    if max_salary is not None:
        if max_salary < min_salary:
            raise ConfigError(
                f"**Maximum salary** (${max_salary}M) is below the "
                f"**minimum salary** (${min_salary}M)."
            )
        if max_salary > salary_cap:
            raise ConfigError(
                f"**Maximum salary** (${max_salary}M) exceeds the "
                f"**salary cap** (${salary_cap}M)."
            )
    if weekly < 0 or exceptional < 0:
        raise ConfigError("Movement caps cannot be negative.")


# ── contract rules ───────────────────────────────────────────────────


class TermsConfigModal(base.PanelModal, title="Edit contract rules"):
    """
    Contract length bounds, roster size, incentive ceiling and offer TTL.

    The two term fields are first because they are the pair admins came
    for: together they define the contract lengths a Team Principal is
    allowed to offer.
    """

    def __init__(self, *, season_id: int, tier_id: int | None, current) -> None:
        super().__init__()
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current

        self._min_term = discord.ui.TextInput(
            label="Min contract length (seasons)",
            default=str(current.min_term_seasons),
            max_length=3,
        )
        self._max_term = discord.ui.TextInput(
            label="Max contract length (seasons)",
            default=str(current.max_term_seasons),
            max_length=3,
        )
        self._slots = discord.ui.TextInput(
            label="Active driver slots per team",
            default=str(current.active_driver_slots),
            max_length=3,
        )
        self._incentive_pct = discord.ui.TextInput(
            label="Max incentives (% of salary)",
            default=f"{current.max_incentive_pct * _PCT_SCALE:.1f}",
            max_length=6,
        )
        self._ttl = discord.ui.TextInput(
            label="Offer expiry (hours)",
            default=str(current.offer_ttl_hours),
            max_length=5,
        )
        for widget in (
            self._min_term,
            self._max_term,
            self._slots,
            self._incentive_pct,
            self._ttl,
        ):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            min_term = _parse_int(self._min_term.value, field="Min contract length")
            max_term = _parse_int(self._max_term.value, field="Max contract length")
            slots = _parse_int(self._slots.value, field="Active driver slots")
            pct = _parse_decimal(
                self._incentive_pct.value, field="Max incentives"
            ) / _PCT_SCALE
            ttl = _parse_int(self._ttl.value, field="Offer expiry")
            validate_terms(
                min_term=min_term,
                max_term=max_term,
                slots=slots,
                incentive_pct=pct,
                ttl_hours=ttl,
            )
        except ConfigError as exc:
            await report_error(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        await _save(
            season_id=self._season_id,
            tier_id=self._tier_id,
            current=self._current,
            min_term_seasons=min_term,
            max_term_seasons=max_term,
            active_driver_slots=slots,
            max_incentive_pct=pct,
            offer_ttl_hours=ttl,
        )

        span = (
            f"exactly **{min_term}** season(s)"
            if min_term == max_term
            else f"**{min_term}–{max_term}** seasons"
        )
        await interaction.followup.send(
            f"✅ Contract rules saved.\nTeam Principals may now sign drivers "
            f"for {span}.",
            ephemeral=True,
        )


def validate_terms(
    *,
    min_term: int,
    max_term: int,
    slots: int,
    incentive_pct: Decimal,
    ttl_hours: int,
) -> None:
    """
    Reject unsatisfiable contract rules.

    Exposed (not underscore-prefixed) because it is the single definition
    of what a legal term range is, and the schema's CHECK constraints
    mirror it. Kept out of `bot/contracts/` so the offer rules stay free
    of numeric literals.
    """
    if min_term < 1:
        raise ConfigError(
            f"**Min contract length** must be at least 1 season (got {min_term}). "
            "A zero-season contract is not a shorter deal, it is no deal."
        )
    if max_term < min_term:
        raise ConfigError(
            f"**Max contract length** ({max_term}) is below the **minimum** "
            f"({min_term}). No contract length would be legal, so every offer "
            f"would be rejected."
        )
    if slots < 1:
        raise ConfigError(
            f"**Active driver slots** must be at least 1 (got {slots})."
        )
    if incentive_pct < 0:
        raise ConfigError("**Max incentives** cannot be negative.")
    if ttl_hours < 1:
        raise ConfigError(
            f"**Offer expiry** must be at least 1 hour (got {ttl_hours})."
        )


class RaceTermsConfigModal(
    base.PanelModal, title="Edit race terms & premiums"
):
    """
    Race-based contract length and the two re-signing premiums.

    A third section rather than more fields on Contract rules: Discord
    allows five inputs per modal and that one is already full.

    Contracts are measured in races, not seasons, so these are the
    bounds a Team Principal is actually held to. The premiums default to
    zero, which leaves contract pricing exactly as it was before this
    update — raise them only when the league wants longer deals and
    re-signings to cost more.
    """

    def __init__(self, *, season_id: int, tier_id: int | None, current) -> None:
        super().__init__()
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current

        self._races = discord.ui.TextInput(
            label="Races per season",
            default=str(current.races_per_season),
            max_length=4,
        )
        self._min_races = discord.ui.TextInput(
            label="Min contract length (races)",
            default=str(current.min_term_races),
            max_length=4,
        )
        self._max_races = discord.ui.TextInput(
            label="Max contract length (races)",
            default=str(current.max_term_races),
            max_length=4,
        )
        self._length_premium = discord.ui.TextInput(
            label="Length premium (% per extra race)",
            default=f"{current.length_premium_pct * _PCT_SCALE:.3f}",
            max_length=8,
        )
        self._resign_premium = discord.ui.TextInput(
            label="Re-sign premium (% on own driver)",
            default=f"{current.resign_premium_pct * _PCT_SCALE:.3f}",
            max_length=8,
        )
        for widget in (
            self._races,
            self._min_races,
            self._max_races,
            self._length_premium,
            self._resign_premium,
        ):
            self.add_item(widget)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            races = _parse_int(self._races.value, field="Races per season")
            min_races = _parse_int(
                self._min_races.value, field="Min contract length (races)"
            )
            max_races = _parse_int(
                self._max_races.value, field="Max contract length (races)"
            )
            length_pct = _parse_decimal(
                self._length_premium.value, field="Length premium"
            ) / _PCT_SCALE
            resign_pct = _parse_decimal(
                self._resign_premium.value, field="Re-sign premium"
            ) / _PCT_SCALE
            validate_race_terms(
                races_per_season=races,
                min_term_races=min_races,
                max_term_races=max_races,
                length_premium_pct=length_pct,
                resign_premium_pct=resign_pct,
            )
        except ConfigError as exc:
            await report_error(interaction, str(exc))
            return

        await interaction.response.defer(ephemeral=True)
        await _save(
            season_id=self._season_id,
            tier_id=self._tier_id,
            current=self._current,
            races_per_season=races,
            min_term_races=min_races,
            max_term_races=max_races,
            length_premium_pct=length_pct,
            resign_premium_pct=resign_pct,
        )

        span = (
            f"exactly **{min_races}** race(s)"
            if min_races == max_races
            else f"**{min_races}\u2013{max_races}** races"
        )
        if length_pct == 0 and resign_pct == 0:
            premium_note = (
                "Premiums are **off**, so contract prices are unchanged."
            )
        else:
            premium_note = (
                f"Length premium **{length_pct * _PCT_SCALE:.3f}%** per race "
                f"beyond the minimum; re-signing your own driver costs "
                f"**{resign_pct * _PCT_SCALE:.3f}%** more."
            )
        await interaction.followup.send(
            f"\u2705 Race terms saved.\nContracts may run for {span} "
            f"across a **{races}**-race season.\n{premium_note}",
            ephemeral=True,
        )


def validate_race_terms(
    *,
    races_per_season: int,
    min_term_races: int,
    max_term_races: int,
    length_premium_pct: Decimal,
    resign_premium_pct: Decimal,
) -> None:
    """
    Reject race-term settings no offer could satisfy.

    Mirrors `validate_terms`: the point is to fail here, with a sentence
    explaining why, rather than at offer time where a Team Principal
    would see an unexplained rejection on every contract they tried.
    """
    if races_per_season < 1:
        raise ConfigError(
            f"**Races per season** must be at least 1 (got "
            f"{races_per_season}). Salary is charged per race, so a "
            f"zero-race season would make every contract free."
        )
    if min_term_races < 1:
        raise ConfigError(
            f"**Min contract length (races)** must be at least 1 (got "
            f"{min_term_races})."
        )
    if max_term_races < min_term_races:
        raise ConfigError(
            f"**Max contract length** ({max_term_races} races) is below the "
            f"**minimum** ({min_term_races} races). No contract length "
            f"would be legal, so every offer would be rejected."
        )
    if length_premium_pct < 0:
        raise ConfigError(
            "**Length premium** cannot be negative. A negative premium "
            "would pay teams to lock drivers into the longest deal "
            "possible, which is the opposite of the intent."
        )
    if resign_premium_pct < 0:
        raise ConfigError(
            "**Re-sign premium** cannot be negative. A negative premium "
            "would make re-signing your own driver cheaper than signing "
            "a rival's, which is what this setting exists to prevent."
        )


# ── chooser ──────────────────────────────────────────────────────────


def build_config_embed(current, *, scope_label: str) -> discord.Embed:
    """Shows the current values so an admin can see before they edit."""
    span = (
        f"exactly {current.min_term_seasons} season(s)"
        if current.min_term_seasons == current.max_term_seasons
        else f"{current.min_term_seasons}–{current.max_term_seasons} seasons"
    )
    ceiling = (
        "none" if current.max_salary is None else f"${current.max_salary}M"
    )
    embed = discord.Embed(
        title="💰 League rules",
        description=f"Editing **{scope_label}**. Pick a section to change.",
        color=COLOR_INFO,
    )
    embed.add_field(
        name="Money limits",
        value=(
            f"Salary cap **${current.salary_cap}M**\n"
            f"Salary floor **${current.min_salary}M** · ceiling **{ceiling}**\n"
            f"Weekly move cap ±${current.weekly_move_cap}M\n"
            f"Exceptional move cap ±${current.exceptional_move_cap}M"
        ),
        inline=False,
    )
    embed.add_field(
        name="Contract rules",
        value=(
            f"Contract length **{span}**\n"
            f"Active driver slots **{current.active_driver_slots}** per team\n"
            f"Max incentives **{current.max_incentive_pct * _PCT_SCALE:.1f}%** "
            f"of salary\n"
            f"Offers expire after **{current.offer_ttl_hours}h**"
        ),
        inline=False,
    )
    race_span = (
        f"exactly {current.min_term_races} race(s)"
        if current.min_term_races == current.max_term_races
        else f"{current.min_term_races}\u2013{current.max_term_races} races"
    )
    premiums = (
        "off — contract prices unchanged"
        if current.length_premium_pct == 0 and current.resign_premium_pct == 0
        else (
            f"length {current.length_premium_pct * _PCT_SCALE:.3f}%/race · "
            f"re-sign {current.resign_premium_pct * _PCT_SCALE:.3f}%"
        )
    )
    embed.add_field(
        name="Race terms & premiums",
        value=(
            f"Season length **{current.races_per_season}** races\n"
            f"Contract length **{race_span}**\n"
            f"Premiums **{premiums}**"
        ),
        inline=False,
    )
    return embed


class ConfigSectionView(OwnedView):
    """
    Three buttons, because five inputs is Discord's hard modal limit
    and the league has more than ten things to configure.

    Not admin-gated itself: every caller checks Manage Server before
    opening it, and the modals write through the same path either way.
    """

    def __init__(
        self,
        *,
        season_id: int,
        tier_id: int | None,
        current,
        opener_id: int,
        on_back: BackCallback | None = None,
    ) -> None:
        super().__init__(opener_id=opener_id)
        self.add_item(
            _SectionButton(
                MoneyConfigModal,
                label="Money limits",
                emoji="💰",
                season_id=season_id,
                tier_id=tier_id,
                current=current,
            )
        )
        self.add_item(
            _SectionButton(
                TermsConfigModal,
                label="Contract rules",
                emoji="📝",
                season_id=season_id,
                tier_id=tier_id,
                current=current,
            )
        )
        self.add_item(
            _SectionButton(
                RaceTermsConfigModal,
                label="Race terms & premiums",
                emoji="🏁",
                season_id=season_id,
                tier_id=tier_id,
                current=current,
            )
        )
        # Only the panel path has somewhere to go back to; the slash
        # command opens this as its own top-level ephemeral message.
        if on_back is not None:
            self.add_item(BackButton(on_back, label="Back to setup"))


class _SectionButton(discord.ui.Button):
    def __init__(
        self,
        modal_cls,
        *,
        label: str,
        emoji: str,
        season_id: int,
        tier_id: int | None,
        current,
    ) -> None:
        super().__init__(label=label, style=discord.ButtonStyle.primary, emoji=emoji)
        self._modal_cls = modal_cls
        self._season_id = season_id
        self._tier_id = tier_id
        self._current = current

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(
            self._modal_cls(
                season_id=self._season_id,
                tier_id=self._tier_id,
                current=self._current,
            )
        )
