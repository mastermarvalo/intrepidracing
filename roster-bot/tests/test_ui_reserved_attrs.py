"""
discord.py ≥ 2.6 gives every `discord.ui.Item` a private `_parent`
(its container) and walks it in `Item._run_checks` before every
callback. Our buttons/selects used `self._parent` for the owning view,
so the first click on the panel raised
`AttributeError: '_FreeAgencyView' object has no attribute '_run_checks'`.

This guard scans every `discord.ui.*` subclass in the codebase and
fails if it assigns any attribute the library reserves for itself. Add
to the list if a future discord.py release grows a new one.
"""

import ast
import pathlib
from decimal import Decimal

import discord

ROOT = pathlib.Path(__file__).resolve().parents[1] / "bot"

# Instance attributes discord.py sets on Item / View / Modal and reads
# back internally. Shadowing any of them breaks dispatch silently until
# a user clicks.
RESERVED = frozenset({
    "_parent", "_view", "_row", "_id", "_rendered_row", "_provided_custom_id",
    "_underlying", "_max_row", "_children", "_timeout", "_cache_key",
})


def _ui_classes():
    for path in sorted(ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            bases = [ast.unparse(b) for b in node.bases]
            if any(b.startswith("discord.ui.") or b in _OUR_UI_BASES for b in bases):
                yield path, node


_OUR_UI_BASES = {"OwnedView", "AdminOwnedView", "BackButton"}


def _self_assignments(cls: ast.ClassDef):
    for node in ast.walk(cls):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.ctx, ast.Store)
            and isinstance(node.value, ast.Name)
            and node.value.id == "self"
        ):
            yield node


def test_no_ui_class_shadows_a_discord_private_attribute():
    offenders = []
    for path, cls in _ui_classes():
        for attr in _self_assignments(cls):
            if attr.attr in RESERVED:
                offenders.append(f"{path.relative_to(ROOT.parent)}:{attr.lineno} "
                                 f"{cls.name} sets self.{attr.attr}")
    assert offenders == [], "\n".join(offenders)


def test_reserved_list_matches_installed_discord_py():
    """If the library stops using `_parent` this test tells us the guard is stale."""
    assert hasattr(discord.ui.Item, "_run_checks")
    item = discord.ui.Button(label="x")
    assert item._parent is None
    assert "_parent" in RESERVED


# ── The exact dispatch path that crashed in production ────────────────


class _Interaction:
    """Minimal stand-in: `_run_checks` only reads `user.id`."""

    class user:
        id = 1


async def _click(item: discord.ui.Item) -> bool:
    # This is what discord.ui.View._scheduled_task awaits before a callback.
    return await item._run_checks(_Interaction())  # type: ignore[arg-type]


async def test_free_agency_toggle_passes_discord_checks():
    from bot.ui import setup_screen

    parent = setup_screen.SetupView.__new__(setup_screen.SetupView)
    parent.opener_id = 1
    view = setup_screen._FreeAgencyView(parent=parent)
    toggle = setup_screen._FreeAgencyToggle(view, is_open=False)
    view.add_item(toggle)
    assert toggle._parent is None
    assert await _click(toggle)


async def test_adjust_cap_select_passes_discord_checks():
    from bot.ui import setup_screen

    parent = setup_screen.SetupView.__new__(setup_screen.SetupView)
    parent.opener_id = 1
    view = setup_screen._TeamsView(parent=parent)
    select = setup_screen._AdjustCapSelect(
        view, [_FakeTeam(name="Haas", key="haas", payroll=Decimal("10"))]
    )
    view.add_item(select)
    assert await _click(select)


async def test_driver_picker_passes_discord_checks():
    from bot.ui import drivers_screen

    async def noop_back(_):
        return None

    view = drivers_screen.DriversView(summaries=[], opener_id=1, on_back=noop_back)
    select = drivers_screen._DriverPickerSelect(view, [])
    view.add_item(select)
    assert await _click(select)


class _FakeTeam:
    def __init__(self, **kw):
        self.__dict__.update(kw)
