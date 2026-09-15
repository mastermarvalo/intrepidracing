"""
Commissioner approval orchestration, shared by the slash commands and
the control panel.

This is deliberately NOT in `bot/workflow.py`. Approving an offer is not
a pure data operation: it assigns Discord roles through `roster_ops` and
posts a public embed to the transactions channel. Those are unavoidably
Discord-aware, so they live here, and `workflow.py` stays free of guild
objects.

The ordering inside `approve_offer` and `approve_trade` is load-bearing
and preserved exactly from the original command bodies:

  1. The money side commits inside the DB transaction.
  2. Role changes happen *after* the transaction, outside it.
  3. A role failure is reported but never undoes the contract.

Step 3 is a deliberate choice, not an oversight. The contract and the
ledger are authoritative; a missing Discord role is cosmetic and can be
fixed by hand, whereas rolling back an approved contract would leave the
ledger and the cap sheet disagreeing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import discord

from bot import db, queries, roster_ops, workflow
from bot.contracts import render as contract_render
from bot.contracts import service as contracts_service

log = logging.getLogger(__name__)

# How many queue rows the panel renders per page. Discord allows 25
# select options; the queue uses one option per item plus none reserved.
QUEUE_PAGE_SIZE = 25


class ApprovalError(Exception):
    """Raised with text meant to be shown directly to the actor."""


@dataclass(frozen=True)
class OfferApprovalResult:
    offer_id: int
    contract_id: int
    external_ref: str
    role_warning: str | None = None


@dataclass(frozen=True)
class TradeApprovalResult:
    trade_id: int
    role_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class PendingOffer:
    offer_id: int
    driver_name: str
    team_name: str
    tier_code: str
    salary: Decimal
    term_seasons: int
    contract_type: str
    signing_bonus: Decimal
    offer_kind: str
    created_at: datetime


@dataclass(frozen=True)
class PendingTrade:
    trade_id: int
    proposing_team_name: str
    other_team_name: str
    item_count: int
    created_at: datetime


@dataclass(frozen=True)
class PendingQueue:
    season_id: int | None
    season_name: str | None
    offers: list[PendingOffer] = field(default_factory=list)
    trades: list[PendingTrade] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.offers) + len(self.trades)

    @property
    def is_empty(self) -> bool:
        return self.total == 0


async def fetch_pending_queue(
    guild_id: int, *, limit: int = QUEUE_PAGE_SIZE
) -> PendingQueue:
    """Read-only snapshot of everything waiting on a commissioner."""
    async with db.connect() as conn:
        season = await queries.fetch_active_season(conn, guild_id)
        if season is None:
            return PendingQueue(season_id=None, season_name=None)

        offer_rows = await queries.fetch_offers_awaiting_approval(
            conn, season.id, limit
        )
        trade_rows = await queries.fetch_trades_awaiting_approval(
            conn, season.id, limit
        )

    return PendingQueue(
        season_id=season.id,
        season_name=season.name,
        offers=[
            PendingOffer(
                offer_id=r["id"],
                driver_name=r["driver_name"],
                team_name=r["team_name"],
                tier_code=r["tier_code"],
                salary=r["salary"],
                term_seasons=r["term_seasons"],
                contract_type=r["contract_type"],
                signing_bonus=r["signing_bonus"],
                offer_kind=r["offer_kind"],
                created_at=r["created_at"],
            )
            for r in offer_rows
        ],
        trades=[
            PendingTrade(
                trade_id=r["id"],
                proposing_team_name=r["proposing_team_name"],
                other_team_name=r["other_team_name"],
                item_count=r["item_count"],
                created_at=r["created_at"],
            )
            for r in trade_rows
        ],
    )


# ── Offers ───────────────────────────────────────────────────────────


async def approve_offer(
    client,
    *,
    guild: discord.Guild,
    actor: discord.abc.User,
    offer_id: int,
) -> OfferApprovalResult:
    """
    Approve an accepted offer, creating the active contract.

    Raises `ApprovalError` when the offer is missing or not in an
    approvable state; in both cases nothing has been written.
    """
    async with db.connect() as conn:
        offer = await queries.fetch_offer_by_id(conn, offer_id)
        if offer is None:
            raise ApprovalError(f"No offer `{offer_id}`.")

        market_value = await queries.fetch_latest_published_valuation(
            conn, offer.driver_id
        )
        try:
            approval = await contracts_service.commissioner_approve(
                conn,
                offer_id,
                actor_id=actor.id,
                value_at_signing=market_value,
            )
        except contracts_service.TransitionError as exc:
            raise ApprovalError(str(exc)) from exc

        contract = await queries.fetch_contract_by_id(conn, approval.contract_id)
        assert contract is not None
        team = await queries.fetch_team_by_id(conn, contract.team_id)
        tier = await queries.fetch_tier_by_id(conn, contract.tier_id)
        driver_row = await conn.fetchrow(
            "SELECT member_id, display_name FROM drivers WHERE id = $1",
            contract.driver_id,
        )
        guild_config = await queries.fetch_guild_config(conn, guild.id)

    # Role assignment via the shared helper (same code path as
    # /roster sign, per CLAUDE.md §8 invariant).
    member = guild.get_member(driver_row["member_id"])
    role_warning: str | None = None
    if member is not None and team is not None:
        try:
            await roster_ops.sign_to_team(
                guild=guild,
                member=member,
                team=team,
                actor=actor,
                reason=f"Contract {approval.external_ref} approved",
            )
        except roster_ops.RoleAssignmentError as exc:
            role_warning = str(exc)

    # Public signed post to the transactions channel.
    if guild_config.transactions_channel_id and team is not None and tier is not None:
        channel = client.get_channel(guild_config.transactions_channel_id)
        if isinstance(channel, discord.TextChannel):
            try:
                await channel.send(
                    embed=contract_render.render_signed_contract_post(
                        team_name=team.name,
                        driver_name=driver_row["display_name"],
                        tier_label=tier.label,
                        contract_value=contract.contract_value,
                        signing_bonus=contract.signing_bonus,
                        term_seasons=contract.term_seasons,
                        contract_type=contract.contract_type,
                        value_at_signing=contract.value_at_signing,
                        external_ref=approval.external_ref,
                        approved_by_mention=actor.mention,
                    )
                )
            except discord.Forbidden:
                log.warning(
                    "No permission to post signed-contract to channel %s",
                    guild_config.transactions_channel_id,
                )

    return OfferApprovalResult(
        offer_id=offer_id,
        contract_id=approval.contract_id,
        external_ref=approval.external_ref,
        role_warning=role_warning,
    )


async def reject_offer(
    *, offer_id: int, actor_id: int, note: str | None = None
) -> None:
    async with db.connect() as conn:
        try:
            await contracts_service.commissioner_reject(
                conn, offer_id, actor_id=actor_id, note=note
            )
        except contracts_service.TransitionError as exc:
            raise ApprovalError(str(exc)) from exc


# ── Trades ───────────────────────────────────────────────────────────


async def approve_trade(
    *,
    guild: discord.Guild,
    actor: discord.abc.User,
    trade_id: int,
) -> TradeApprovalResult:
    """
    Approve an accepted trade, executing the contract transfers.

    Role swaps run after the transaction commits. A failed swap is
    collected into `role_warnings` rather than raised — the money side is
    authoritative and must not be rolled back over a role error.
    """
    async with db.connect() as conn:
        trade = await queries.fetch_trade_by_id(conn, trade_id)
        if trade is None:
            raise ApprovalError(f"No trade `{trade_id}`.")
        try:
            await contracts_service.commissioner_approve_trade(
                conn, trade_id, actor_id=actor.id
            )
        except contracts_service.TransitionError as exc:
            raise ApprovalError(str(exc)) from exc

        items = await queries.fetch_trade_items(conn, trade_id)
        # (member_id, old_team_id, new_team_id) for the role swaps below.
        role_ops: list[tuple[int, int, int]] = []
        for item in items:
            contract = await queries.fetch_contract_by_id(conn, item.contract_id)
            if contract is None:
                continue
            driver_row = await conn.fetchrow(
                "SELECT member_id FROM drivers WHERE id = $1", contract.driver_id
            )
            if driver_row is None:
                continue
            # contract.team_id was already updated by the approve call.
            role_ops.append((driver_row["member_id"], item.from_team_id, contract.team_id))

        teams_by_id: dict[int, object] = {}
        for _, old_id, new_id in role_ops:
            for tid in (old_id, new_id):
                if tid not in teams_by_id:
                    teams_by_id[tid] = await queries.fetch_team_by_id(conn, tid)

    role_warnings: list[str] = []
    for member_id, old_team_id, new_team_id in role_ops:
        member = guild.get_member(member_id)
        if member is None:
            role_warnings.append(f"member {member_id} not in guild — role not swapped")
            continue
        old_team = teams_by_id.get(old_team_id)
        new_team = teams_by_id.get(new_team_id)
        try:
            if old_team is not None:
                await roster_ops.drop_from_team(
                    guild=guild,
                    member=member,
                    team=old_team,
                    actor=actor,
                    reason=f"Trade {trade_id} — leaving {old_team.name}",
                )
            if new_team is not None:
                await roster_ops.sign_to_team(
                    guild=guild,
                    member=member,
                    team=new_team,
                    actor=actor,
                    reason=f"Trade {trade_id} — joining {new_team.name}",
                )
        except roster_ops.RoleAssignmentError as exc:
            role_warnings.append(str(exc))

    return TradeApprovalResult(trade_id=trade_id, role_warnings=role_warnings)


async def reject_trade(
    *, trade_id: int, actor_id: int, note: str | None = None
) -> None:
    async with db.connect() as conn:
        try:
            await contracts_service.commissioner_reject_trade(
                conn, trade_id, actor_id=actor_id, note=note
            )
        except contracts_service.TransitionError as exc:
            raise ApprovalError(str(exc)) from exc


# ── Phase 8: season carry-over ───────────────────────────────────────


@dataclass(frozen=True)
class CarryOverResult:
    report: workflow.CarryOverReport
    role_warnings: list[str]


async def carry_over_season(
    *,
    guild: discord.Guild,
    actor: discord.abc.User,
    from_season_name: str,
) -> CarryOverResult:
    """
    Run the Discord-free carry-over, then strip team roles from every
    driver whose contract expired. Carried drivers keep their role —
    nothing changed for them. Role failures are warnings, never a
    rollback: the ledger is already the source of truth.
    """
    try:
        report = await workflow.carry_over_contracts(
            guild_id=guild.id,
            actor_id=actor.id,
            from_season_name=from_season_name,
        )
    except workflow.WorkflowError as exc:
        raise ApprovalError(str(exc)) from exc

    teams_by_id: dict[int, object] = {}
    async with db.connect() as conn:
        for _, team_id in report.outcome.expired_members:
            if team_id not in teams_by_id:
                teams_by_id[team_id] = await queries.fetch_team_by_id(conn, team_id)

    role_warnings: list[str] = []
    for member_id, team_id in report.outcome.expired_members:
        member = guild.get_member(member_id)
        team = teams_by_id.get(team_id)
        if member is None or team is None:
            role_warnings.append(f"member {member_id} not in guild — role not removed")
            continue
        try:
            await roster_ops.drop_from_team(
                guild=guild,
                member=member,
                team=team,
                actor=actor,
                reason=f"Contract expired at end of {report.from_season_name}",
            )
        except roster_ops.RoleAssignmentError as exc:
            role_warnings.append(str(exc))
    return CarryOverResult(report=report, role_warnings=role_warnings)
