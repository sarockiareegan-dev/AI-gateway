from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Final, TypeAlias, assert_never

from fastapi import HTTPException, status

from agami.auth.context import Actor, Resource
from agami.auth.permissions import Action, ResourceType
from agami.auth.service import Allowed, Denied, authorize, raise_for_decision

OrganizationExists: TypeAlias = Callable[[str], Awaitable[bool]]


@dataclass(frozen=True, slots=True)
class UnknownOrganization:
    organization_id: str


@dataclass(frozen=True, slots=True)
class OwnershipChangeRejected:
    pass


OrgModelWriteDecision: TypeAlias = Allowed | Denied | UnknownOrganization | OwnershipChangeRejected


async def authorize_org_model_write(
    actor: Actor,
    action: Action,
    stored_organization_id: str | None,
    incoming_organization_id: str | None,
    organization_exists: OrganizationExists,
) -> OrgModelWriteDecision:
    """``stored_organization_id`` is the owner on record, or the requested owner when creating."""
    if action is not Action.CREATE and incoming_organization_id not in (None, stored_organization_id):
        return OwnershipChangeRejected()
    if stored_organization_id is None:
        return Allowed()
    if action is Action.CREATE and not await organization_exists(stored_organization_id):
        return UnknownOrganization(stored_organization_id)
    return authorize(actor, action, Resource(ResourceType.MODEL, organization_id=stored_organization_id))


def raise_for_org_model_write(decision: OrgModelWriteDecision, action: Action, organization_id: str | None) -> None:
    match decision:
        case Allowed():
            return
        case Denied():
            raise_for_decision(decision, action, Resource(ResourceType.MODEL, organization_id=organization_id or ""))
        case UnknownOrganization(organization_id=unknown):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"error": f"Organization id={unknown} does not exist."},
            )
        case OwnershipChangeRejected():
            error: Final = (
                "A model's organization cannot be changed. Delete it and create it in the other organization."
            )
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail={"error": error})
        case _:
            assert_never(decision)
