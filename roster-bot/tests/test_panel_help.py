"""
The help browser reads the live command tree, so these tests guard the
thing that would actually break: a group silently vanishing from /help,
or an embed exceeding a Discord limit and failing to send at runtime.
"""

import discord
import pytest
from discord import app_commands

from bot.panel_help import (
    GROUP_BLURBS,
    build_help_embed,
    collect_commands,
    help_category_options,
)

# Discord's own limits — an embed breaching these raises at send time.
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FIELD_NAME_LIMIT = 256
EMBED_TOTAL_LIMIT = 6000
MAX_FIELDS = 25
MAX_SELECT_OPTIONS = 25
SELECT_DESC_LIMIT = 100


class _FakeTree:
    """Stands in for app_commands.CommandTree with a fixed command list."""

    def __init__(self, commands):
        self._commands = commands

    def walk_commands(self):
        return iter(self._commands)


def _cmd(name: str, description: str = "does a thing", parent=None):
    c = app_commands.Command(name=name, description=description, callback=_noop)
    c.parent = parent
    return c


async def _noop(interaction):  # pragma: no cover - never invoked
    return None


def _group(name: str, parent=None):
    g = app_commands.Group(name=name, description=f"{name} group")
    g.parent = parent
    return g


# ── collect_commands ─────────────────────────────────────────────────


def test_no_tree_yields_empty_catalog():
    assert collect_commands(None) == {}


def test_ungrouped_commands_land_under_general():
    tree = _FakeTree([_cmd("league"), _cmd("help")])
    found = collect_commands(tree)

    assert set(found) == {"general"}
    assert [n for n, _ in found["general"]] == ["/help", "/league"]


def test_subcommands_are_attributed_to_their_root_group():
    admin = _group("market-admin")
    tree = _FakeTree([admin, _cmd("run", parent=admin), _cmd("publish", parent=admin)])
    found = collect_commands(tree)

    assert set(found) == {"market-admin"}
    assert [n for n, _ in found["market-admin"]] == [
        "/market-admin publish",
        "/market-admin run",
    ]


def test_nested_group_attributes_to_the_outermost_root():
    """`/market-admin valuation run` must file under market-admin, not valuation."""
    admin = _group("market-admin")
    valuation = _group("valuation", parent=admin)
    tree = _FakeTree([admin, valuation, _cmd("run", parent=valuation)])

    found = collect_commands(tree)
    assert set(found) == {"market-admin"}


def test_groups_themselves_are_not_listed_as_commands():
    admin = _group("market-admin")
    tree = _FakeTree([admin, _cmd("run", parent=admin)])

    assert len(collect_commands(tree)["market-admin"]) == 1


def test_unknown_group_is_still_collected():
    """A future cog must appear in help without anyone editing this module."""
    newgroup = _group("brand-new")
    tree = _FakeTree([newgroup, _cmd("thing", parent=newgroup)])

    assert "brand-new" in collect_commands(tree)


# ── Select options ───────────────────────────────────────────────────


def test_select_options_respect_discord_limits():
    groups = []
    for i in range(40):
        g = _group(f"g{i}")
        groups.extend([g, _cmd("x", parent=g)])

    options = help_category_options(_FakeTree(groups))

    assert len(options) <= MAX_SELECT_OPTIONS
    for opt in options:
        assert len(opt.description) <= SELECT_DESC_LIMIT


def test_select_falls_back_to_curated_groups_without_a_tree():
    assert help_category_options(None), "help must render before the tree is populated"


def test_curated_groups_come_before_unknown_ones():
    admin = _group("market-admin")
    zzz = _group("zzz-new")
    tree = _FakeTree([admin, _cmd("a", parent=admin), zzz, _cmd("b", parent=zzz)])

    values = [o.value for o in help_category_options(tree)]
    assert values.index("market-admin") < values.index("zzz-new")


# ── Embeds ───────────────────────────────────────────────────────────


def _assert_embed_within_limits(embed: discord.Embed) -> None:
    assert len(embed.fields) <= MAX_FIELDS
    for f in embed.fields:
        assert len(f.name) <= EMBED_FIELD_NAME_LIMIT, f.name
        assert len(f.value) <= EMBED_FIELD_VALUE_LIMIT, f.name
    assert len(embed) <= EMBED_TOTAL_LIMIT


def test_overview_embed_is_within_limits():
    admin = _group("market-admin")
    cmds = [admin] + [_cmd(f"c{i}", parent=admin) for i in range(30)]
    _assert_embed_within_limits(build_help_embed(None, _FakeTree(cmds)))


def test_overview_points_at_the_panel_first():
    """The whole point of the panel is that most admins should not read this list."""
    assert "/league" in build_help_embed(None, None).description


def test_group_embed_packs_many_commands_without_breaching_limits():
    admin = _group("market-admin")
    cmds = [admin] + [
        _cmd(f"command-number-{i}", "a fairly wordy description " * 3, parent=admin)
        for i in range(30)
    ]

    embed = build_help_embed("market-admin", _FakeTree(cmds))
    _assert_embed_within_limits(embed)


def test_group_embed_lists_every_command_it_can_fit():
    admin = _group("market-admin")
    cmds = [admin] + [_cmd(f"c{i}", parent=admin) for i in range(5)]

    embed = build_help_embed("market-admin", _FakeTree(cmds))
    body = " ".join(f.value for f in embed.fields)
    for i in range(5):
        assert f"/market-admin c{i}" in body


def test_empty_group_says_so_rather_than_rendering_blank():
    embed = build_help_embed("trade", _FakeTree([]))
    assert any("None registered" in f.value for f in embed.fields)


@pytest.mark.parametrize("key", sorted(GROUP_BLURBS))
def test_every_curated_group_renders(key):
    _assert_embed_within_limits(build_help_embed(key, None))
