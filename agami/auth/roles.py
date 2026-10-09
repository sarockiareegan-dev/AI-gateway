from collections.abc import Mapping
from types import MappingProxyType
from typing import Final

from agami._compat import StrEnum
from agami.auth.permissions import Action, ResourceType


class TenantRole(StrEnum):
    ORG_ADMIN = "org_admin"
    TEAM_ADMIN = "team_admin"
    TEAM_MEMBER = "team_member"
    VIEWER = "viewer"


class Grant(StrEnum):
    ANY = "any"
    OWN = "own"


def _row(
    org_admin: Grant | None = None,
    team_admin: Grant | None = None,
    team_member: Grant | None = None,
    viewer: Grant | None = None,
) -> Mapping[TenantRole, Grant]:
    return MappingProxyType(
        {
            role: grant
            for role, grant in (
                (TenantRole.ORG_ADMIN, org_admin),
                (TenantRole.TEAM_ADMIN, team_admin),
                (TenantRole.TEAM_MEMBER, team_member),
                (TenantRole.VIEWER, viewer),
            )
            if grant is not None
        }
    )


_EVERYONE: Final = _row(Grant.ANY, Grant.ANY, Grant.ANY, Grant.ANY)
_OPERATORS: Final = _row(Grant.ANY, Grant.ANY, Grant.ANY)
_ADMINS: Final = _row(Grant.ANY, Grant.ANY)
_ORG_ADMIN: Final = _row(Grant.ANY)
_PERSONAL_KEYS: Final = _row(Grant.ANY, Grant.ANY, Grant.OWN)

# Pairs absent from the matrix are reserved for super admins: creating and deleting organizations.
PERMISSION_MATRIX: Final[Mapping[tuple[ResourceType, Action], Mapping[TenantRole, Grant]]] = MappingProxyType(
    {
        (ResourceType.ORGANIZATION, Action.VIEW): _EVERYONE,
        (ResourceType.ORGANIZATION, Action.EDIT): _ORG_ADMIN,
        (ResourceType.ORGANIZATION, Action.MANAGE_MEMBERS): _ORG_ADMIN,
        (ResourceType.TEAM, Action.VIEW): _EVERYONE,
        (ResourceType.TEAM, Action.CREATE): _ADMINS,
        (ResourceType.TEAM, Action.EDIT): _ADMINS,
        (ResourceType.TEAM, Action.MANAGE_MEMBERS): _ADMINS,
        (ResourceType.TEAM, Action.DELETE): _ORG_ADMIN,
        (ResourceType.KEY, Action.VIEW): _EVERYONE,
        (ResourceType.KEY, Action.CREATE): _PERSONAL_KEYS,
        (ResourceType.KEY, Action.EDIT): _PERSONAL_KEYS,
        (ResourceType.KEY, Action.REVOKE): _PERSONAL_KEYS,
        (ResourceType.KEY, Action.DELETE): _ORG_ADMIN,
        (ResourceType.KEY, Action.USE): _OPERATORS,
        (ResourceType.BUDGET, Action.VIEW): _EVERYONE,
        (ResourceType.BUDGET, Action.CREATE): _ORG_ADMIN,
        (ResourceType.BUDGET, Action.EDIT): _ORG_ADMIN,
        (ResourceType.BUDGET, Action.OVERRIDE): _ORG_ADMIN,
        (ResourceType.MODEL, Action.VIEW): _EVERYONE,
        (ResourceType.MODEL, Action.USE): _OPERATORS,
        (ResourceType.MODEL, Action.CREATE): _ORG_ADMIN,
        (ResourceType.MODEL, Action.EDIT): _ORG_ADMIN,
        (ResourceType.MODEL, Action.DELETE): _ORG_ADMIN,
        (ResourceType.MODEL, Action.ASSIGN): _ORG_ADMIN,
        (ResourceType.MODEL, Action.RESTRICT): _ORG_ADMIN,
    }
)
