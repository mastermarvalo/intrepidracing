from dataclasses import dataclass, field
from datetime import UTC, datetime
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
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
