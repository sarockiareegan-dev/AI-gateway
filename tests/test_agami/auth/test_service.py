from types import MappingProxyType
from typing import Final

import pytest
from fastapi import HTTPException

from agami.auth.context import Actor, Resource, TeamMembership
from agami.auth.permissions import Action, ResourceType
from agami.auth.roles import TenantRole
from agami.auth.service import Allowed, Denied, DenialReason, authorize, raise_for_decision

ORG: Final = "org-a"
OTHER_ORG: Final = "org-b"
TEAM: Final = "team-a1"
OTHER_TEAM: Final = "team-a2"
USER: Final = "user-1"

R = ResourceType
A = Action
ORG_ADMIN, TEAM_ADMIN, TEAM_MEMBER, VIEWER = (
    TenantRole.ORG_ADMIN,
    TenantRole.TEAM_ADMIN,
    TenantRole.TEAM_MEMBER,
    TenantRole.VIEWER,
)
ALL_ROLES: Final = frozenset(TenantRole)
OWN: Final = "own"

# The Agami RBAC spec's permission matrix, plus the product decisions that only super admins
# create or delete organizations and that org admins manage their organization's models.
SPEC_MATRIX: Final[dict[tuple[ResourceType, Action], dict[TenantRole, bool | str]]] = {
    (R.ORGANIZATION, A.VIEW): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True, VIEWER: True},
    (R.ORGANIZATION, A.EDIT): {ORG_ADMIN: True},
    (R.ORGANIZATION, A.MANAGE_MEMBERS): {ORG_ADMIN: True},
    (R.ORGANIZATION, A.CREATE): {},
    (R.ORGANIZATION, A.DELETE): {},
    (R.TEAM, A.VIEW): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True, VIEWER: True},
    (R.TEAM, A.CREATE): {ORG_ADMIN: True, TEAM_ADMIN: True},
    (R.TEAM, A.EDIT): {ORG_ADMIN: True, TEAM_ADMIN: True},
    (R.TEAM, A.MANAGE_MEMBERS): {ORG_ADMIN: True, TEAM_ADMIN: True},
    (R.TEAM, A.DELETE): {ORG_ADMIN: True},
    (R.KEY, A.VIEW): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True, VIEWER: True},
    (R.KEY, A.CREATE): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: OWN},
    (R.KEY, A.EDIT): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: OWN},
    (R.KEY, A.REVOKE): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: OWN},
    (R.KEY, A.DELETE): {ORG_ADMIN: True},
    (R.KEY, A.USE): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True},
    (R.BUDGET, A.VIEW): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True, VIEWER: True},
    (R.BUDGET, A.CREATE): {ORG_ADMIN: True},
    (R.BUDGET, A.EDIT): {ORG_ADMIN: True},
    (R.BUDGET, A.OVERRIDE): {ORG_ADMIN: True},
    (R.MODEL, A.VIEW): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True, VIEWER: True},
    (R.MODEL, A.USE): {ORG_ADMIN: True, TEAM_ADMIN: True, TEAM_MEMBER: True},
    (R.MODEL, A.CREATE): {ORG_ADMIN: True},
    (R.MODEL, A.EDIT): {ORG_ADMIN: True},
    (R.MODEL, A.DELETE): {ORG_ADMIN: True},
    (R.MODEL, A.ASSIGN): {ORG_ADMIN: True},
    (R.MODEL, A.RESTRICT): {ORG_ADMIN: True},
}


def _actor(
    org_roles: frozenset[TenantRole] = frozenset(),
    team_roles: frozenset[TenantRole] = frozenset(),
    *,
    team_org: str = ORG,
    is_super_admin: bool = False,
    is_active: bool = True,
) -> Actor:
    return Actor(
        user_id=USER,
        is_super_admin=is_super_admin,
        is_active=is_active,
        organization_roles=MappingProxyType({ORG: org_roles} if org_roles else {}),
        team_memberships=MappingProxyType(
            {TEAM: TeamMembership(organization_id=team_org, roles=team_roles)} if team_roles else {}
        ),
    )


def _actor_with_role(role: TenantRole) -> Actor:
    if role in (TEAM_ADMIN, TEAM_MEMBER):
        return _actor(team_roles=frozenset({role}))
    return _actor(org_roles=frozenset({role}))


def _resource(
    resource_type: ResourceType, *, organization_id: str = ORG, team_id: str | None = TEAM, owner: str | None = None
) -> Resource:
    team: Final = None if resource_type is R.ORGANIZATION else team_id
    return Resource(resource_type=resource_type, organization_id=organization_id, team_id=team, owner_user_id=owner)


