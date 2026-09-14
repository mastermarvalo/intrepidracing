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
    max_term_seasons: int
    max_incentive_pct: Decimal
    offer_ttl_hours: int
    tier_id: int | None = None
    max_salary: Decimal | None = None
    free_agency_open: bool = False
    market_channel_id: int | None = None
    transactions_channel_id: int | None = None
    approvals_channel_id: int | None = None
    commissioner_role_id: int | None = None
