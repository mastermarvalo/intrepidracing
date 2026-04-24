from typing import Protocol

import discord

from bot.models import SlotType, Team, TeamSlot


class MemberLike(Protocol):
    """Structural type for discord.Member — lets tests pass plain objects."""

    @property
    def id(self) -> int: ...

    @property
    def roles(self) -> list[discord.Role]: ...


def _slot_block(pool: list[MemberLike], slot: TeamSlot) -> str:
    """
    Build the text block for one slot: bold label followed by member mentions.

    Members within the seat count get plain mentions. Extras get *(overflow)*.
    Empty seats are padded with *Spot Open*.
    """
    assigned = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]

    lines = [f"**{slot.label}**"]
    for i, member in enumerate(assigned):
        if i < slot.quantity:
            lines.append(f"<@{member.id}>")
        else:
            lines.append(f"<@{member.id}> *(overflow)*")

    open_spots = max(0, slot.quantity - len(assigned))
    lines.extend(["*Spot Open*"] * open_spots)

    return "\n".join(lines)


def _section_text(
    pool: list[MemberLike],
    slots: list[TeamSlot],
    slot_type: SlotType,
) -> str:
    """Combine all slots of one type into a single text block, in sort_order."""
    typed = sorted(
        (s for s in slots if s.slot_type == slot_type),
        key=lambda s: s.sort_order,
    )
    return "\n".join(_slot_block(pool, slot) for slot in typed)


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

    staff_text = _section_text(pool, team.slots, "staff")
    driver_text = _section_text(pool, team.slots, "driver")

    # Each section is a single field: underlined bold header as the name,
    # all slots concatenated as the value. This avoids the blank-line gap
    # that a separate header field (with a zero-width-space value) creates.
    if staff_text:
        embed.add_field(name="__**Staff**__", value=staff_text, inline=False)

    if driver_text:
        embed.add_field(name="__**Drivers**__", value=driver_text, inline=False)

    return embed
