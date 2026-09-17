"""
/contract command group — TP-side offer lifecycle.

Commands:
  Phase 4:
    /contract offer     multi-stage flow (args → modal → review → submit)
    /contract offers    my team(s)' outstanding offers
    /contract withdraw  cancel an open offer I own
    /contract counter   driver responds with new terms
    /contract accept    driver accepts an offer
    /contract decline   driver declines an offer
    /contract status    driver's active contract + open offers + history
  Phase 5:
    /contract release   end an active contract (frozen P/L in ledger)
    /contract buyout    release + dead-money row against team cap

Design notes:
  * CLAUDE.md §6 prescribes select-view → modal → review. Discord's
    modal-can't-contain-selects constraint is the reason. We honour
    the flow but move the enumerable fields (tier, team, offer_kind,
    TTL) onto slash-command args with Choice / autocomplete so users
    don't have to click through five dropdowns to get to the modal.
    The free-text fields live in the modal; the review panel is a
    followup with a Submit button. Contract type defaults to
    `standard` — the F1 preset only ships a few and Phase 4 MVP
    doesn't lean on type variety.
  * Authority: `_authorised_for_team` mirrors `/roster sign`'s
    permission model — the caller must hold `teams.principal_role_id`
    for the offering team, or Manage Server. Driver-side actions
    (accept/decline/counter) require the caller to be the driver's
    Discord member.
  * Persistence: the offer row is written the moment the review-panel
    Submit is clicked — a bot restart during the modal→review window
    only loses in-flight modal state, not a submitted offer.
"""

from __future__ import annotations

import logging
from decimal import Decimal, InvalidOperation

import discord
from discord import app_commands
from discord.ext import commands

from bot import db, queries
from bot.contracts import render as contract_render
from bot.contracts import rules, service
from bot.market import budget_ops
from bot.ui import base

log = logging.getLogger(__name__)

_DEFAULT_CONTRACT_TYPE = "standard"
# Same fallback `queries.derive_term_races` uses when no league_config
# row carries a calendar, so validation and storage cannot disagree.
_FALLBACK_RACES = 24
_OFFER_KIND_CHOICES = [
    app_commands.Choice(name="New signing", value="new"),
    app_commands.Choice(name="Extension", value="extension"),
    app_commands.Choice(name="Trade-and-sign", value="trade_and_sign"),
]
_TTL_CHOICES = [
    app_commands.Choice(name="24 hours", value=24),
    app_commands.Choice(name="48 hours", value=48),
    app_commands.Choice(name="72 hours", value=72),
    app_commands.Choice(name="7 days", value=168),
]


# ── permission helpers ─────────────────────────────────────────────────


def _is_admin(interaction: discord.Interaction) -> bool:
    if not isinstance(interaction.user, discord.Member):
        return False
    return interaction.user.guild_permissions.manage_guild


def _authorised_for_team(interaction: discord.Interaction, team) -> bool:
    if _is_admin(interaction):
        return True
    if not isinstance(interaction.user, discord.Member):
        return False
    if team.principal_role_id is None:
        return False
    return any(r.id == team.principal_role_id for r in interaction.user.roles)


async def _teams_user_principals(interaction) -> list:
    """Every team where the invoking member holds the principal role."""
    if not isinstance(interaction.user, discord.Member) or interaction.guild_id is None:
        return []
    async with db.connect() as conn:
        all_teams = await queries.fetch_all_teams(conn, interaction.guild_id)
    return [
        t for t in all_teams
        if t.principal_role_id is not None
        and any(r.id == t.principal_role_id for r in interaction.user.roles)
    ]


# ── cog ────────────────────────────────────────────────────────────────


