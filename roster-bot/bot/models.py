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
    principal_role_id: int | None = None
    message_id: int | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    slots: list[TeamSlot] = field(default_factory=list)
