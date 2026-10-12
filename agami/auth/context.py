from collections.abc import Mapping
from dataclasses import dataclass

from agami.auth.permissions import ResourceType
from agami.auth.roles import TenantRole


@dataclass(frozen=True, slots=True)
class TeamMembership:
    organization_id: str
    roles: frozenset[TenantRole]


@dataclass(frozen=True, slots=True)
class Actor:
    user_id: str
    is_super_admin: bool
    is_active: bool
    organization_roles: Mapping[str, frozenset[TenantRole]]
    team_memberships: Mapping[str, TeamMembership]


@dataclass(frozen=True, slots=True)
class Resource:
    resource_type: ResourceType
    organization_id: str
    resource_id: str | None = None
    team_id: str | None = None
    owner_user_id: str | None = None
