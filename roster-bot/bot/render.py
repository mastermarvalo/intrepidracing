from io import BytesIO
from typing import Literal, Protocol

import discord
from PIL import Image, ImageDraw

from bot.models import SlotType, Team, TeamSlot

# bytes cache so we only run Pillow once per unique color
_flair_cache: dict[int | None, bytes] = {}

_FLAIR_W, _FLAIR_H = 800, 48
_DIAMOND_SIZES = [10, 13, 16, 20, 16, 13, 10]


def _render_flair_bytes(color: int | None) -> bytes:
    rgb_int = color if color is not None else 0x5865F2  # blurple fallback
    r, g, b = (rgb_int >> 16) & 0xFF, (rgb_int >> 8) & 0xFF, rgb_int & 0xFF

    img = Image.new("RGBA", (_FLAIR_W, _FLAIR_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    n = len(_DIAMOND_SIZES)
    spacing = _FLAIR_W // (n + 1)
    cy = _FLAIR_H // 2

    for i, s in enumerate(_DIAMOND_SIZES):
        cx = spacing * (i + 1)
        draw.polygon([(cx, cy - s), (cx + s, cy), (cx, cy + s), (cx - s, cy)], fill=(r, g, b, 255))

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build_flair_file(color: int | None) -> discord.File:
    if color not in _flair_cache:
        _flair_cache[color] = _render_flair_bytes(color)
    return discord.File(BytesIO(_flair_cache[color]), filename="flair.png")


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
    pool = [m for m in members if any(r.id == team.team_role_id for r in m.roles)]

    embed = discord.Embed(
        color=discord.Color(team.color) if team.color is not None else discord.Color.blurple()
    )

    if team.logo_url:
        embed.set_thumbnail(url=team.logo_url)

    sections: list[str] = [f"# __{team.name}__"]

    if team.tagline:
        sections.append(team.tagline)

    staff_text = _section_text(pool, team.slots, "staff")
    driver_text = _section_text(pool, team.slots, "driver")

    if staff_text:
        sections.append(f"## __Staff__\n{staff_text}")
    if driver_text:
        sections.append(f"## __Drivers__\n{driver_text}")

    embed.description = "\n\n".join(sections)
    embed.set_image(url="attachment://flair.png")

    return embed



def _tier_sort_key(role: discord.Role) -> int:
    parts = role.name.strip().split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 999


def build_fa_embed(guild: discord.Guild, fa_role_id: int) -> discord.Embed:
    """Build the free agents embed for a guild."""
    fa_role = guild.get_role(fa_role_id)
    if fa_role is None:
        return discord.Embed(
            title="Free Agents",
            description="The configured free agent role no longer exists.",
            color=discord.Color.red(),
        )

    tier_roles = {
        r for r in guild.roles
        if r.name.strip().lower().startswith("tier ")
        and "reserve" not in r.name.strip().lower()
    }

    by_tier: dict[discord.Role, list[discord.Member]] = {}
    for member in guild.members:
        if fa_role not in member.roles:
            continue
        for role in member.roles:
            if role in tier_roles:
                by_tier.setdefault(role, []).append(member)

    embed = discord.Embed(title="Free Agents", color=discord.Color.green())

    if not by_tier:
        embed.description = "No free agents found."
        return embed

    for role in sorted(by_tier, key=_tier_sort_key):
        mentions = " ".join(m.mention for m in by_tier[role])
        embed.add_field(name=role.name, value=mentions, inline=False)

    return embed


def build_transaction_embed(
    team: Team,
    member: discord.Member,
    action: Literal["signed", "dropped"],
) -> discord.Embed:
    verb = "Signed" if action == "signed" else "Dropped"
    prep = "to" if action == "signed" else "from"
    color = discord.Color.green() if action == "signed" else discord.Color.red()

    if team.message_id:
        roster_url = (
            f"https://discord.com/channels/{team.guild_id}/{team.channel_id}/{team.message_id}"
        )
        team_ref = f"[{team.name}]({roster_url})"
    else:
        team_ref = team.name

    embed = discord.Embed(
        description=f"**{member.display_name}** has been **{action}** {prep} **{team_ref}**",
        color=color,
    )
    embed.set_author(name=f"{verb}: {member.display_name}", icon_url=member.display_avatar.url)

    if team.logo_url:
        embed.set_thumbnail(url=team.logo_url)

    return embed
