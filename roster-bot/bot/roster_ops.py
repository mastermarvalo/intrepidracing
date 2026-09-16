"""
Shared role-mutation helpers.

`/roster sign` and `/contract approve` both need the same behaviour:
assign the team role, strip the free-agent role if it's set. Rather
than duplicate the logic (or violate CLAUDE.md's "do not duplicate
role logic" note), both call through here.

Kept small on purpose — this module only owns the role-mutation
sequence, not the surrounding permission checks or user messaging.
Callers decide who is allowed to do what; this function just does it
transactionally.
"""

from __future__ import annotations

import logging

import discord

from bot import db, queries

log = logging.getLogger(__name__)


class RoleAssignmentError(Exception):
    """Raised when the bot cannot mutate the required role."""


async def sign_to_team(
    *,
    guild: discord.Guild,
    member: discord.Member,
    team,
    actor: discord.abc.User,
    reason: str | None = None,
) -> None:
    """
    Give `member` the team role and strip the FA role if configured.
    Raises RoleAssignmentError on missing role or missing permissions.
    Reason string is passed through to the audit log Discord shows
    for role changes.
    """
    async with db.connect() as conn:
        config = await queries.fetch_guild_config(conn, guild.id)

    team_role = guild.get_role(team.team_role_id)
    if team_role is None:
        raise RoleAssignmentError(
            f"Team role {team.team_role_id} for team {team.key} is missing "
            f"— was it deleted from the server?"
        )

    to_add: list[discord.Role] = [team_role]
    to_remove: list[discord.Role] = []
    if config.free_agent_role_id:
        fa_role = guild.get_role(config.free_agent_role_id)
        if fa_role is not None and fa_role in member.roles:
            to_remove.append(fa_role)

    audit_reason = reason or f"Signed to {team.name} by {actor}"
    try:
        await member.add_roles(*to_add, reason=audit_reason)
        if to_remove:
            await member.remove_roles(*to_remove, reason=audit_reason)
    except discord.Forbidden as exc:
        raise RoleAssignmentError(
            "The bot doesn't have permission to manage team roles. Make "
            "sure its role sits above the team roles in Server Settings → "
            "Roles."
        ) from exc


async def drop_from_team(
    *,
    guild: discord.Guild,
    member: discord.Member,
    team,
    actor: discord.abc.User,
    reason: str | None = None,
) -> None:
    """Strip the team role and re-add the FA role if configured."""
    async with db.connect() as conn:
        config = await queries.fetch_guild_config(conn, guild.id)

    team_role = guild.get_role(team.team_role_id)
    to_remove: list[discord.Role] = []
    if team_role is not None and team_role in member.roles:
        to_remove.append(team_role)

    to_add: list[discord.Role] = []
    if config.free_agent_role_id:
        fa_role = guild.get_role(config.free_agent_role_id)
        if fa_role is not None and fa_role not in member.roles:
            to_add.append(fa_role)

    audit_reason = reason or f"Dropped from {team.name} by {actor}"
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason=audit_reason)
        if to_add:
            await member.add_roles(*to_add, reason=audit_reason)
    except discord.Forbidden as exc:
        raise RoleAssignmentError(
            "The bot doesn't have permission to manage team roles."
        ) from exc


async def drop_from_team_best_effort(
    *,
    guild: discord.Guild | None,
    member_id: int,
    team,
    actor: discord.abc.User,
    reason: str | None = None,
) -> str | None:
    """
    Drop the team role without letting a role failure undo book-keeping.

    Returns None when the role was dropped or there was nothing to drop,
    otherwise the reason it could not be, for the caller to surface.

    Ending a contract is two separate things: the money, which is
    already committed to the ledger by the time this runs, and the
    Discord role, which is cosmetic and can be fixed by hand. A missing
    permission must not roll back a settled release or void, but it must
    not pass silently either — a driver still wearing a team role looks
    signed to everyone in the server.

    `member_id` rather than a Member because callers hold a driver row,
    and a driver who has left the guild has no roles left to strip.
    """
    if guild is None or team is None:
        return None
    member = guild.get_member(member_id)
    if member is None:
        return None
    try:
        await drop_from_team(
            guild=guild, member=member, team=team, actor=actor, reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        return str(exc) or exc.__class__.__name__
    return None
