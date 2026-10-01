from dataclasses import dataclass
from enum import StrEnum
from typing import Final, TypeAlias, assert_never

from fastapi import HTTPException, status

from agami.auth.context import Actor, Resource
from agami.auth.permissions import Action
from agami.auth.roles import PERMISSION_MATRIX, Grant, TenantRole


class DenialReason(StrEnum):
    INACTIVE_ACTOR = "inactive_actor"
    NOT_A_MEMBER = "not_a_member"
    NOT_PERMITTED = "not_permitted"
    NOT_OWNER = "not_owner"


@dataclass(frozen=True, slots=True)
class Allowed:
    pass


@dataclass(frozen=True, slots=True)
class Denied:
    reason: DenialReason


Decision: TypeAlias = Allowed | Denied


def effective_roles(actor: Actor, resource: Resource) -> frozenset[TenantRole]:
    """Org roles always apply inside their org; team roles apply only to that team's resources,
    or to org-level resources when the team belongs to the resource's org."""
    org_roles: Final = actor.organization_roles.get(resource.organization_id, frozenset[TenantRole]())
    team_roles: Final = frozenset(
        role
        for team_id, membership in actor.team_memberships.items()
        if membership.organization_id == resource.organization_id
        and (resource.team_id is None or team_id == resource.team_id)
        for role in membership.roles
    )
    return org_roles | team_roles


def authorize(actor: Actor, action: Action, resource: Resource) -> Decision:
    if not actor.is_active:
        return Denied(DenialReason.INACTIVE_ACTOR)
    if actor.is_super_admin:
        return Allowed()
    roles: Final = effective_roles(actor, resource)
    if not roles:
        return Denied(DenialReason.NOT_A_MEMBER)
    role_grants: Final = PERMISSION_MATRIX.get((resource.resource_type, action), {})
    grants: Final = frozenset(role_grants[role] for role in roles if role in role_grants)
    if Grant.ANY in grants:
        return Allowed()
    if Grant.OWN in grants:
        is_owner: Final = resource.owner_user_id is not None and resource.owner_user_id == actor.user_id
        return Allowed() if is_owner else Denied(DenialReason.NOT_OWNER)
    return Denied(DenialReason.NOT_PERMITTED)


def raise_for_decision(decision: Decision, action: Action, resource: Resource) -> None:
    match decision:
        case Allowed():
            return
        case Denied(reason=reason):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": f"Not allowed to {action.value} this {resource.resource_type.value}.",
                    "reason": reason.value,
                },
            )
        case _:
            assert_never(decision)
