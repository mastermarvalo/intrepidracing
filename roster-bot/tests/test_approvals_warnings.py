"""
Approval-time warnings (G11, G12, G26).

`approvals.approve_offer` commits the money side inside the DB
transaction and then does three things that can silently not happen:
snapshot `value_at_signing`, assign the Discord team role, and post the
public signing embed. Each one used to fail quietly while the receipt
still said "Approved". These tests pin the warnings that now come back
on `OfferApprovalResult`, and pin that the happy path stays silent.

The DB fixture is real (`pg_conn_migrated`) because the warnings are
driven by persisted state — a NULL `value_at_signing`, a missing team
row, a `guild_config` with no transactions channel. Only the Discord
side is faked.
"""

from decimal import Decimal

import discord
import pytest

from bot import approvals, queries, roster_ops
from bot.contracts import service
from bot.presets import f1 as f1_preset

GUILD_ID = 42
DRIVER_MEMBER_ID = 111
TRANSACTIONS_CHANNEL_ID = 7001


# ── Discord fakes ────────────────────────────────────────────────────


class FakeRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class FakeMember:
    """Records role mutations instead of calling Discord."""

    def __init__(self, member_id: int) -> None:
        self.id = member_id
        self.roles: list[FakeRole] = []
        self.added: list[int] = []
        self.removed: list[int] = []

    async def add_roles(self, *roles, reason=None) -> None:
        self.added.extend(r.id for r in roles)
        self.roles.extend(roles)

    async def remove_roles(self, *roles, reason=None) -> None:
        self.removed.extend(r.id for r in roles)


class FakeGuild:
    def __init__(self, *, members=(), roles=()) -> None:
        self.id = GUILD_ID
        self._members = {m.id: m for m in members}
        self._roles = {r.id: r for r in roles}

    def get_member(self, member_id: int):
        return self._members.get(member_id)

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class FakeActor:
    id = 999
    mention = "<@999>"

    def __str__(self) -> str:  # used in roster_ops audit reasons
        return "Commissioner"


class FakeTextChannel(discord.TextChannel):
    """
    Real `discord.TextChannel` subclass so `isinstance` in approvals.py
    still means something; `__init__` deliberately does not call super,
    which would need a connection state.
    """

    def __init__(self, *, forbidden: bool = False) -> None:
        self.sent: list[discord.Embed] = []
        self._forbidden = forbidden

    async def send(self, *, embed=None, **kwargs):
        if self._forbidden:
            raise discord.Forbidden(_FakeResponse(), "missing permissions")
        self.sent.append(embed)


class _FakeResponse:
    status = 403
    reason = "Forbidden"


class FakeClient:
    def __init__(self, channels=None) -> None:
        self._channels = channels or {}

    def get_channel(self, channel_id: int):
        return self._channels.get(channel_id)


# ── DB scaffolding ───────────────────────────────────────────────────


@pytest.fixture
def approvals_db(monkeypatch, pg_conn_migrated):
    """Point `db.connect()` at the test schema for approvals + roster_ops."""

    class _Ctx:
        async def __aenter__(self):
            return pg_conn_migrated

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(approvals.db, "connect", lambda: _Ctx())
    monkeypatch.setattr(roster_ops.db, "connect", lambda: _Ctx())
    return pg_conn_migrated


class Fixture:
    """Ids for one seeded season/tier/team/driver."""

    def __init__(self, *, season_id, tier_id, team_id, team_role_id, driver_id):
        self.season_id = season_id
        self.tier_id = tier_id
        self.team_id = team_id
        self.team_role_id = team_role_id
        self.driver_id = driver_id


async def _seed(conn, *, transactions_channel_id=TRANSACTIONS_CHANNEL_ID):
    season_id = await conn.fetchval(
        "INSERT INTO seasons (guild_id, name, is_active) VALUES ($1, 'S1', TRUE) "
        "RETURNING id",
        GUILD_ID,
    )
    await f1_preset.seed_season(conn, season_id)
    tier_id = await conn.fetchval(
        "SELECT id FROM tiers WHERE season_id = $1 AND code = 't1'", season_id
    )
    team_role_id = 200
    team_id = await conn.fetchval(
        """
        INSERT INTO teams (guild_id, key, name, team_role_id, channel_id)
        VALUES ($1, 'rb', 'Red Bull', $2, 300) RETURNING id
        """,
        GUILD_ID, team_role_id,
    )
    driver_id = await queries.insert_driver(
        conn, season_id, tier_id,
        member_id=DRIVER_MEMBER_ID, display_name="Test Driver", status="active",
    )
    if transactions_channel_id is not None:
        await conn.execute(
            "INSERT INTO guild_config (guild_id, transactions_channel_id) "
            "VALUES ($1, $2)",
            GUILD_ID, transactions_channel_id,
        )
    return Fixture(
        season_id=season_id, tier_id=tier_id, team_id=team_id,
        team_role_id=team_role_id, driver_id=driver_id,
    )


