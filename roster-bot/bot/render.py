import asyncio
import math
import os
from io import BytesIO
from typing import Literal, Protocol

import discord
from PIL import Image, ImageDraw, ImageFont

from bot.models import SlotType, Team, TeamSlot

_FONT_PATH = os.path.join(os.path.dirname(__file__), "assets", "Formula1-Bold_web_0.ttf")

# bytes cache keyed by (color, team_name, dark_mode)
_flair_cache: dict[tuple[int | None, str, bool], bytes] = {}

# avatar image bytes keyed by CDN URL (auto-invalidates when user changes avatar)
_avatar_cache: dict[str, bytes] = {}

_FLAIR_W = 960
_FLAIR_H = 90


def _render_flair_bytes(color: int | None, team_name: str, *, dark_mode: bool = False) -> bytes:
    rgb_int = color if color is not None else 0x5865F2  # blurple fallback
    r = (rgb_int >> 16) & 0xFF
    g = (rgb_int >> 8) & 0xFF
    b = rgb_int & 0xFF
    if dark_mode:
        r, g, b = int(r * 0.4), int(g * 0.4), int(b * 0.4)

    img = Image.new("RGBA", (_FLAIR_W, _FLAIR_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    draw.rectangle([(0, 0), (_FLAIR_W - 1, _FLAIR_H - 1)], fill=(r, g, b, 255))

    font_size = 48
    font: ImageFont.ImageFont | ImageFont.FreeTypeFont = ImageFont.load_default()
    while font_size >= 10:
        try:
            f = ImageFont.truetype(_FONT_PATH, font_size)
            bbox = draw.textbbox((0, 0), team_name, font=f)
            if bbox[2] - bbox[0] <= _FLAIR_W - 48:
                font = f
                break
        except OSError:
            break
        font_size -= 2

    bbox = draw.textbbox((0, 0), team_name, font=font)
    tx = (_FLAIR_W - (bbox[2] - bbox[0])) // 2
    ty = (_FLAIR_H - (bbox[3] - bbox[1])) // 2 - bbox[1]
    draw.text((tx, ty), team_name, font=font, fill=(255, 255, 255, 255))

    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def build_flair_file(color: int | None, team_name: str, *, dark_mode: bool = False) -> discord.File:
    key = (color, team_name, dark_mode)
    if key not in _flair_cache:
        _flair_cache[key] = _render_flair_bytes(color, team_name, dark_mode=dark_mode)
    return discord.File(BytesIO(_flair_cache[key]), filename="flair.png")


class _AvatarLike(Protocol):
    @property
    def url(self) -> str: ...


class MemberLike(Protocol):
    """Structural type for discord.Member — lets tests pass plain objects."""

    @property
    def id(self) -> int: ...

    @property
    def roles(self) -> list[discord.Role]: ...

    @property
    def display_name(self) -> str: ...

    @property
    def display_avatar(self) -> _AvatarLike: ...


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
        color=discord.Color(team.color) if team.color is not None else discord.Color.blurple(),
    )

    sections: list[str] = []

    if team.tagline:
        sections.append(f"## {team.tagline}")

    staff_text = _section_text(pool, team.slots, "staff")
    driver_text = _section_text(pool, team.slots, "driver")

    if staff_text:
        sections.append(f"## __Staff__\n{staff_text}")
    if driver_text:
        sections.append(f"## __Drivers__\n{driver_text}")
    if getattr(team, "info_label", None) and getattr(team, "info_body", None):
        sections.append(f"## __{team.info_label}__\n{team.info_body}")

    embed.description = "\n\n".join(sections) if sections else None

    # Logo always in thumbnail (top-right).
    # Banner goes to set_image (bottom) when present.
    # Flair is sent as a separate first embed (see build_flair_embed), so no
    # set_image fallback is needed here.
    if team.logo_url:
        embed.set_thumbnail(url=team.logo_url)
    if team.banner_url:
        embed.set_image(url=team.banner_url)

    return embed


def build_roster_embeds(team: Team, members: list[MemberLike]) -> list[discord.Embed]:
    """Return [info_embed, player_embed, …] for a roster message.

    info_embed  — tagline, info box, logo thumbnail, banner image.
    player_embeds — one per filled slot entry, set_author with avatar + name;
                    one per open spot, plain text.  Max 8 player embeds total
                    (keeps the message within Discord's 10-embed limit when
                    combined with the flair card).
    """
    pool = [m for m in members if any(r.id == team.team_role_id for r in m.roles)]
    color = discord.Color(team.color) if team.color is not None else discord.Color.blurple()

    # Info embed — header content only, no player mentions
    info = discord.Embed(color=color)
    parts: list[str] = []
    if team.tagline:
        parts.append(f"## {team.tagline}")
    if getattr(team, "info_label", None) and getattr(team, "info_body", None):
        parts.append(f"## __{team.info_label}__\n{team.info_body}")
    info.description = "\n\n".join(parts) if parts else None
    if team.logo_url:
        info.set_thumbnail(url=team.logo_url)
    if team.banner_url:
        info.set_image(url=team.banner_url)

    # Per-player embeds in slot sort order
    MAX = 8
    player_embeds: list[discord.Embed] = []
    overflow = 0

    for slot in sorted(team.slots, key=lambda s: s.sort_order):
        slot_members = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]
        for member in slot_members:
            if len(player_embeds) < MAX:
                e = discord.Embed(color=color)
                e.set_author(name=member.display_name, icon_url=member.display_avatar.url)
                e.description = slot.label
                player_embeds.append(e)
            else:
                overflow += 1
        for _ in range(max(0, slot.quantity - len(slot_members))):
            if len(player_embeds) < MAX:
                e = discord.Embed(color=discord.Color(0x2C2F33))
                e.description = f"*Spot Open* — {slot.label}"
                player_embeds.append(e)
            else:
                overflow += 1

    if overflow:
        suffix = f"\n*… and {overflow} more*"
        info.description = (info.description or "") + suffix

    return [info] + player_embeds