class ContractsCog(commands.Cog):
    contract = app_commands.Group(
        name="contract",
        description="Contract offers, counters, and status",
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ── /contract offer ─────────────────────────────────────────────────

    @contract.command(name="offer", description="Open a new contract offer to a driver")
    @app_commands.describe(
        team="Team you're making the offer for",
        tier="Driver's tier (e.g. t1)",
        driver="Driver's Discord account",
        offer_kind="Kind of deal",
        ttl_hours="How long the driver has to respond",
    )
    @app_commands.choices(offer_kind=_OFFER_KIND_CHOICES, ttl_hours=_TTL_CHOICES)
    async def contract_offer(
        self,
        interaction: discord.Interaction,
        team: str,
        tier: str,
        driver: discord.Member,
        offer_kind: app_commands.Choice[str],
        ttl_hours: app_commands.Choice[int],
    ) -> None:
        assert interaction.guild_id is not None

        async with db.connect() as conn:
            team_row = await queries.fetch_team(conn, interaction.guild_id, team.lower())
            if team_row is None:
                await interaction.response.send_message(
                    f"No team `{team}`.", ephemeral=True
                )
                return
            if not _authorised_for_team(interaction, team_row):
                await interaction.response.send_message(
                    f"You aren't a principal for **{team_row.name}**.",
                    ephemeral=True,
                )
                return
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            tier_row = await queries.fetch_tier(conn, season.id, tier)
            if tier_row is None:
                await interaction.response.send_message(
                    f"No tier `{tier}` in **{season.name}**.", ephemeral=True
                )
                return
            driver_row = await queries.fetch_driver(
                conn, season.id, tier_row.id, driver.id
            )
            if driver_row is None:
                await interaction.response.send_message(
                    f"{driver.display_name} isn't registered in tier `{tier}`.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(
            _OfferModal(
                team=team_row,
                tier=tier_row,
                season_id=season.id,
                driver=driver,
                driver_row=driver_row,
                offer_kind=offer_kind.value,
                ttl_hours=ttl_hours.value,
            )
        )

    @contract_offer.autocomplete("team")
    async def _team_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        teams = await _teams_user_principals(interaction)
        if _is_admin(interaction) and not teams:
            # Admins might principal nothing but still act on any team.
            assert interaction.guild_id is not None
            async with db.connect() as conn:
                teams = await queries.fetch_all_teams(conn, interaction.guild_id)
        low = current.lower()
        matches = [t for t in teams if low in t.key.lower() or low in t.name.lower()]
        return [
            app_commands.Choice(name=t.name, value=t.key)
            for t in matches[:25]
        ]

    @contract_offer.autocomplete("tier")
    async def _tier_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                return []
            tiers = await queries.fetch_all_tiers(conn, season.id)
        low = current.lower()
        matches = [t for t in tiers if low in t.code.lower() or low in t.label.lower()]
        return [
            app_commands.Choice(name=f"{t.code} — {t.label}", value=t.code)
            for t in matches[:25]
        ]

    # ── /contract offers ────────────────────────────────────────────────

    @contract.command(name="offers", description="My team(s)' open offers")
    async def contract_offers(self, interaction: discord.Interaction) -> None:
        teams = await _teams_user_principals(interaction)
        if not teams and not _is_admin(interaction):
            await interaction.response.send_message(
                "You aren't a principal for any team.", ephemeral=True
            )
            return

        rows: list[dict] = []
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            if _is_admin(interaction) and not teams:
                teams = await queries.fetch_all_teams(conn, interaction.guild_id)
            for t in teams:
                offers = await queries.fetch_open_offers_for_team(conn, t.id)
                for o in offers:
                    driver_row = await conn.fetchrow(
                        "SELECT display_name FROM drivers WHERE id = $1",
                        o.driver_id,
                    )
                    rows.append({
                        "id": o.id,
                        "state": o.state,
                        "salary": o.salary,
                        "term_seasons": o.term_seasons,
                        "driver_name": (
                            driver_row["display_name"] if driver_row else "?"
                        ),
                        "expires_at": o.expires_at,
                    })

        embed = contract_render.render_offers_list(
            title="Open offers", offers=rows
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /contract withdraw ──────────────────────────────────────────────

    @contract.command(name="withdraw", description="Cancel an open offer you own")
    @app_commands.describe(offer_id="Offer id (see /contract offers)")
    async def contract_withdraw(
        self, interaction: discord.Interaction, offer_id: int
    ) -> None:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            offer = await queries.fetch_offer_by_id(conn, offer_id)
            if offer is None:
                await interaction.response.send_message(
                    f"No offer `{offer_id}`.", ephemeral=True
                )
                return
            team = await queries.fetch_team_by_id(conn, offer.team_id)
            if team is None or not _authorised_for_team(interaction, team):
                await interaction.response.send_message(
                    "You aren't authorised to withdraw this offer.", ephemeral=True
                )
                return
            try:
                await service.team_withdraw(
                    conn, offer_id, actor_id=interaction.user.id
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        await interaction.response.send_message(
            f"✅ Offer `{offer_id}` withdrawn.", ephemeral=True
        )

    # ── /contract accept ────────────────────────────────────────────────

    @contract.command(name="accept", description="Accept an offer (driver only)")
    @app_commands.describe(offer_id="Offer id from the offer card")
    async def contract_accept(
        self, interaction: discord.Interaction, offer_id: int
    ) -> None:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            offer = await queries.fetch_offer_by_id(conn, offer_id)
            if offer is None:
                await interaction.response.send_message(
                    f"No offer `{offer_id}`.", ephemeral=True
                )
                return
            driver_row = await conn.fetchrow(
                "SELECT member_id FROM drivers WHERE id = $1", offer.driver_id
            )
            if driver_row is None or driver_row["member_id"] != interaction.user.id:
                await interaction.response.send_message(
                    "Only the driver named on the offer can accept it.",
                    ephemeral=True,
                )
                return
            try:
                await service.driver_accept(
                    conn, offer_id, actor_id=interaction.user.id
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        await interaction.response.send_message(
            f"✅ Accepted offer `{offer_id}`. Waiting on commissioner "
            f"approval — `/market-admin approve offer_id: {offer_id}`.",
            ephemeral=True,
        )

    # ── /contract decline ───────────────────────────────────────────────

    @contract.command(name="decline", description="Decline an offer (driver only)")
    @app_commands.describe(
        offer_id="Offer id from the offer card",
        note="Optional note for the audit log",
    )
    async def contract_decline(
        self,
        interaction: discord.Interaction,
        offer_id: int,
        note: str | None = None,
    ) -> None:
        async with db.connect() as conn:
            offer = await queries.fetch_offer_by_id(conn, offer_id)
            if offer is None:
                await interaction.response.send_message(
                    f"No offer `{offer_id}`.", ephemeral=True
                )
                return
            driver_row = await conn.fetchrow(
                "SELECT member_id FROM drivers WHERE id = $1", offer.driver_id
            )
            if driver_row is None or driver_row["member_id"] != interaction.user.id:
                await interaction.response.send_message(
                    "Only the driver named on the offer can decline it.",
                    ephemeral=True,
                )
                return
            try:
                await service.driver_decline(
                    conn, offer_id, actor_id=interaction.user.id, note=note
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        await interaction.response.send_message(
            f"✅ Declined offer `{offer_id}`.", ephemeral=True
        )

    # ── /contract counter ───────────────────────────────────────────────

    @contract.command(name="counter", description="Counter with new terms (driver only)")
    @app_commands.describe(offer_id="Offer id you want to counter")
    async def contract_counter(
        self, interaction: discord.Interaction, offer_id: int
    ) -> None:
        async with db.connect() as conn:
            offer = await queries.fetch_offer_by_id(conn, offer_id)
            if offer is None:
                await interaction.response.send_message(
                    f"No offer `{offer_id}`.", ephemeral=True
                )
                return
            driver_row = await conn.fetchrow(
                "SELECT member_id FROM drivers WHERE id = $1", offer.driver_id
            )
            if driver_row is None or driver_row["member_id"] != interaction.user.id:
                await interaction.response.send_message(
                    "Only the driver named on the offer can counter.",
                    ephemeral=True,
                )
                return

        await interaction.response.send_modal(
            _CounterModal(parent_offer=offer)
        )

    # ── /contract status ────────────────────────────────────────────────

    @contract.command(name="status", description="Driver's contract + open offers + history")
    @app_commands.describe(driver="Driver's Discord account")
    async def contract_status(
        self, interaction: discord.Interaction, driver: discord.Member
    ) -> None:
        assert interaction.guild_id is not None
        async with db.connect() as conn:
            season = await queries.fetch_active_season(conn, interaction.guild_id)
            if season is None:
                await interaction.response.send_message(
                    "No active season.", ephemeral=True
                )
                return
            driver_row = await queries.fetch_driver_by_member(
                conn, season.id, driver.id
            )
            if driver_row is None:
                await interaction.response.send_message(
                    f"{driver.display_name} isn't registered as a driver in "
                    f"**{season.name}**.",
                    ephemeral=True,
                )
                return
            tier = await queries.fetch_tier_by_id(conn, driver_row.tier_id)
            active = await queries.fetch_active_contract_for_driver(
                conn, driver_row.id
            )
            active_dict = None
            if active is not None:
                team = await queries.fetch_team_by_id(conn, active.team_id)
                active_dict = {
                    "team_name": team.name if team else "?",
                    "contract_value": active.contract_value,
                    "term_seasons": active.term_seasons,
                    "season_index": active.season_index,
                    "contract_type": active.contract_type,
                    "signed_at": active.signed_at,
                    "external_ref": active.external_ref,
                }
            open_offers = await queries.fetch_open_offers_for_driver(
                conn, driver_row.id
            )
            offers_view: list[dict] = []
            for o in open_offers:
                team = await queries.fetch_team_by_id(conn, o.team_id)
                offers_view.append({
                    "id": o.id,
                    "state": o.state,
                    "team_name": team.name if team else "?",
                    "salary": o.salary,
                })
            history_raw = await queries.fetch_contract_history_for_driver(
                conn, driver_row.id
            )
            history_view: list[dict] = []
            for h in history_raw:
                team = await queries.fetch_team_by_id(conn, h.team_id)
                history_view.append({
                    "team_name": team.name if team else "?",
                    "contract_value": h.contract_value,
                    "state": h.state,
                })

        embed = contract_render.render_contract_status(
            driver_name=driver_row.display_name,
            tier_label=tier.label if tier else "?",
            active=active_dict,
            history=history_view,
            open_offers=offers_view,
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ── /contract release ───────────────────────────────────────────────

    @contract.command(name="release", description="End an active contract")
    @app_commands.describe(
        contract_id="Contract id to release",
        note="Reason (recorded in the audit ledger)",
    )
    async def contract_release(
        self, interaction: discord.Interaction, contract_id: int, note: str
    ) -> None:
        assert interaction.guild is not None
        async with db.connect() as conn:
            contract = await queries.fetch_contract_by_id(conn, contract_id)
            if contract is None:
                await interaction.response.send_message(
                    f"No contract `{contract_id}`.", ephemeral=True
                )
                return
            team = await queries.fetch_team_by_id(conn, contract.team_id)
            if team is None or not _authorised_for_team(interaction, team):
                await interaction.response.send_message(
                    "You aren't authorised to release for this team.",
                    ephemeral=True,
                )
                return
            market_value = await queries.fetch_latest_published_valuation(
                conn, contract.driver_id
            )
            try:
                await service.release_contract(
                    conn, contract_id,
                    actor_id=interaction.user.id,
                    market_value_at_release=market_value,
                    note=note,
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

            # Best-effort role drop via the shared helper. Failure is
            # logged but does not undo the release — the money side is
            # the authoritative record.
            driver_row = await conn.fetchrow(
                "SELECT member_id, display_name FROM drivers WHERE id = $1",
                contract.driver_id,
            )

        role_note: str | None = None
        member = interaction.guild.get_member(driver_row["member_id"])
        if member is not None and team is not None:
            try:
                from bot import roster_ops
                await roster_ops.drop_from_team(
                    guild=interaction.guild, member=member, team=team,
                    actor=interaction.user,
                    reason=f"Contract {contract_id} released",
                )
            except Exception as exc:  # noqa: BLE001
                role_note = f"\n⚠ Role not dropped: {exc}"

        await interaction.response.send_message(
            f"✅ Released contract `{contract_id}` "
            f"({driver_row['display_name']}).{role_note or ''}",
            ephemeral=True,
        )

    # ── /contract buyout ────────────────────────────────────────────────

    @contract.command(name="buyout", description="Buy out an active contract")
    @app_commands.describe(
        contract_id="Contract id to buy out",
        buyout_m="Buyout amount in $M (dead money against team cap this season)",
        note="Reason (recorded in the audit ledger)",
    )
    async def contract_buyout(
        self,
        interaction: discord.Interaction,
        contract_id: int,
        buyout_m: str,
        note: str,
    ) -> None:
        assert interaction.guild is not None
        try:
            buyout = Decimal(buyout_m.strip().lstrip("$").rstrip("Mm"))
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse buyout amount: {exc}", ephemeral=True
            )
            return
        async with db.connect() as conn:
            contract = await queries.fetch_contract_by_id(conn, contract_id)
            if contract is None:
                await interaction.response.send_message(
                    f"No contract `{contract_id}`.", ephemeral=True
                )
                return
            team = await queries.fetch_team_by_id(conn, contract.team_id)
            if team is None or not _authorised_for_team(interaction, team):
                await interaction.response.send_message(
                    "You aren't authorised to buy out for this team.",
                    ephemeral=True,
                )
                return
            market_value = await queries.fetch_latest_published_valuation(
                conn, contract.driver_id
            )
            try:
                await service.buyout_contract(
                    conn, contract_id,
                    actor_id=interaction.user.id,
                    buyout_amount=buyout,
                    market_value_at_release=market_value,
                    note=note,
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return
            driver_row = await conn.fetchrow(
                "SELECT member_id, display_name FROM drivers WHERE id = $1",
                contract.driver_id,
            )

        role_note: str | None = None
        member = interaction.guild.get_member(driver_row["member_id"])
        if member is not None and team is not None:
            try:
                from bot import roster_ops
                await roster_ops.drop_from_team(
                    guild=interaction.guild, member=member, team=team,
                    actor=interaction.user,
                    reason=f"Contract {contract_id} bought out",
                )
            except Exception as exc:  # noqa: BLE001
                role_note = f"\n⚠ Role not dropped: {exc}"

        await interaction.response.send_message(
            f"✅ Bought out contract `{contract_id}` "
            f"({driver_row['display_name']}) — dead money "
            f"${buyout}M against the team cap this season."
            f"{role_note or ''}",
            ephemeral=True,
        )


# ── modals + views ─────────────────────────────────────────────────────


def _races_per_season(cfg) -> int:
    """
    The league's calendar, with the same 24-race fallback
    `queries.derive_term_races` applies, so a term that is validated
    here and stored there cannot come out different.
    """
    return getattr(cfg, "races_per_season", None) or _FALLBACK_RACES


def _offer_term_races(cfg, term_seasons: int) -> int:
    """
    The race term a season-denominated offer means.

    Still used for anything that only knows a season count.
    """
    return max(1, term_seasons * _races_per_season(cfg))


def _seasons_for_races(cfg, term_races: int) -> int:
    """
    The season span a race term touches, rounded UP.

    `term_seasons` remains on the offer because the salary is a
    per-season rate and the season-denominated bounds are still checked
    against it. A 10-race deal in a 24-race season touches one season; a
    30-race deal touches two.

    Rounding up rather than down matters: a 30-race deal that reported
    one season would be quoted against a single season's salary while
    actually running into a second.
    """
    per_season = _races_per_season(cfg)
    return max(1, -(-term_races // per_season))


def parse_term(cfg, raw: str) -> tuple[int, int]:
    """
    Read a Team Principal's term entry as `(term_races, term_seasons)`.

    Offers are made in RACES since migration 018, because a whole-season
    term could not express a 10-race stand-in deal or match the races
    remaining when re-signing mid-term. Seasons stay available as a
    convenience because most deals are still full seasons and nobody
    wants to type 48:

        "10"   -> 10 races
        "2s"   -> 2 seasons  -> 48 races on a 24-race calendar
        "2 seasons"

    Raises ValueError with a message meant to be shown to the TP.
    """
    text = raw.strip().lower()
    if not text:
        raise ValueError("Enter a contract length.")
    seasons_asked = False
    # Race suffixes are stripped FIRST. "races" ends in "s", so checking
    # the season suffixes first read "5 races" as five SEASONS -- a
    # 120-race deal from a TP who asked for five races.
    for suffix in ("races", "race", "r"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
            break
    else:
        for suffix in ("seasons", "season", "s"):
            if text.endswith(suffix):
                text = text[: -len(suffix)].strip()
                seasons_asked = True
                break
    if not text:
        raise ValueError(
            "Enter a number — for example `10` for ten races, or `2s` "
            "for two seasons."
        )
    try:
        count = int(text)
    except ValueError:
        raise ValueError(
            f"Could not read {raw.strip()!r} as a contract length. Use a "
            "number of races (`10`) or seasons (`2s`)."
        ) from None
    if count < 1:
        raise ValueError("A contract must run for at least one race.")
    if seasons_asked:
        races = _offer_term_races(cfg, count)
        return races, count
    return count, _seasons_for_races(cfg, count)


class _OfferModal(base.PanelModal):
    def __init__(
        self,
        *,
        team,
        tier,
        season_id: int,
        driver: discord.Member,
        driver_row,
        offer_kind: str,
        ttl_hours: int,
    ) -> None:
        super().__init__(title=f"Offer — {team.name} → {driver.display_name}")
        self._team = team
        self._tier = tier
        self._season_id = season_id
        self._driver = driver
        self._driver_row = driver_row
        self._offer_kind = offer_kind
        self._ttl_hours = ttl_hours

        self._salary = discord.ui.TextInput(
            label="Salary ($M)", placeholder="e.g. 12.50", required=True
        )
        self._term = discord.ui.TextInput(
            label="Term (races, or '2s' for seasons)",
            placeholder="e.g. 10 for ten races, or 2s for two seasons",
            required=True,
        )
        self._bonus = discord.ui.TextInput(
            label="Signing bonus ($M)", placeholder="0.00", required=False,
            default="0",
        )
        self._incentives = discord.ui.TextInput(
            label="Incentives (free text)", required=False,
            style=discord.TextStyle.paragraph,
        )
        self._message = discord.ui.TextInput(
            label="Note to driver", required=False,
            style=discord.TextStyle.paragraph,
        )
        for w in (self._salary, self._term, self._bonus,
                  self._incentives, self._message):
            self.add_item(w)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            salary = Decimal(self._salary.value.strip())
            signing_bonus = Decimal(self._bonus.value.strip() or "0")
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse a number: {exc}", ephemeral=True
            )
            return

        assert interaction.guild_id is not None
        async with db.connect() as conn:
            cfg = await queries.fetch_league_config_row(
                conn, self._season_id, self._tier.id
            )
            if cfg is None:
                cfg = await queries.fetch_league_config_row(
                    conn, self._season_id, None
                )
            if cfg is None:
                await interaction.response.send_message(
                    "No league_config for this scope. Ask a commissioner "
                    "to seed the F1 preset first.",
                    ephemeral=True,
                )
                return
            # Parsed here, not above, because reading "2s" needs the
            # league's calendar and the calendar lives on cfg.
            try:
                term_races, term_seasons = parse_term(
                    cfg, self._term.value
                )
            except ValueError as exc:
                await interaction.response.send_message(
                    str(exc), ephemeral=True
                )
                return
            payroll_before = await queries.fetch_team_effective_payroll(
                conn, self._team.id, self._season_id,
            )
            # G18: cap_adjustment rows move this team's ceiling.
            cap_adjustment = await queries.fetch_cap_adjustment_total(
                conn, self._season_id, self._team.id,
            )
            budget_snap = await budget_ops.snapshot(
                conn, season_id=self._season_id, tier_id=self._tier.id,
                team_id=self._team.id, actor_id=interaction.user.id,
            )
            slots_used = await queries.fetch_team_active_slot_count(conn, self._team.id)
            existing_contract = await queries.fetch_active_contract_for_driver(
                conn, self._driver_row.id
            )
            duplicate = await queries.fetch_open_offer_for_team_driver(
                conn, self._team.id, self._driver_row.id
            )
            market_value = await queries.fetch_latest_published_valuation(
                conn, self._driver_row.id
            )

        acting_as_admin = _is_admin(interaction)
        acting_as_principal = (
            _authorised_for_team(interaction, self._team) and not acting_as_admin
        )
        inputs = rules.OfferInputs(
            actor_id=interaction.user.id,
            actor_is_principal=acting_as_principal,
            actor_is_admin=acting_as_admin,
            driver_present_in_tier=True,
            driver_status=self._driver_row.status,
            driver_has_active_contract=existing_contract is not None,
            duplicate_open_offer_exists=duplicate is not None,
            salary=salary,
            min_salary=cfg.min_salary,
            max_salary=cfg.max_salary,
            signing_bonus=signing_bonus,
            incentives_amount=service._parse_incentives_amount(
                self._incentives.value or None
            ),
            max_incentive_pct=cfg.max_incentive_pct,
            team_payroll_before=payroll_before,
            salary_cap=cfg.salary_cap,
            cap_adjustment=cap_adjustment,
            team_budget=budget_snap.balance if budget_snap else None,
            active_slots_used=slots_used,
            active_slots_max=cfg.active_driver_slots,
            has_linked_release=False,
            term_seasons=term_seasons,
            min_term_seasons=cfg.min_term_seasons,
            max_term_seasons=cfg.max_term_seasons,
            term_races=term_races,
            min_term_races=cfg.min_term_races,
            max_term_races=cfg.max_term_races,
            offer_kind=self._offer_kind,
            free_agency_open=cfg.free_agency_open,
        )
        validation = rules.validate_offer(inputs)

        review = contract_render.render_review_panel(
            team_name=self._team.name,
            driver_name=self._driver.display_name,
            tier_label=self._tier.label,
            offer_kind=self._offer_kind,
            salary=salary,
            term_seasons=term_seasons,
            contract_type=_DEFAULT_CONTRACT_TYPE,
            signing_bonus=signing_bonus,
            incentives=self._incentives.value or None,
            message=self._message.value or None,
            payroll_before=payroll_before,
            salary_cap=cfg.salary_cap,
            current_market_value=market_value,
            validation=validation,
            team_budget=budget_snap.balance if budget_snap else None,
            term_races=term_races,
            races_per_season=_races_per_season(cfg),
        )
        view = _ReviewSubmitView(
            team=self._team,
            tier=self._tier,
            season_id=self._season_id,
            driver=self._driver,
            driver_row=self._driver_row,
            offer_kind=self._offer_kind,
            ttl_hours=self._ttl_hours,
            salary=salary,
            term_seasons=term_seasons,
            signing_bonus=signing_bonus,
            incentives=self._incentives.value or None,
            message=self._message.value or None,
            validation=validation,
            term_races=term_races,
        )
        await interaction.response.send_message(
            embed=review, view=view, ephemeral=True
        )


class _ReviewSubmitView(discord.ui.View):
    def __init__(
        self,
        *,
        team, tier, season_id: int, driver: discord.Member, driver_row,
        offer_kind: str, ttl_hours: int,
        salary: Decimal, term_seasons: int, signing_bonus: Decimal,
        incentives: str | None, message: str | None,
        validation: rules.OfferValidation,
        term_races: int,
    ) -> None:
        super().__init__(timeout=300)
        self._team = team
        self._tier = tier
        self._season_id = season_id
        self._driver = driver
        self._driver_row = driver_row
        self._offer_kind = offer_kind
        self._ttl_hours = ttl_hours
        self._salary = salary
        self._term = term_seasons
        self._term_races = term_races
        self._bonus = signing_bonus
        self._incentives = incentives
        self._message = message
        self._validation = validation
        self.submit_btn.disabled = not validation.ok

    @discord.ui.button(label="Submit offer", style=discord.ButtonStyle.success)
    async def submit_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        if not self._validation.ok:
            await interaction.response.send_message(
                "Cannot submit — one or more checks failed.", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)
        async with db.connect() as conn:
            cfg = await queries.fetch_league_config_row(
                conn, self._season_id, self._tier.id
            ) or await queries.fetch_league_config_row(
                conn, self._season_id, None
            )
            offer_id = await service.submit_offer(
                conn,
                season_id=self._season_id,
                tier_id=self._tier.id,
                driver_id=self._driver_row.id,
                team_id=self._team.id,
                offered_by=interaction.user.id,
                offer_kind=self._offer_kind,
                salary=self._salary,
                term_seasons=self._term,
                contract_type=_DEFAULT_CONTRACT_TYPE,
                signing_bonus=self._bonus,
                incentives=self._incentives,
                message=self._message,
                ttl_hours=self._ttl_hours,
                validation=self._validation.to_json(),
                initial_state="pending_driver",
                term_races=self._term_races,
            )
            approvals_channel_id = (cfg.approvals_channel_id if cfg else None)

        thread_id = await _deliver_offer(
            self.bot_from_interaction(interaction),
            interaction=interaction,
            offer_id=offer_id,
            team=self._team,
            tier=self._tier,
            driver=self._driver,
            salary=self._salary,
            term_seasons=self._term,
            signing_bonus=self._bonus,
            incentives=self._incentives,
            message=self._message,
            ttl_hours=self._ttl_hours,
            approvals_channel_id=approvals_channel_id,
            term_races=self._term_races,
            races_per_season=(
                _races_per_season(cfg) if cfg is not None else None
            ),
        )
        if thread_id is not None:
            async with db.connect() as conn:
                await service.record_thread(conn, offer_id, thread_id)

        thread_bit = f" (thread: <#{thread_id}>)" if thread_id else ""
        await interaction.followup.send(
            f"✅ Offer `{offer_id}` submitted to **{self._driver.display_name}**"
            f"{thread_bit}.",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_btn(
        self, interaction: discord.Interaction, _button: discord.ui.Button
    ) -> None:
        await interaction.response.edit_message(content="Cancelled.", view=None)
        self.stop()

    @staticmethod
    def bot_from_interaction(interaction: discord.Interaction) -> commands.Bot:
        return interaction.client  # type: ignore[return-value]


class _CounterModal(base.PanelModal):
    def __init__(self, *, parent_offer) -> None:
        super().__init__(title=f"Counter offer #{parent_offer.id}")
        self._owner = parent_offer
        self._salary = discord.ui.TextInput(
            label="Counter salary ($M)",
            default=str(parent_offer.salary), required=True,
        )
        self._term = discord.ui.TextInput(
            label="Term (races, or '2s' for seasons)",
            default=str(parent_offer.term_races), required=True,
        )
        self._bonus = discord.ui.TextInput(
            label="Signing bonus ($M)",
            default=str(parent_offer.signing_bonus), required=False,
        )
        self._incentives = discord.ui.TextInput(
            label="Incentives", required=False,
            style=discord.TextStyle.paragraph,
            default=parent_offer.incentives or "",
        )
        self._message = discord.ui.TextInput(
            label="Note to team", required=False,
            style=discord.TextStyle.paragraph,
        )
        for w in (self._salary, self._term, self._bonus,
                  self._incentives, self._message):
            self.add_item(w)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            salary = Decimal(self._salary.value.strip())
            bonus = Decimal(self._bonus.value.strip() or "0")
        except InvalidOperation as exc:
            await interaction.response.send_message(
                f"Could not parse a number: {exc}", ephemeral=True
            )
            return

        async with db.connect() as conn:
            cfg = await queries.fetch_league_config_row(
                conn, self._owner.season_id, self._owner.tier_id
            ) or await queries.fetch_league_config_row(
                conn, self._owner.season_id, None
            )
            if cfg is None:
                await interaction.response.send_message(
                    "No league_config for this scope.", ephemeral=True
                )
                return
            # Needs the calendar, so parsed after cfg loads.
            try:
                term_races, term = parse_term(cfg, self._term.value)
            except ValueError as exc:
                await interaction.response.send_message(
                    str(exc), ephemeral=True
                )
                return
            payroll_before = await queries.fetch_team_effective_payroll(
                conn, self._owner.team_id, self._owner.season_id,
            )
            # G18: the counter is validated against the same ceiling.
            cap_adjustment = await queries.fetch_cap_adjustment_total(
                conn, self._owner.season_id, self._owner.team_id,
            )
            budget_snap = await budget_ops.snapshot(
                conn, season_id=self._owner.season_id, tier_id=self._owner.tier_id,
                team_id=self._owner.team_id, actor_id=interaction.user.id,
            )
            slots_used = await queries.fetch_team_active_slot_count(
                conn, self._owner.team_id
            )
            driver_row = await conn.fetchrow(
                "SELECT status FROM drivers WHERE id = $1", self._owner.driver_id
            )
            existing = await queries.fetch_active_contract_for_driver(
                conn, self._owner.driver_id
            )
            inputs = rules.OfferInputs(
                actor_id=interaction.user.id,
                actor_is_principal=False,
                actor_is_admin=False,
                driver_present_in_tier=True,
                driver_status=driver_row["status"] if driver_row else "unknown",
                driver_has_active_contract=existing is not None,
                duplicate_open_offer_exists=False,
                salary=salary,
                min_salary=cfg.min_salary,
                max_salary=cfg.max_salary,
                signing_bonus=bonus,
                incentives_amount=service._parse_incentives_amount(
                    self._incentives.value or None
                ),
                max_incentive_pct=cfg.max_incentive_pct,
                team_payroll_before=payroll_before,
                salary_cap=cfg.salary_cap,
                cap_adjustment=cap_adjustment,
                team_budget=budget_snap.balance if budget_snap else None,
                active_slots_used=slots_used,
                active_slots_max=cfg.active_driver_slots,
                has_linked_release=False,
                term_seasons=term,
                min_term_seasons=cfg.min_term_seasons,
                max_term_seasons=cfg.max_term_seasons,
                term_races=term_races,
                min_term_races=cfg.min_term_races,
                max_term_races=cfg.max_term_races,
                offer_kind=self._owner.offer_kind,
                free_agency_open=cfg.free_agency_open,
            )
            # Skip actor-is-authorised for counter validation — the
            # counter comes from the driver, not a team principal.
            validation = rules.validate_offer(inputs)

            try:
                child_id = await service.driver_counter(
                    conn,
                    self._owner.id,
                    actor_id=interaction.user.id,
                    salary=salary,
                    term_seasons=term,
                    signing_bonus=bonus,
                    incentives=self._incentives.value or None,
                    message=self._message.value or None,
                    ttl_hours=cfg.offer_ttl_hours,
                    validation=validation.to_json(),
                    term_races=term_races,
                )
            except service.TransitionError as exc:
                await interaction.response.send_message(str(exc), ephemeral=True)
                return

        await interaction.response.send_message(
            f"✅ Counter `{child_id}` sent to team for review.",
            ephemeral=True,
        )


# ── delivery helpers ───────────────────────────────────────────────────


async def _deliver_offer(
    bot: commands.Bot,
    *,
    interaction: discord.Interaction,
    offer_id: int,
    team,
    tier,
    driver: discord.Member,
    salary: Decimal,
    term_seasons: int,
    signing_bonus: Decimal,
    incentives: str | None,
    message: str | None,
    ttl_hours: int,
    approvals_channel_id: int | None,
    term_races: int | None = None,
    races_per_season: int | None = None,
) -> int | None:
    """
    Create a private negotiation thread in the approvals channel with
    the driver + TP, post the offer card, return the thread id. Fall
    back to a DM if the thread cannot be created.

    The driver must see the term in the unit it was agreed in: a
    10-race deal shown as "1 season(s)" is a different deal.
    """
    from datetime import UTC, datetime, timedelta

    expires_at = datetime.now(UTC) + timedelta(hours=ttl_hours)
    async with db.connect() as conn:
        market_value = await queries.fetch_latest_published_valuation(
            conn, (await queries.fetch_driver_by_member(
                conn, tier.season_id, driver.id
            )).id
        )
        current_contract = None
        active = await queries.fetch_active_contract_for_driver(
            conn, (await queries.fetch_driver_by_member(
                conn, tier.season_id, driver.id
            )).id
        )
        if active is not None:
            current_contract = active.contract_value

    embed = contract_render.render_driver_offer_card(
        team_name=team.name,
        tier_label=tier.label,
        salary=salary,
        term_seasons=term_seasons,
        contract_type=_DEFAULT_CONTRACT_TYPE,
        signing_bonus=signing_bonus,
        incentives=incentives,
        message=message,
        expires_at=expires_at,
        current_market_value=market_value,
        current_contract_value=current_contract,
        term_races=term_races,
        races_per_season=races_per_season,
    )
    footer_line = (
        f"Offer id `{offer_id}`. `/contract accept id: {offer_id}` · "
        f"`/contract decline id: {offer_id}` · "
        f"`/contract counter id: {offer_id}`"
    )

    if approvals_channel_id is not None:
        channel = bot.get_channel(approvals_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                thread = await channel.create_thread(
                    name=(
                        f"offer-{offer_id}-{team.key}-{driver.display_name}"
                    )[:100],
                    type=discord.ChannelType.private_thread,
                    invitable=False,
                    reason=f"Offer {offer_id} negotiation",
                )
                await thread.send(
                    content=(
                        f"{driver.mention} — new offer from **{team.name}**.\n"
                        f"{footer_line}"
                    ),
                    embed=embed,
                )
                return thread.id
            except (discord.Forbidden, discord.HTTPException) as exc:
                log.warning(
                    "Could not create negotiation thread for offer %s: %s",
                    offer_id, exc,
                )

    try:
        await driver.send(embed=embed, content=footer_line)
    except discord.Forbidden:
        log.warning(
            "Could not DM driver %s about offer %s (DMs disabled).",
            driver.id, offer_id,
        )
    return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ContractsCog(bot))