async def _publish_value(conn, fx, value=Decimal("6.50")):
    run_id = await queries.insert_valuation_run(
        conn, season_id=fx.season_id, tier_id=fx.tier_id,
        round_label="R1", created_by=1, published=True,
    )
    await queries.insert_driver_valuations(
        conn,
        run_id,
        [
            {
                "driver_id": fx.driver_id,
                "market_value": value,
                "previous_value": None,
                "delta": Decimal("0"),
                "rank_in_tier": 1,
                "capped": False,
                "breakdown": [],
            }
        ],
    )


async def _offer_awaiting_approval(conn, fx, *, salary=Decimal("5.00")):
    offer_id = await service.submit_offer(
        conn,
        season_id=fx.season_id, tier_id=fx.tier_id,
        driver_id=fx.driver_id, team_id=fx.team_id,
        offered_by=100, offer_kind="new",
        salary=salary, term_seasons=2, contract_type="standard",
        signing_bonus=Decimal("0"), incentives=None, message=None,
        ttl_hours=48, validation={"ok": True},
    )
    await service.driver_accept(conn, offer_id, actor_id=DRIVER_MEMBER_ID)
    return offer_id


def _guild_with_member():
    member = FakeMember(DRIVER_MEMBER_ID)
    return FakeGuild(members=[member], roles=[FakeRole(200)]), member


# ── Happy path: no warnings at all ───────────────────────────────────


async def test_fully_configured_approval_reports_no_warnings(approvals_db):
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, member = _guild_with_member()
    channel = FakeTextChannel()

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: channel}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.role_warning is None
    assert result.valuation_warning is None
    assert result.announcement_warning is None
    assert result.warnings == []
    assert result.has_warnings is False
    assert result.announced is True
    assert result.value_at_signing == Decimal("6.50")
    assert member.added == [fx.team_role_id], "team role must be assigned"
    assert len(channel.sent) == 1, "signing must be announced once"


# ── G12: role assignment silently skipped ────────────────────────────


async def test_member_not_in_guild_warns_role_not_assigned(approvals_db):
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild = FakeGuild(members=[], roles=[FakeRole(200)])  # member not cached
    channel = FakeTextChannel()

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: channel}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.role_warning == approvals.MEMBER_MISSING_ROLE_WARNING
    assert result.role_warning in result.warnings
    assert result.has_warnings is True
    # The commissioner has to be able to tell the role did NOT happen and
    # that the roster now disagrees with the contract.
    assert "NOT assigned" in result.role_warning
    assert "disagrees with the contract" in result.role_warning
    # The contract itself still exists — a role failure never rolls back.
    contract = await queries.fetch_contract_by_id(approvals_db, result.contract_id)
    assert contract is not None and contract.state == "active"


async def test_missing_team_row_warns_role_not_assigned(approvals_db, monkeypatch):
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, member = _guild_with_member()

    async def _no_team(conn, team_id):
        return None

    monkeypatch.setattr(approvals.queries, "fetch_team_by_id", _no_team)

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: FakeTextChannel()}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.role_warning == approvals.TEAM_MISSING_ROLE_WARNING
    assert "NOT assigned" in result.role_warning
    assert "disagrees with the contract" in result.role_warning
    assert member.added == [], "no role may be assigned without a team"


async def test_role_assignment_error_still_surfaces(approvals_db):
    """The pre-existing RoleAssignmentError wording must not regress."""
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    # Guild has the member but not the team role → RoleAssignmentError.
    member = FakeMember(DRIVER_MEMBER_ID)
    guild = FakeGuild(members=[member], roles=[])

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: FakeTextChannel()}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.role_warning is not None
    assert str(fx.team_role_id) in result.role_warning


# ── G11: no published valuation → P/L can never be tracked ───────────


