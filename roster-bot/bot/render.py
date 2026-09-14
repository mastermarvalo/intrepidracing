import asyncio
import math
import os
from io import BytesIO
from typing import Literal, Protocol

import aiohttp
import discord
from PIL import Image, ImageDraw, ImageFont

from bot.models import SlotType, Team, TeamSlot

_FONT_PATH     = os.path.join(os.path.dirname(__file__), "assets", "Formula1-Bold_web_0.ttf")
_BOT_LOGO_PATH = os.path.join(os.path.dirname(__file__), "assets", "bot_logo.png")

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
    Build the text block for one slot: bold label followed by member names.

    Members within the seat count get plain names. Extras get *(overflow)*.
    Empty seats are padded with *Spot Open*.
    """
    assigned = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]

    lines = [f"**{slot.label}**"]
    for i, member in enumerate(assigned):
        if i < slot.quantity:
            lines.append(member.display_name)
        else:
            lines.append(f"{member.display_name} *(overflow)*")

    open_spots = max(0, slot.quantity - len(assigned))
    lines.extend(["-# Spot Open"] * open_spots)

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
    return [build_embed(team, members)]


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


async def _fetch_url_bytes(url: str) -> bytes | None:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status == 200:
                    return await resp.read()
    except Exception:
        pass
    return None


async def _fetch_avatar_bytes(member: discord.Member) -> bytes | None:
    url = str(member.display_avatar.replace(size=128, format="png"))
    if url in _avatar_cache:
        return _avatar_cache[url]
    try:
        data = await member.display_avatar.replace(size=128, format="png").read()
        _avatar_cache[url] = data
        return data
    except Exception:
        return None


async def build_avatar_card(
    team: Team,
    members: list[discord.Member],
    *,
    slot_filter: str | None = None,
) -> discord.File | None:
    pool = [m for m in members if any(r.id == team.team_role_id for r in m.roles)]
    if not pool:
        return None

    async def _none() -> None:
        return None

    avatar_tasks = [_fetch_avatar_bytes(m) for m in pool]
    logo_task = _fetch_url_bytes(team.logo_url) if team.logo_url else _none()
    *av_results, logo_bytes = await asyncio.gather(*avatar_tasks, logo_task)
    av_bytes: dict[int, bytes | None] = {m.id: data for m, data in zip(pool, av_results)}

    # Organise by slot, then any remainder not assigned to a slot
    sections: list[tuple[str, list[discord.Member]]] = []
    seen: set[int] = set()
    for slot in sorted(team.slots, key=lambda s: s.sort_order):
        if slot_filter and slot.label.lower() != slot_filter.lower():
            continue
        mems = [m for m in pool if any(r.id == slot.slot_role_id for r in m.roles)]
        if mems:
            sections.append((slot.label, mems))
            seen.update(m.id for m in mems)
    if not slot_filter:
        rest = [m for m in pool if m.id not in seen]
        if rest:
            sections.append(("Members", rest))

    if not sections:
        return None

    # ── layout constants ──────────────────────────────────────────────────────
    W        = 960
    AVATAR_D = 80
    CELL_W   = 110   # name zone; avatar is centered within it
    CELL_GAP = 8
    PAD_X    = 32
    LABEL_H  = 38
    LABEL_MB = 14
    NAME_GAP = 6
    NAME_H   = 20
    ROW_H    = AVATAR_D + NAME_GAP + NAME_H
    ROW_GAP  = 14
    SEC_GAP  = 28

    PAD_Y = 20

    per_row = max(1, (W - 2 * PAD_X + CELL_GAP) // (CELL_W + CELL_GAP))

    # Compute total canvas height
    total_h = PAD_Y
    for i, (_, mems) in enumerate(sections):
        if i:
            total_h += SEC_GAP
        n_rows = math.ceil(len(mems) / per_row)
        total_h += LABEL_H + LABEL_MB + n_rows * ROW_H + max(0, n_rows - 1) * ROW_GAP
    total_h += 20

    # Background — team color tinted dark; floor prevents near-black on dark teams
    rgb_int = team.color if team.color is not None else 0x5865F2
    bg = (
        max(int(((rgb_int >> 16) & 0xFF) * 0.15), 20),
        max(int(((rgb_int >>  8) & 0xFF) * 0.15), 20),
        max(int((rgb_int         & 0xFF) * 0.15), 20),
        255,
    )
    img  = Image.new("RGBA", (W, total_h), bg)
    draw = ImageDraw.Draw(img)

    # ── corner logos: bot logo always top-left, team logo beside it if present ─
    LOGO_SIZE = 48
    LOGO_PAD  = 8
    box_size  = LOGO_SIZE + LOGO_PAD * 2
    corner_x  = LOGO_PAD

    def _corner_box(image_data: bytes | None, path: str | None = None) -> Image.Image | None:
        try:
            if image_data:
                src = Image.open(BytesIO(image_data)).convert("RGBA")
            elif path:
                src = Image.open(path).convert("RGBA")
            else:
                return None
            src = src.resize((LOGO_SIZE, LOGO_SIZE), Image.LANCZOS)
            box = Image.new("RGBA", (box_size, box_size), (0, 0, 0, 200))
            box.alpha_composite(src, (LOGO_PAD, LOGO_PAD))
            return box
        except Exception:
            return None

    bot_box  = _corner_box(None, _BOT_LOGO_PATH)
    team_box = _corner_box(logo_bytes)

    for box in filter(None, [bot_box, team_box]):
        img.alpha_composite(box, (corner_x, LOGO_PAD))
        corner_x += box_size + 4

    _FontT = ImageFont.FreeTypeFont | ImageFont.ImageFont
    try:
        label_font: _FontT = ImageFont.truetype(_FONT_PATH, 24)
    except OSError:
        label_font = ImageFont.load_default()

    def _fit_name(text: str) -> tuple[_FontT, str]:
        for size in (11, 10, 9, 8, 7):
            try:
                f: _FontT = ImageFont.truetype(_FONT_PATH, size)
            except OSError:
                f = ImageFont.load_default()
            if draw.textbbox((0, 0), text, font=f)[2] <= CELL_W:
                return f, text
        try:
            f = ImageFont.truetype(_FONT_PATH, 7)
        except OSError:
            f = ImageFont.load_default()
        t = text
        while len(t) > 1:
            t = t[:-1]
            if draw.textbbox((0, 0), t + "…", font=f)[2] <= CELL_W:
                return f, t + "…"
        return f, t

    circle_mask = Image.new("L", (AVATAR_D, AVATAR_D), 0)
    ImageDraw.Draw(circle_mask).ellipse((0, 0, AVATAR_D - 1, AVATAR_D - 1), fill=255)

    y = PAD_Y
    for i, (label, mems) in enumerate(sections):
        if i:
            y += SEC_GAP

        lbbox = draw.textbbox((0, 0), label.upper(), font=label_font)
        lx = (W - (lbbox[2] - lbbox[0])) // 2
        draw.text((lx, y), label.upper(), font=label_font, fill=(255, 255, 255, 220))
        y += LABEL_H + LABEL_MB

        for row_start in range(0, len(mems), per_row):
            row = mems[row_start: row_start + per_row]

            row_w = len(row) * CELL_W + (len(row) - 1) * CELL_GAP
            row_x = (W - row_w) // 2

            for col, member in enumerate(row):
                cell_x = row_x + col * (CELL_W + CELL_GAP)
                av_x   = cell_x + (CELL_W - AVATAR_D) // 2

                data = av_bytes.get(member.id)
                if data:
                    try:
                        av = Image.open(BytesIO(data)).convert("RGBA").resize(
                            (AVATAR_D, AVATAR_D), Image.LANCZOS
                        )
                        circle = Image.new("RGBA", (AVATAR_D, AVATAR_D), (0, 0, 0, 0))
                        circle.paste(av, mask=circle_mask)
                        img.alpha_composite(circle, (av_x, y))
                    except Exception:
                        draw.ellipse(
                            (av_x, y, av_x + AVATAR_D - 1, y + AVATAR_D - 1),
                            fill=(60, 60, 60, 255),
                        )
                else:
                    draw.ellipse(
                        (av_x, y, av_x + AVATAR_D - 1, y + AVATAR_D - 1),
                        fill=(60, 60, 60, 255),
                    )

                name_font, name = _fit_name(member.display_name)
                nbbox = draw.textbbox((0, 0), name, font=name_font)
                nx = cell_x + (CELL_W - (nbbox[2] - nbbox[0])) // 2
                draw.text(
                    (nx, y + AVATAR_D + NAME_GAP),
                    name,
                    font=name_font,
                    fill=(210, 210, 210, 255),
                )

            is_last_row = row_start + per_row >= len(mems)
            y += ROW_H + (0 if is_last_row else ROW_GAP)

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
        names = "\n".join(m.display_name for m in by_tier[role])
        embed.add_field(name=role.name, value=names, inline=False)

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
