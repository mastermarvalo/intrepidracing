"""
Command catalogue for `/help` and the panel's command browser.

Built by walking the live app-command tree rather than from a hand-kept
list. A static list would be accurate on the day it was written and
wrong within two phases; this cannot drift, because it is reading the
same objects Discord was handed.

Only the group blurbs and their ordering are curated here — that is
editorial (which groups matter most to a new admin), not factual.
"""

from __future__ import annotations

from dataclasses import dataclass

import discord
from discord import app_commands

# Discord platform limits.
_EMBED_FIELD_LIMIT = 1024
_MAX_SELECT_OPTIONS = 25
_MAX_FIELDS = 25

_COLOR = discord.Color.from_str("#1a3d6d")

_UNGROUPED = "general"


@dataclass(frozen=True)
class GroupBlurb:
    emoji: str
    label: str
    summary: str
    audience: str


# Curated: order is the order a new commissioner should meet them.
GROUP_BLURBS: dict[str, GroupBlurb] = {
    _UNGROUPED: GroupBlurb(
        "🏁", "Start here", "The guided panel and this help browser.", "Everyone"
    ),
    "market-admin": GroupBlurb(
        "⚙",
        "Commissioner",
        "Seasons, tiers, config, results import, valuations, boards, approvals.",
        "Manage Server",
    ),
    "roster": GroupBlurb(
        "📋", "Rosters", "Team roster embeds, signing and dropping, history.", "Manage Server / TP"
    ),
    "contract": GroupBlurb(
        "📝",
        "Contracts",
        "Offers, counters, acceptance, releases and buyouts.",
        "TPs and drivers",
    ),
    "trade": GroupBlurb("🔁", "Trades", "Propose, accept, decline and track trades.", "TPs"),
    "market": GroupBlurb(
        "💹", "Market", "Driver values, movers, cap sheets, P/L, dashboards.", "Everyone"
    ),
    "sheets": GroupBlurb(
        "📊", "Stat boards", "Live Google Sheets boards posted into channels.", "Manage Server"
    ),
    "league": GroupBlurb("🏁", "Control panel", "The guided front door.", "Everyone"),
}

# COMMAND_CATALOG is the set of keys `/help group:<x>` accepts. Kept as a
# module-level name so the cog can validate input without a tree.
COMMAND_CATALOG = GROUP_BLURBS


def _blurb(key: str) -> GroupBlurb:
    return GROUP_BLURBS.get(
        key, GroupBlurb("•", key, "", "")
    )


def collect_commands(
    tree: app_commands.CommandTree | None,
) -> dict[str, list[tuple[str, str]]]:
    """
    Walk the tree into `{group_key: [(full_name, description), ...]}`.

    Returns an empty mapping when handed no tree, so callers can render
    a help screen during tests or before the tree is populated.
    """
    out: dict[str, list[tuple[str, str]]] = {}
    if tree is None:
        return out

    for cmd in tree.walk_commands():
        if isinstance(cmd, app_commands.Group):
            continue
        parent = cmd.parent
        if parent is None:
            key = _UNGROUPED
            full = f"/{cmd.name}"
        else:
            root = parent
            while getattr(root, "parent", None) is not None:
                root = root.parent
            key = root.name
            full = f"/{cmd.qualified_name}"
        out.setdefault(key, []).append((full, cmd.description or ""))

    for cmds in out.values():
        cmds.sort(key=lambda pair: pair[0])
    return out


def _ordered_keys(found: dict[str, list[tuple[str, str]]]) -> list[str]:
    """Curated groups first in their curated order, then anything new."""
    known = [k for k in GROUP_BLURBS if k in found]
    extra = sorted(k for k in found if k not in GROUP_BLURBS)
    return known + extra


def help_category_options(
    tree: app_commands.CommandTree | None = None,
) -> list[discord.SelectOption]:
    """Select options for the group picker, capped at Discord's limit."""
    found = collect_commands(tree)
    keys = _ordered_keys(found) or list(GROUP_BLURBS)

    options: list[discord.SelectOption] = []
    for key in keys[:_MAX_SELECT_OPTIONS]:
        b = _blurb(key)
        count = len(found.get(key, []))
        options.append(
            discord.SelectOption(
                label=b.label,
                value=key,
                description=(f"{count} command(s) · {b.summary}")[:100],
                emoji=b.emoji,
            )
        )
    return options


def build_help_embed(
    key: str | None,
    tree: app_commands.CommandTree | None = None,
) -> discord.Embed:
    """
    Overview when `key` is None, otherwise one group in full.

    The overview leads with the panel, because for most admins the right
    answer is 'do not learn 62 commands, press /league'.
    """
    found = collect_commands(tree)

    if key is None:
        total = sum(len(v) for v in found.values())
        embed = discord.Embed(
            title="📖 Commands",
            description=(
                "**You do not need to learn these.** `/league` opens a guided panel "
                "that walks the common jobs — setup, race night, approvals.\n\n"
                "Every command below still works exactly as before. Pick a group to browse."
            ),
            color=_COLOR,
        )
        for k in _ordered_keys(found)[:_MAX_FIELDS]:
            b = _blurb(k)
            embed.add_field(
                name=f"{b.emoji} {b.label} · {len(found.get(k, []))}",
                value=f"{b.summary}\n*{b.audience}*" if b.audience else b.summary,
                inline=True,
            )
        if total:
            embed.set_footer(text=f"{total} commands across {len(found)} groups")
        return embed

    b = _blurb(key)
    cmds = found.get(key, [])
    embed = discord.Embed(
        title=f"{b.emoji} {b.label}",
        description=b.summary or None,
        color=_COLOR,
    )
    if b.audience:
        embed.add_field(name="Who can use these", value=b.audience, inline=False)

    if not cmds:
        embed.add_field(
            name="Commands",
            value="None registered in this server yet.",
            inline=False,
        )
        return embed

    # Pack into as few fields as possible; a field per command burns
    # through the 25-field cap on the larger groups.
    chunks: list[str] = []
    current = ""
    for name, desc in cmds:
        line = f"`{name}`\n{desc}\n" if desc else f"`{name}`\n"
        if len(current) + len(line) > _EMBED_FIELD_LIMIT:
            chunks.append(current)
            current = line
        else:
            current += line
    if current:
        chunks.append(current)

    for i, chunk in enumerate(chunks[: _MAX_FIELDS - 1]):
        embed.add_field(
            name="Commands" if i == 0 else "…continued",
            value=chunk,
            inline=False,
        )
    embed.set_footer(text=f"{len(cmds)} command(s) in this group")
    return embed
