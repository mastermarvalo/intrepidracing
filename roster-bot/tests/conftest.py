"""
Shared fixtures for render tests.

FakeRole and FakeMember satisfy the MemberLike protocol without touching discord.py
internals. They're plain dataclasses — no mocking framework needed.
"""

from dataclasses import dataclass, field

import pytest

from bot.models import Team, TeamSlot


@dataclass
class FakeRole:
    id: int


@dataclass
class FakeAvatar:
    url: str = "https://cdn.discordapp.com/embed/avatars/0.png"


@dataclass
class FakeMember:
    id: int
    roles: list[FakeRole] = field(default_factory=list)
    display_name: str = "TestMember"
    display_avatar: FakeAvatar = field(default_factory=FakeAvatar)


def make_team(
    *,
    team_role_id: int = 100,
    slots: list[TeamSlot] | None = None,
    tagline: str | None = None,
    logo_url: str | None = None,
) -> Team:
    return Team(
        id=1,
        guild_id=999,
        key="testteam",
        name="Test Team",
        team_role_id=team_role_id,
        channel_id=1,
        tagline=tagline,
        logo_url=logo_url,
        slots=slots or [],
    )


def make_slot(
    *,
    slot_role_id: int,
    label: str = "Slot",
    quantity: int = 2,
    slot_type: str = "driver",
    sort_order: int = 0,
) -> TeamSlot:
    return TeamSlot(
        id=1,
        team_id=1,
        slot_role_id=slot_role_id,
        label=label,
        quantity=quantity,
        slot_type=slot_type,  # type: ignore[arg-type]
        sort_order=sort_order,
    )
