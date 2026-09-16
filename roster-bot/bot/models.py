from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

SlotType = Literal["staff", "driver"]


@dataclass
class TeamSlot:
    id: int
    team_id: int
    slot_role_id: int
    label: str
    quantity: int
    slot_type: SlotType
    sort_order: int


@dataclass
class GuildConfig:
    guild_id: int
    free_agent_role_id: int | None = None
    fa_channel_id: int | None = None
    fa_message_id: int | None = None
    transactions_channel_id: int | None = None


@dataclass
class Team:
    id: int
    guild_id: int
    key: str
    name: str
    team_role_id: int
    channel_id: int
    tagline: str | None = None
    logo_url: str | None = None
    banner_url: str | None = None
    principal_role_id: int | None = None
    color: int | None = None
    message_id: int | None = None
    info_label: str | None = None
    info_body: str | None = None
    dark_mode: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    slots: list[TeamSlot] = field(default_factory=list)


@dataclass
class StatBoard:
    id: int
    guild_id: int
    title: str
    sheet_id: str
    sheet_range: str
    channel_id: int
    message_id: int | None = None
    forum_thread_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


# ── Market & contracts (Phase 1: seasons, tiers, drivers, league config) ──


@dataclass
class Season:
    id: int
    guild_id: int
    name: str
    is_active: bool = False
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Tier:
    id: int
    season_id: int
    code: str
    label: str
    rank_order: int
    tier_role_id: int | None = None
    accent_color: int | None = None


@dataclass
class Driver:
    id: int
    season_id: int
    tier_id: int
    member_id: int
    display_name: str
    status: str
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Contract:
    id: int
    season_id: int
    tier_id: int
    driver_id: int
    team_id: int
    contract_value: Decimal
    term_seasons: int
    contract_type: str
    state: str
    signing_bonus: Decimal = Decimal("0")
    max_incentives: Decimal = Decimal("0")
    value_at_signing: Decimal | None = None
    signed_at: datetime | None = None
    expires_after: int | None = None
    voided_at: datetime | None = None
    approved_by: int | None = None
    external_ref: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    # Phase 8: one row per season served. season_index is 1-based within
    # term_seasons; carried_from links to the previous season's row and
    # origin to the originally signed row (both None on a fresh signing).
    season_index: int = 1
    carried_from_contract_id: int | None = None
    origin_contract_id: int | None = None
    # Phase 9: terms are counted in RACES, not seasons. `term_races` is
    # the whole deal's length; `races_served_before` is how many of them
    # earlier rows in the chain already served, so a carried row knows
    # where in the term it starts without re-walking the chain.
    # `contracts.term_races` is NOT NULL: migration 015 backfilled every
    # existing row as `term_seasons × races_per_season`, and inserts
    # derive it the same way when a caller omits it. The Optional here is
    # only so a caller can say "derive it for me"; a Contract loaded from
    # the database always carries a real term.
    term_races: int | None = None
    races_served_before: int = 0

    @property
    def seasons_remaining_after_this(self) -> int:
        """How many further seasons the deal runs after this row's season."""
        return max(self.term_seasons - self.season_index, 0)


@dataclass
class ContractOffer:
    id: int
    season_id: int
    tier_id: int
    driver_id: int
    team_id: int
    offered_by: int
    offer_kind: str
    salary: Decimal
    term_seasons: int
    contract_type: str
    state: str
    expires_at: datetime
    validation: dict
    signing_bonus: Decimal = Decimal("0")
    incentives: str | None = None
    message: str | None = None
    parent_offer_id: int | None = None
    thread_id: int | None = None
    resolved_at: datetime | None = None
    resolved_by: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LedgerEntry:
    id: int
    season_id: int
    tier_id: int
    kind: str
    detail: dict
    driver_id: int | None = None
    team_id: int | None = None
    contract_id: int | None = None
    offer_id: int | None = None
    amount: Decimal | None = None
    actor_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class Trade:
    id: int
    season_id: int
    proposing_team_id: int
    other_team_id: int
    proposed_by: int
    state: str
    expires_at: datetime
    message: str | None = None
    resolved_at: datetime | None = None
    resolved_by: int | None = None
    thread_id: int | None = None
    approved_ref: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class TradeItem:
    trade_id: int
    from_team_id: int
    contract_id: int


@dataclass
class DeadMoneyEntry:
    id: int
    season_id: int
    tier_id: int
    team_id: int
    amount: Decimal
    source_contract_id: int | None = None
    note: str | None = None
    actor_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class MarketBoard:
    id: int
    season_id: int
    kind: str
    channel_id: int
    tier_id: int | None = None
    message_id: int | None = None
    page: int = 0
    forum_thread_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class LeagueConfig:
    """
    Resolved config for a (season, tier). If a tier-scoped row exists it is
    returned as-is; otherwise the season-default row (tier_id IS NULL) fills
    the gap. See bot/market/config.py (added in Phase 2) for the resolver.
    """

    id: int
    season_id: int
    salary_cap: Decimal
    min_salary: Decimal
    active_driver_slots: int
    weekly_move_cap: Decimal
    exceptional_move_cap: Decimal
    min_term_seasons: int
    max_term_seasons: int
    max_incentive_pct: Decimal
    offer_ttl_hours: int
    tier_id: int | None = None
    # ── race-denominated terms and contract premiums (Phase 9)
    # The season bounds above are kept and still enforced; these run
    # alongside. `races_per_season` converts a season rate to the
    # per-race charge and is what the season bounds were backfilled with.
    # Both premium rates default to zero, which reproduces exactly the
    # pricing behaviour of every season before migration 015.
    races_per_season: int = 24
    min_term_races: int = 1
    max_term_races: int = 24
    resign_premium_pct: Decimal = Decimal("0")
    length_premium_pct: Decimal = Decimal("0")
    max_salary: Decimal | None = None
    free_agency_open: bool = False
    market_channel_id: int | None = None
    transactions_channel_id: int | None = None
    approvals_channel_id: int | None = None
    commissioner_role_id: int | None = None


@dataclass
class BudgetEntry:
    """One append-only row of `team_budget_ledger`. `amount` is signed."""

    id: int
    season_id: int
    team_id: int
    kind: str
    amount: Decimal
    race_result_id: int | None = None
    round_id: int | None = None
    from_season_id: int | None = None
    note: str | None = None
    detail: dict = field(default_factory=dict)
    is_correction: bool = False
    actor_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class DriverEarning:
    """
    One append-only row of `driver_earnings_ledger`. `amount` is signed.

    A driver-side tally only: no row here moves a team's cash. Identity
    is `(guild_id, member_id)` rather than `driver_id`, so a total
    survives a change of tier and carries into the next season by
    itself. The season/tier/contract/round links are provenance and may
    be NULL on an old row whose season was deleted.
    """

    id: int
    guild_id: int
    member_id: int
    kind: str
    amount: Decimal
    season_id: int | None = None
    tier_id: int | None = None
    driver_id: int | None = None
    contract_id: int | None = None
    round_id: int | None = None
    note: str | None = None
    actor_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class CareerEarnings:
    """
    One driver's lifetime earnings, as the leaderboard reads them.

    `display_name` is the most recent name the league recorded for this
    member, and is None for a member with no `drivers` row left at all
    (every season they raced in has since been deleted). Callers render
    the member id in that case rather than dropping the row: the money
    was still earned.
    """

    member_id: int
    total: Decimal
    display_name: str | None = None
    races_paid: int = 0
    seasons_paid: int = 0