async def test_no_published_valuation_warns_pl_is_untrackable(approvals_db):
    fx = await _seed(approvals_db)  # deliberately no valuation published
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, _ = _guild_with_member()

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: FakeTextChannel()}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.valuation_warning is not None
    assert "Test Driver" in result.valuation_warning
    assert "P/L can never be tracked" in result.valuation_warning
    assert "valuation" in result.valuation_warning
    assert result.value_at_signing is None
    assert result.valuation_warning in result.warnings
    # And the stored snapshot really is empty — this is what makes it
    # unrecoverable later.
    contract = await queries.fetch_contract_by_id(approvals_db, result.contract_id)
    assert contract.value_at_signing is None


async def test_unpublished_valuation_does_not_count(approvals_db):
    fx = await _seed(approvals_db)
    run_id = await queries.insert_valuation_run(
        approvals_db, season_id=fx.season_id, tier_id=fx.tier_id,
        round_label="R1", created_by=1, published=False,
    )
    await queries.insert_driver_valuations(
        approvals_db,
        run_id,
        [
            {
                "driver_id": fx.driver_id,
                "market_value": Decimal("9.00"),
                "previous_value": None,
                "delta": Decimal("0"),
                "rank_in_tier": 1,
                "capped": False,
                "breakdown": [],
            }
        ],
    )
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, _ = _guild_with_member()

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: FakeTextChannel()}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.value_at_signing is None
    assert result.valuation_warning is not None


# ── G26: nothing announced ───────────────────────────────────────────


async def test_no_transactions_channel_warns_nothing_announced(approvals_db):
    fx = await _seed(approvals_db, transactions_channel_id=None)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, member = _guild_with_member()

    result = await approvals.approve_offer(
        FakeClient(),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.announcement_warning == approvals.NO_TRANSACTIONS_CHANNEL_WARNING
    assert "nothing was announced" in result.announcement_warning
    assert "backfill" in result.announcement_warning
    assert "Setup → Channels" in result.announcement_warning
    assert result.announced is False
    # Role assignment is unaffected by the announcement failure.
    assert member.added == [fx.team_role_id]
    assert result.role_warning is None


async def test_forbidden_post_warns_nothing_announced(approvals_db):
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, _ = _guild_with_member()
    channel = FakeTextChannel(forbidden=True)

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: channel}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.announcement_warning == approvals.FORBIDDEN_ANNOUNCE_WARNING
    assert "announced" in result.announcement_warning
    assert "backfill" in result.announcement_warning
    assert "Setup → Channels" in result.announcement_warning
    assert result.announced is False
    assert channel.sent == []


async def test_unreachable_channel_warns_nothing_announced(approvals_db):
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, _ = _guild_with_member()

    result = await approvals.approve_offer(
        FakeClient(),  # channel id configured, client cannot resolve it
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.announcement_warning is not None
    assert str(TRANSACTIONS_CHANNEL_ID) in result.announcement_warning
    assert "nothing was announced" in result.announcement_warning
    assert "Setup → Channels" in result.announcement_warning
    assert result.announced is False


# ── All three at once, and the shape other code reads ────────────────


async def test_all_three_warnings_can_fire_together(approvals_db):
    fx = await _seed(approvals_db, transactions_channel_id=None)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild = FakeGuild(members=[], roles=[])

    result = await approvals.approve_offer(
        FakeClient(),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.valuation_warning is not None
    assert result.role_warning is not None
    assert result.announcement_warning is not None
    assert len(result.warnings) == 3
    # Reporting order: snapshot, role, announcement.
    assert result.warnings == [
        result.valuation_warning,
        result.role_warning,
        result.announcement_warning,
    ]


async def test_result_keeps_its_existing_fields(approvals_db):
    """Additive only — existing readers must keep working."""
    fx = await _seed(approvals_db)
    await _publish_value(approvals_db, fx)
    offer_id = await _offer_awaiting_approval(approvals_db, fx)
    guild, _ = _guild_with_member()

    result = await approvals.approve_offer(
        FakeClient({TRANSACTIONS_CHANNEL_ID: FakeTextChannel()}),
        guild=guild,
        actor=FakeActor(),
        offer_id=offer_id,
    )

    assert result.offer_id == offer_id
    assert isinstance(result.contract_id, int)
    assert result.external_ref
    assert hasattr(result, "role_warning")
    # Constructible with the old signature only.
    legacy = approvals.OfferApprovalResult(offer_id=1, contract_id=2, external_ref="X")
    assert legacy.warnings == []
    assert legacy.announced is False
