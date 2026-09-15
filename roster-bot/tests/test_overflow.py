"""Overflow-specific tests — members beyond slot quantity are flagged, not hidden."""

from bot.render import build_embed
from tests.conftest import FakeMember, FakeRole, make_slot, make_team

TEAM_ROLE = 100
DRIVER_ROLE = 201


def member(id: int, *role_ids: int) -> FakeMember:
    return FakeMember(id=id, roles=[FakeRole(r) for r in role_ids])


def driver_value(embed) -> str:
    desc = embed.description or ""
    header = "## __Drivers__"
    if header not in desc:
        return ""
    after = desc[desc.index(header) + len(header):]
    next_h = after.find("## __")
    return (after[:next_h] if next_h != -1 else after).strip()


def test_overflow_members_all_visible():
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
        member(12, TEAM_ROLE, DRIVER_ROLE),  # overflow
    ]
    value = driver_value(build_embed(team, members))
    assert "Member10" in value
    assert "Member11" in value
    assert "Member12" in value


def test_overflow_members_flagged():
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=1)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),  # overflow
    ]
    value = driver_value(build_embed(team, members))
    # First line after the label is member 10 (within quota)
    lines = value.splitlines()
    label_idx = next(i for i, line in enumerate(lines) if "Slot" in line)
    assert lines[label_idx + 1] == "Member10"
    assert "overflow" in lines[label_idx + 2]
    assert "Member11" in lines[label_idx + 2]


def test_no_open_spots_when_overflowing():
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=1)
    team = make_team(slots=[slot])
    members = [member(10, TEAM_ROLE, DRIVER_ROLE), member(11, TEAM_ROLE, DRIVER_ROLE)]
    value = driver_value(build_embed(team, members))
    assert "Spot Open" not in value


def test_overflow_exact_capacity_no_flag():
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [member(10, TEAM_ROLE, DRIVER_ROLE), member(11, TEAM_ROLE, DRIVER_ROLE)]
    value = driver_value(build_embed(team, members))
    assert "overflow" not in value
    assert "Spot Open" not in value


def test_single_overflow():
    slot = make_slot(slot_role_id=DRIVER_ROLE, quantity=2)
    team = make_team(slots=[slot])
    members = [
        member(10, TEAM_ROLE, DRIVER_ROLE),
        member(11, TEAM_ROLE, DRIVER_ROLE),
        member(12, TEAM_ROLE, DRIVER_ROLE),
    ]
    value = driver_value(build_embed(team, members))
    lines = value.splitlines()
    label_idx = next(i for i, line in enumerate(lines) if "Slot" in line)
    assert lines[label_idx + 1] == "Member10"
    assert lines[label_idx + 2] == "Member11"
    assert "overflow" in lines[label_idx + 3]
