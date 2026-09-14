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