def build_flair_embed(color: int | None) -> discord.Embed:
    """A standalone embed whose only content is the flair image.

    Sent as the *first* embed in every roster message so the title-card
    appears at the very top, above the roster content embed.
    """
    e = discord.Embed(
        color=discord.Color(color) if color is not None else discord.Color.blurple()
    )
    e.set_image(url="attachment://flair.png")
    return e


def roster_flair_file(team: Team) -> discord.File:
    """The generated flair/title-card file — always included in roster messages."""
    return build_flair_file(team.color, team.name, dark_mode=team.dark_mode)


async def _fetch_avatar_bytes(member: discord.Member) -> bytes | None:
    url = str(member.display_avatar.replace(size=64, format="png"))
    if url in _avatar_cache:
        return _avatar_cache[url]
    try:
        data = await member.display_avatar.replace(size=64, format="png").read()
        _avatar_cache[url] = data
        return data
    except Exception:
        return None


async def build_avatar_card(team: Team, members: list[discord.Member]) -> discord.File | None:
    pool = [m for m in members if any(r.id == team.team_role_id for r in m.roles)]
    if not pool:
        return None

    results = await asyncio.gather(*(_fetch_avatar_bytes(m) for m in pool))
    av_bytes: dict[int, bytes | None] = {m.id: data for m, data in zip(pool, results)}

    # Organise by slot then any remainder not in a slot
    sections: list[tuple[str, list[discord.Member]]] = []
    seen: set[int] = set()
    for slot in sorted(team.slots, key=lambda s: s.sort_order):
        mems = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]
        if mems:
            sections.append((slot.label, mems))
            seen.update(m.id for m in mems)
    rest = [m for m in pool if m.id not in seen]
    if rest:
        sections.append(("Members", rest))

    if not sections:
        return None

    # Layout
    W        = 960
    AVATAR_D = 64
    H_GAP    = 16
    PAD_X    = 20
    PAD_Y    = 16
    LABEL_H  = 26
    LABEL_MB = 8
    NAME_H   = 18
    ROW_STRIDE = AVATAR_D + 4 + NAME_H + 6
    SEC_GAP  = 18
    per_row  = (W - 2 * PAD_X + H_GAP) // (AVATAR_D + H_GAP)

    total_h = PAD_Y
    for i, (_, mems) in enumerate(sections):
        if i:
            total_h += SEC_GAP
        total_h += LABEL_H + LABEL_MB + math.ceil(len(mems) / per_row) * ROW_STRIDE
    total_h += PAD_Y

    rgb_int = team.color if team.color is not None else 0x5865F2
    bg = (
        int(((rgb_int >> 16) & 0xFF) * 0.12),
        int(((rgb_int >> 8)  & 0xFF) * 0.12),
        int((rgb_int         & 0xFF) * 0.12),
        255,
    )
    img  = Image.new("RGBA", (W, total_h), bg)
    draw = ImageDraw.Draw(img)

    try:
        label_font = ImageFont.truetype(_FONT_PATH, 16)
        name_font  = ImageFont.truetype(_FONT_PATH, 11)
    except OSError:
        label_font = name_font = ImageFont.load_default()

    circle_mask = Image.new("L", (AVATAR_D, AVATAR_D), 0)
    ImageDraw.Draw(circle_mask).ellipse((0, 0, AVATAR_D - 1, AVATAR_D - 1), fill=255)

    y = PAD_Y
    for i, (label, mems) in enumerate(sections):
        if i:
            y += SEC_GAP
        draw.text((PAD_X, y), label.upper(), font=label_font, fill=(255, 255, 255, 180))
        y += LABEL_H + LABEL_MB

        for row_start in range(0, len(mems), per_row):
            for col, member in enumerate(mems[row_start: row_start + per_row]):
                ax = PAD_X + col * (AVATAR_D + H_GAP)
                data = av_bytes.get(member.id)
                if data:
                    try:
                        av = Image.open(BytesIO(data)).convert("RGBA").resize(
                            (AVATAR_D, AVATAR_D), Image.LANCZOS
                        )
                        circle = Image.new("RGBA", (AVATAR_D, AVATAR_D), (0, 0, 0, 0))
                        circle.paste(av, mask=circle_mask)
                        img.alpha_composite(circle, (ax, y))
                    except Exception:
                        draw.ellipse((ax, y, ax + AVATAR_D - 1, y + AVATAR_D - 1), fill=(60, 60, 60, 255))
                else:
                    draw.ellipse((ax, y, ax + AVATAR_D - 1, y + AVATAR_D - 1), fill=(60, 60, 60, 255))

                name = member.display_name[:13] + "…" if len(member.display_name) > 14 else member.display_name
                bbox = draw.textbbox((0, 0), name, font=name_font)
                nx = ax + (AVATAR_D - (bbox[2] - bbox[0])) // 2
                draw.text((nx, y + AVATAR_D + 4), name, font=name_font, fill=(204, 204, 204, 255))
            y += ROW_STRIDE

    buf = BytesIO()
    img.save(buf, "PNG")
    buf.seek(0)
    return discord.File(buf, filename="avatars.png")


def build_avatar_embed(color: int | None) -> discord.Embed:
    e = discord.Embed(
        color=discord.Color(color) if color is not None else discord.Color.blurple()
    )
    e.set_image(url="attachment://avatars.png")
    return e


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
