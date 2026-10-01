from types import MappingProxyType
from typing import Final

import pytest
from fastapi import HTTPException

from agami.auth.context import Actor
from agami.auth.model_ownership import (
    OrgModelWriteDecision,
    OwnershipChangeRejected,
    UnknownOrganization,
    authorize_org_model_write,
    raise_for_org_model_write,
)
from agami.auth.permissions import Action
from agami.auth.roles import TenantRole
from agami.auth.service import Allowed, Denied, DenialReason

ORG: Final = "org-a"
OTHER_ORG: Final = "org-b"
WRITE_ACTIONS: Final = (Action.CREATE, Action.EDIT, Action.DELETE)


def _actor(org_roles: dict[str, frozenset[TenantRole]], *, is_super_admin: bool = False) -> Actor:
    return Actor(
        user_id="user-1",
        is_super_admin=is_super_admin,
        is_active=True,
        organization_roles=MappingProxyType(org_roles),
        team_memberships=MappingProxyType({}),
    )


ORG_ADMIN: Final = _actor({ORG: frozenset({TenantRole.ORG_ADMIN})})
ORG_VIEWER: Final = _actor({ORG: frozenset({TenantRole.VIEWER})})
SUPER_ADMIN: Final = _actor({}, is_super_admin=True)


async def _exists(organization_id: str) -> bool:
    return organization_id in (ORG, OTHER_ORG)


async def _decide(
    actor: Actor, action: Action, stored: str | None, incoming: str | None = None
) -> OrgModelWriteDecision:
    return await authorize_org_model_write(
        actor=actor,
        action=action,
        stored_organization_id=stored,
        incoming_organization_id=incoming,
        organization_exists=_exists,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("action", WRITE_ACTIONS)
async def test_org_admin_manages_models_in_their_own_organization(action: Action) -> None:
    assert await _decide(ORG_ADMIN, action, ORG) == Allowed()


@pytest.mark.asyncio
@pytest.mark.parametrize("action", WRITE_ACTIONS)
async def test_org_admin_cannot_touch_another_organizations_models(action: Action) -> None:
    assert await _decide(ORG_ADMIN, action, OTHER_ORG) == Denied(DenialReason.NOT_A_MEMBER)


@pytest.mark.asyncio
@pytest.mark.parametrize("action", WRITE_ACTIONS)
async def test_viewer_cannot_write_models(action: Action) -> None:
    assert await _decide(ORG_VIEWER, action, ORG) == Denied(DenialReason.NOT_PERMITTED)


@pytest.mark.asyncio
async def test_creating_in_an_unknown_organization_is_rejected_even_for_super_admin() -> None:
    assert await _decide(SUPER_ADMIN, Action.CREATE, "ghost") == UnknownOrganization("ghost")


@pytest.mark.asyncio
@pytest.mark.parametrize(("stored", "incoming"), [(ORG, OTHER_ORG), (None, ORG)])
@pytest.mark.parametrize("actor", [ORG_ADMIN, SUPER_ADMIN])
async def test_an_update_cannot_move_a_model_between_owners(actor: Actor, stored: str | None, incoming: str) -> None:
    assert await _decide(actor, Action.EDIT, stored, incoming) == OwnershipChangeRejected()


@pytest.mark.asyncio
async def test_an_update_restating_the_same_organization_is_authorized_normally() -> None:
    assert await _decide(ORG_ADMIN, Action.EDIT, ORG, ORG) == Allowed()
    assert await _decide(ORG_VIEWER, Action.EDIT, ORG, ORG) == Denied(DenialReason.NOT_PERMITTED)


def test_allowed_does_not_raise() -> None:
    raise_for_org_model_write(Allowed(), Action.CREATE, ORG)


@pytest.mark.parametrize(
    ("decision", "status_code", "needle"),
    [
        (Denied(DenialReason.NOT_A_MEMBER), 403, "not_a_member"),
        (UnknownOrganization("ghost"), 400, "ghost"),
        (OwnershipChangeRejected(), 400, "cannot be changed"),
    ],
)
def test_rejections_map_to_http_errors(decision: OrgModelWriteDecision, status_code: int, needle: str) -> None:
    with pytest.raises(HTTPException) as exc_info:
        raise_for_org_model_write(decision, Action.EDIT, ORG)

    assert exc_info.value.status_code == status_code
    assert needle in str(exc_info.value.detail)
