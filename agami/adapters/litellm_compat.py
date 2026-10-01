"""Read-only mapping from LiteLLM's user, organization membership and team rows to an Agami Actor."""

from collections.abc import Awaitable, Callable, Iterable, Sequence
from types import MappingProxyType
from typing import Final, TypeAlias

from pydantic import TypeAdapter

from agami.auth.context import Actor, TeamMembership
from agami.auth.roles import TenantRole
from litellm.models.team import LiteLLM_TeamTable
from litellm.models.user import LiteLLM_UserTable
from litellm.proxy._types import LitellmUserRoles

_VIEW_ONLY_USER_ROLES: Final = frozenset(
    {LitellmUserRoles.INTERNAL_USER_VIEW_ONLY, LitellmUserRoles.PROXY_ADMIN_VIEW_ONLY}
)
UserLookup: TypeAlias = Callable[[str], Awaitable[LiteLLM_UserTable | None]]
TeamsLookup: TypeAlias = Callable[[Sequence[str]], Awaitable[Sequence[LiteLLM_TeamTable]]]

_USER_METADATA: Final[TypeAdapter[dict[str, object]]] = TypeAdapter(dict[str, object])
_TEAM_ROLE_BY_MEMBER_ROLE: Final = MappingProxyType({"admin": TenantRole.TEAM_ADMIN, "user": TenantRole.TEAM_MEMBER})


def _org_role(membership_role: str | None, view_only: bool) -> TenantRole:
    if not view_only and membership_role == LitellmUserRoles.ORG_ADMIN.value:
        return TenantRole.ORG_ADMIN
    return TenantRole.VIEWER


def _team_roles(team: LiteLLM_TeamTable, user_id: str, view_only: bool) -> frozenset[TenantRole]:
    return frozenset(
        TenantRole.VIEWER if view_only else _TEAM_ROLE_BY_MEMBER_ROLE[member.role]
        for member in team.members_with_roles
        if member.user_id == user_id
    )


def _is_active(user: LiteLLM_UserTable) -> bool:
    metadata: Final = _USER_METADATA.validate_python(
        user.metadata or {}  # pyright: ignore[reportUnknownMemberType] # LiteLLM types metadata as a bare dict
    )
    return metadata.get("scim_active") is not False


def actor_from_litellm(user: LiteLLM_UserTable, teams: Iterable[LiteLLM_TeamTable]) -> Actor:
    view_only: Final = user.user_role in _VIEW_ONLY_USER_ROLES
    organization_roles: Final = MappingProxyType(
        {
            membership.organization_id: frozenset({_org_role(membership.user_role, view_only)})
            for membership in user.organization_memberships or ()
        }
    )
    team_memberships: Final = MappingProxyType(
        {
            team.team_id: TeamMembership(organization_id=team.organization_id, roles=roles)
            for team in teams
            if team.organization_id is not None and (roles := _team_roles(team, user.user_id, view_only))
        }
    )
    return Actor(
        user_id=user.user_id,
        is_super_admin=user.user_role == LitellmUserRoles.PROXY_ADMIN.value,
        is_active=_is_active(user),
        organization_roles=organization_roles,
        team_memberships=team_memberships,
    )


async def load_actor(
    user_id: str | None,
    key_user_role: str | None,
    fetch_user: UserLookup,
    fetch_teams: TeamsLookup,
) -> Actor:
    """Keys without a user row (the master key, service keys) act only with their key-level role."""
    user: Final = await fetch_user(user_id) if user_id else None
    if user is None:
        return Actor(
            user_id=user_id or "",
            is_super_admin=key_user_role == LitellmUserRoles.PROXY_ADMIN.value,
            is_active=True,
            organization_roles=MappingProxyType({}),
            team_memberships=MappingProxyType({}),
        )
    teams: Final = await fetch_teams(tuple(user.teams)) if user.teams else ()
    return actor_from_litellm(user, teams)