MATRIX_CASES: Final = [
    pytest.param(
        resource_type,
        action,
        role,
        SPEC_MATRIX[(resource_type, action)].get(role, False),
        id=f"{resource_type}-{action}-{role}",
    )
    for (resource_type, action) in SPEC_MATRIX
    for role in TenantRole
]


@pytest.mark.parametrize(("resource_type", "action", "role", "expected"), MATRIX_CASES)
def test_permission_matrix_matches_spec(
    resource_type: ResourceType, action: Action, role: TenantRole, expected: bool | str
) -> None:
    actor: Final = _actor_with_role(role)

    own_resource: Final = authorize(actor, action, _resource(resource_type, owner=USER))
    someone_elses: Final = authorize(actor, action, _resource(resource_type, owner="someone-else"))

    match expected:
        case True:
            assert own_resource == Allowed()
            assert someone_elses == Allowed()
        case "own":
            assert own_resource == Allowed()
            assert someone_elses == Denied(DenialReason.NOT_OWNER)
        case _:
            assert own_resource == Denied(DenialReason.NOT_PERMITTED)
            assert someone_elses == Denied(DenialReason.NOT_PERMITTED)


@pytest.mark.parametrize(("resource_type", "action"), list(SPEC_MATRIX))
def test_super_admin_can_do_everything(resource_type: ResourceType, action: Action) -> None:
    assert authorize(_actor(is_super_admin=True), action, _resource(resource_type)) == Allowed()


@pytest.mark.parametrize("role", list(TenantRole))
@pytest.mark.parametrize("action", [A.VIEW, A.EDIT, A.DELETE, A.USE])
@pytest.mark.parametrize("resource_type", list(ResourceType))
def test_no_role_reaches_another_organization(resource_type: ResourceType, action: Action, role: TenantRole) -> None:
    resource: Final = _resource(resource_type, organization_id=OTHER_ORG)

    assert authorize(_actor_with_role(role), action, resource) == Denied(DenialReason.NOT_A_MEMBER)


def test_team_role_does_not_cross_into_a_sibling_team() -> None:
    team_admin: Final = _actor(team_roles=frozenset({TEAM_ADMIN}))

    assert authorize(team_admin, A.EDIT, _resource(R.TEAM, team_id=TEAM)) == Allowed()
    assert authorize(team_admin, A.EDIT, _resource(R.TEAM, team_id=OTHER_TEAM)) == Denied(DenialReason.NOT_A_MEMBER)


def test_team_role_counts_only_inside_the_teams_own_organization() -> None:
    misbound: Final = _actor(team_roles=frozenset({TEAM_ADMIN}), team_org=OTHER_ORG)

    assert authorize(misbound, A.VIEW, _resource(R.ORGANIZATION)) == Denied(DenialReason.NOT_A_MEMBER)


def test_team_role_grants_org_level_actions_inside_its_organization() -> None:
    team_admin: Final = _actor(team_roles=frozenset({TEAM_ADMIN}))

    assert authorize(team_admin, A.CREATE, _resource(R.TEAM, team_id=None)) == Allowed()


def test_strongest_role_wins_when_actor_holds_several() -> None:
    viewer_and_member: Final = _actor(org_roles=frozenset({VIEWER}), team_roles=frozenset({TEAM_MEMBER}))

    assert authorize(viewer_and_member, A.USE, _resource(R.MODEL)) == Allowed()


def test_personal_scope_requires_a_known_owner() -> None:
    member: Final = _actor(team_roles=frozenset({TEAM_MEMBER}))

    assert authorize(member, A.CREATE, _resource(R.KEY, owner=None)) == Denied(DenialReason.NOT_OWNER)


@pytest.mark.parametrize("is_super_admin", [True, False])
def test_inactive_actor_is_denied_even_as_super_admin(is_super_admin: bool) -> None:
    inactive: Final = _actor(org_roles=ALL_ROLES, is_super_admin=is_super_admin, is_active=False)

    assert authorize(inactive, A.VIEW, _resource(R.ORGANIZATION)) == Denied(DenialReason.INACTIVE_ACTOR)


def test_raise_for_decision_maps_denial_to_403() -> None:
    resource: Final = _resource(R.ORGANIZATION)

    raise_for_decision(Allowed(), A.DELETE, resource)
    with pytest.raises(HTTPException) as exc_info:
        raise_for_decision(Denied(DenialReason.NOT_PERMITTED), A.DELETE, resource)

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == {"error": "Not allowed to delete this organization.", "reason": "not_permitted"}
