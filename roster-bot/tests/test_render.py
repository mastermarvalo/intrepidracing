"""Tests for bot.render.build_embed — no live Discord connection needed."""

from bot.render import build_embed
from tests.conftest import FakeMember, FakeRole, make_slot, make_team


TEAM_ROLE = 100
DRIVER_ROLE_1 = 201
DRIVER_ROLE_2 = 202
STAFF_ROLE = 301


def member(id: int, *role_ids: int) -> FakeMember:
    return FakeMember(id=id, roles=[FakeRole(r) for r in role_ids])


def field_by_name(embed, name: str):
    return next(f for f in embed.fields if f.name == name)


def staff_field(embed):
    return field_by_name(embed, "__**Staff**__")


def driver_field(embed):
    return field_by_name(embed, "__**Drivers**__")


# ── title / description ───────────────────────────────────────────────────────


def test_embed_title():
    embed = build_embed(make_team(), [])
    assert embed.title == "Test Team"


def test_embed_tagline_set():
    embed = build_embed(make_team(tagline="6x WCC"), [])
    assert embed.description == "6x WCC"


def test_embed_tagline_absent():
    embed = build_embed(make_team(), [])
    assert not embed.description


def test_embed_thumbnail_set():
    embed = build_embed(make_team(logo_url="https://example.com/logo.png"), [])
    assert embed.thumbnail.url == "https://example.com/logo.png"


def test_embed_thumbnail_absent():
    embed = build_embed(make_team(), [])
    assert embed.thumbnail.url is None


# ── section headers ───────────────────────────────────────────────────────────


def test_no_staff_header_when_no_staff_slots():
    team = make_team(slots=[make_slot(slot_role_id=DRIVER_ROLE_1, slot_type="driver")])
    embed = build_embed(team, [])
    names = [f.name for f in embed.fields]
    assert "__**Staff**__" not in names


def test_no_driver_header_when_no_driver_slots():
    team = make_team(slots=[make_slot(slot_role_id=STAFF_ROLE, slot_type="staff")])
    embed = build_embed(team, [])
    names = [f.name for f in embed.fields]
    assert "__**Drivers**__" not in names


def test_section_headers_order():
    slots = [
        make_slot(slot_role_id=STAFF_ROLE, slot_type="staff", sort_order=0),
        make_slot(slot_role_id=DRIVER_ROLE_1, slot_type="driver", sort_order=0),
    ]
    embed = build_embed(make_team(slots=slots), [])
    names = [f.name for f in embed.fields]
    assert names.index("__**Staff**__") < names.index("__**Drivers**__")


# ── member pool filtering ─────────────────────────────────────────────────────


def test_non_team_members_excluded():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=1)
    team = make_team(slots=[slot])
    # Has the slot role but NOT the team role — should not appear
    embed = build_embed(team, [member(1, DRIVER_ROLE_1)])
    assert "<@1>" not in driver_field(embed).value


def test_team_members_without_slot_role_excluded_from_slot():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=1)
    team = make_team(slots=[slot])
    # On the team but doesn't hold the slot role
    embed = build_embed(team, [member(1, TEAM_ROLE)])
    assert "<@1>" not in driver_field(embed).value
    assert "Spot Open" in driver_field(embed).value


# ── normal fill ───────────────────────────────────────────────────────────────


def test_filled_slot_shows_mention():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=2)
    team = make_team(slots=[slot])
    members = [member(10, TEAM_ROLE, DRIVER_ROLE_1), member(11, TEAM_ROLE, DRIVER_ROLE_1)]
    embed = build_embed(team, members)
    value = driver_field(embed).value
    assert "<@10>" in value
    assert "<@11>" in value
    assert "Spot Open" not in value


def test_partial_fill_pads_with_open_spots():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=3)
    team = make_team(slots=[slot])
    embed = build_embed(team, [member(10, TEAM_ROLE, DRIVER_ROLE_1)])
    value = driver_field(embed).value
    assert "<@10>" in value
    assert value.count("Spot Open") == 2


def test_empty_slot_all_open():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, quantity=2)
    team = make_team(slots=[slot])
    value = driver_field(build_embed(team, [])).value
    assert value.count("Spot Open") == 2
    assert "<@" not in value


# ── slot labels ───────────────────────────────────────────────────────────────


def test_slot_label_appears_in_section_value():
    slot = make_slot(slot_role_id=DRIVER_ROLE_1, label="Tier 1 Drivers", quantity=1)
    team = make_team(slots=[slot])
    value = driver_field(build_embed(team, [])).value
    assert "Tier 1 Drivers" in value


def test_slots_render_in_sort_order():
    slots = [
        make_slot(slot_role_id=DRIVER_ROLE_2, label="Tier 2", sort_order=1),
        make_slot(slot_role_id=DRIVER_ROLE_1, label="Tier 1", sort_order=0),
    ]
    value = driver_field(build_embed(make_team(slots=slots), [])).value
    assert value.index("Tier 1") < value.index("Tier 2")


# ── multiple slots ────────────────────────────────────────────────────────────


def test_multiple_slots_combined_in_one_field():
    slots = [
        make_slot(slot_role_id=DRIVER_ROLE_1, label="Tier 1", quantity=1, sort_order=0),
        make_slot(slot_role_id=DRIVER_ROLE_2, label="Tier 2", quantity=1, sort_order=1),
    ]
    team = make_team(slots=slots)
    members = [member(10, TEAM_ROLE, DRIVER_ROLE_1), member(11, TEAM_ROLE, DRIVER_ROLE_2)]
    embed = build_embed(team, members)
    # Both slots appear in the single Drivers field
    assert len([f for f in embed.fields if "Driver" in f.name]) == 1
    value = driver_field(embed).value
    assert "<@10>" in value
    assert "<@11>" in value
