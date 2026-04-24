from typing import Protocol

import discord

from bot.models import SlotType, Team, TeamSlot


class MemberLike(Protocol):
    """Structural type for discord.Member — lets tests pass plain objects."""

    @property
    def id(self) -> int: ...

    @property
    def roles(self) -> list[discord.Role]: ...


def _slot_value(pool: list[MemberLike], slot: TeamSlot) -> str:
    """
    Build the field value for one slot.

    Members at indices < quantity get plain mentions. Members beyond quantity
    get an *(overflow)* tag — they're on the team role but exceed the seat count.
    Empty seats below quantity are padded with *Spot Open*.
    """
    assigned = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]

    lines: list[str] = []
    for i, member in enumerate(assigned):
        if i < slot.quantity:
            lines.append(f"<@{member.id}>")
        else:
            lines.append(f"<@{member.id}> *(overflow)*")

    open_spots = max(0, slot.quantity - len(assigned))
    lines.extend(["*Spot Open*"] * open_spots)

    return "\n".join(lines) if lines else "*Spot Open*"


def _section_fields(
    pool: list[MemberLike],
    slots: list[TeamSlot],
    slot_type: SlotType,
) -> list[tuple[str, str]]:
    """Return (label, value) pairs for all slots of the given type, in sort_order."""
    typed = sorted(
        (s for s in slots if s.slot_type == slot_type),
        key=lambda s: s.sort_order,
    )
    return [(slot.label, _slot_value(pool, slot)) for slot in typed]


def build_embed(team: Team, members: list[MemberLike]) -> discord.Embed:
    """
    Build the roster embed for *team* against current guild members.

    Filters the full member list to those who hold team_role_id, then renders
    each slot against that pool.
    """
    pool = [m for m in members if any(r.id == team.team_role_id for r in m.roles)]

    embed = discord.Embed(title=team.name, color=discord.Color.blurple())

    if team.tagline:
        embed.description = team.tagline

    if team.logo_url:
        embed.set_thumbnail(url=team.logo_url)

    staff_fields = _section_fields(pool, team.slots, "staff")
    driver_fields = _section_fields(pool, team.slots, "driver")

    # Discord field names are bold by default; no extra markdown needed.
    # ​ (zero-width space) satisfies Discord's "value must be non-empty" rule
    # while keeping the header visually clean.
    if staff_fields:
        embed.add_field(name="Staff", value="​", inline=False)
        for name, value in staff_fields:
            embed.add_field(name=name, value=value, inline=False)

    if driver_fields:
        embed.add_field(name="Drivers", value="​", inline=False)
        for name, value in driver_fields:
            embed.add_field(name=name, value=value, inline=False)

    return embed
