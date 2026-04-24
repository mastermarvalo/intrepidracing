"""Tests for bot.render.build_embed — no live Discord connection needed."""

import pytest

from bot.render import build_embed
from tests.conftest import FakeMember, FakeRole, make_slot, make_team


TEAM_ROLE = 100
DRIVER_ROLE_1 = 201
DRIVER_ROLE_2 = 202
STAFF_ROLE = 301


def member(id: int, *role_ids: int) -> FakeMember:
    return FakeMember(id=id, roles=[FakeRole(r) for r in role_ids])


# ── title / description ───────────────────────────────────────────────────────


def test_embed_title():
    team = make_team()
    embed = build_embed(team, [])
    assert embed.title == "Test Team"


def test_embed_tagline_set():
    team = make_team(tagline="6x WCC")
    embed = build_embed(team, [])
    assert embed.description == "6x WCC"


def test_embed_tagline_absent():
    team = make_team()
    embed = build_embed(team, [])
    assert not embed.description


def test_embed_thumbnail_set():
    team = make_team(logo_url="https://example.com/logo.png")
    embed = build_embed(team, [])
    assert embed.thumbnail.url == "https://example.com/logo.png"


def test_embed_thumbnail_absent():
    team = make_team()
    embed = build_embed(team, [])
    assert embed.thumbnail.url is None


# ── section headers ───────────────────────────────────────────────────────────


def test_no_staff_header_when_no_staff_slots():
    team = make_team(slots=[make_slot(slot_role_id=DRIVER_ROLE_1, slot_type="driver")])
    embed = build_embed(team, [])
    names = [f.name for f in embed.fields]
    assert "Staff" not in names


def test_no_driver_header_when_no_driver_slots():
    team = make_team(slots=[make_slot(slot_role_id=STAFF_ROLE, slot_type="staff")])
    embed = build_embed(team, [])
    names = [f.name for f in embed.fields]
    assert "Drivers" not in names


def test_section_headers_order():
    """Staff section must come before Drivers section."""
    slots = [
        make_slot(slot_role_id=STAFF_ROLE, slot_type="staff", sort_order=0),
        make_slot(slot_role_id=DRIVER_ROLE_1, slot_type="driver", sort_order=0),
    ]
    team = make_team(slots=slots)
    embed = build_embed(team, [])
    names = [f.name for f in embed.fields]
    assert names.index("Staff") < names.index("Drivers")


# ── member pool filtering ─────────────────────────────────────────────────────


def test_non_team_members_excluded():
    """Members without the team role should not appear anywhere in the embed."""
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=1)
    team = make_team(slots=[slot])
    # member 1 has the slot role but NOT the team role
    members = [member(1, DRIVER_ROLE_1)]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "<@1>" not in field.value


def test_team_members_without_slot_role_are_excluded_from_slot():
    """Being on the team doesn't auto-fill a slot; member must also hold the slot role."""
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=1)
    team = make_team(slots=[slot])
    # member has team role but not the driver role
    members = [member(1, TEAM_ROLE)]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "<@1>" not in field.value
    assert "Spot Open" in field.value


# ── normal fill ───────────────────────────────────────────────────────────────


def test_filled_slot_shows_mention():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE_1),
        member(11, TEAM_ROLE, DRIVER_ROLE_1),
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "<@10>" in field.value
    assert "<@11>" in field.value
    assert "Spot Open" not in field.value


def test_partial_fill_pads_with_open_spots():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=3)
    team = make_team(slots=[slot])
    members = [member(10, TEAM_ROLE, DRIVER_ROLE_1)]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "<@10>" in field.value
    assert field.value.count("Spot Open") == 2


def test_empty_slot_all_open():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=2)
    team = make_team(slots=[slot])
    embed = build_embed(team, [])
    field = next(f for f in embed.fields if f.name == slot.label)
    assert field.value.count("Spot Open") == 2
    assert "<@" not in field.value


# ── sort order ────────────────────────────────────────────────────────────────


def test_slots_render_in_sort_order():
    slots = [
        make_slot(slot_role_id=DRIVER_ROLE_2, label="Tier 2", sort_order=1),
        make_slot(slot_role_id=DRIVER_ROLE_1, label="Tier 1", sort_order=0),
    ]
    team = make_team(slots=slots)
    embed = build_embed(team, [])
    driver_names = [f.name for f in embed.fields if f.name in ("Tier 1", "Tier 2")]
    assert driver_names == ["Tier 1", "Tier 2"]


# ── multiple slots, multiple members ─────────────────────────────────────────


def test_multiple_slots_independent():
    """Members in slot A should not appear in slot B's field."""
    slot_a = make_slot(slot_role_id=DRIVER_ROLE_1, label="Tier 1", quantity=1, sort_order=0)
    slot_b = make_slot(slot_role_id=DRIVER_ROLE_2, label="Tier 2", quantity=1, sort_order=1)
    team = make_team(slots=[slot_a, slot_b])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE_1),
        member(11, TEAM_ROLE, DRIVER_ROLE_2),
    ]
    embed = build_embed(team, members)
    field_a = next(f for f in embed.fields if f.name == "Tier 1")
    field_b = next(f for f in embed.fields if f.name == "Tier 2")
    assert "<@10>" in field_a.value and "<@11>" not in field_a.value
    assert "<@11>" in field_b.value and "<@10>" not in field_b.value
