"""Overflow-specific tests — members beyond slot quantity are flagged, not hidden."""

from bot.render import build_embed
from tests.conftest import FakeMember, FakeRole, make_slot, make_team


TEAM_ROLE = 100
DRIVER_ROLE = 201


def member(id: int, *role_ids: int) -> FakeMember:
    return FakeMember(id=id, roles=[FakeRole(r) for r in role_ids])


def test_overflow_members_all_visible():
    """All members should appear in the field even when count exceeds quantity."""
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
        member(12, TEAM_ROLE, DRIVER_ROLE),  # overflow
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "<@10>" in field.value
    assert "<@11>" in field.value
    assert "<@12>" in field.value


def test_overflow_members_flagged():
    """Members beyond quantity should have the overflow marker."""
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=1)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),  # overflow
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    # member 10 is within quota — no overflow tag
    lines = field.value.splitlines()
    assert lines[0] == "<@10>"
    assert "overflow" in lines[1]
    assert "<@11>" in lines[1]


def test_no_open_spots_when_overflowing():
    """Overflow implies all seats are taken — no *Spot Open* padding."""
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=1)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "Spot Open" not in field.value


def test_overflow_exact_capacity_no_flag():
    """Filling exactly to capacity should not trigger overflow."""
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    assert "overflow" not in field.value
    assert "Spot Open" not in field.value


def test_single_overflow():
    """One member over capacity: the third member gets flagged."""
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
        member(12, TEAM_ROLE, DRIVER_ROLE),
    ]
    embed = build_embed(team, members)
    field = next(f for f in embed.fields if f.name == slot.label)
    lines = field.value.splitlines()
    assert lines[0] == "<@10>"
    assert lines[1] == "<@11>"
    assert "overflow" in lines[2]
