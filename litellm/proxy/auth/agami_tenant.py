"""Agami authorize() helpers for organization and team management routes."""

from typing import Final

from fastapi import HTTPException, status

from agami.auth.context import Resource
from agami.auth.invite import is_view_only
from agami.auth.permissions import Action, ResourceType
from agami.auth.service import Allowed, authorize, raise_for_decision
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.auth.agami_access import load_request_actor
from litellm.proxy.utils import PrismaClient


async def require_action(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    action: Action,
    *,
    resource_type: ResourceType,
    organization_id: str,
    team_id: str | None = None,
) -> None:
    """Raise 403 when the caller cannot perform ``action`` on the resource under Agami RBAC."""
    actor: Final = await load_request_actor(user_api_key_dict, prisma_client)
    if is_view_only(actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="View-only roles cannot modify organizations or teams",
        )
    resource: Final = Resource(
        resource_type=resource_type,
        organization_id=organization_id,
        team_id=team_id,
    )
    raise_for_decision(authorize(actor, action, resource), action, resource)


async def require_org_create(user_api_key_dict: UserAPIKeyAuth, prisma_client: PrismaClient) -> None:
    """Organization create is proxy-admin only (absent from the tenant matrix)."""
    actor: Final = await load_request_actor(user_api_key_dict, prisma_client)
    if not actor.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a proxy admin can create organizations",
        )


async def require_org_delete(user_api_key_dict: UserAPIKeyAuth, prisma_client: PrismaClient) -> None:
    actor: Final = await load_request_actor(user_api_key_dict, prisma_client)
    if not actor.is_super_admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a proxy admin can delete organizations",
        )


async def require_org_rename(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
) -> None:
    """Org rename (EDIT) is proxy-admin only after Agami matrix tightening."""
    await require_action(
        user_api_key_dict,
        prisma_client,
        Action.EDIT,
        resource_type=ResourceType.ORGANIZATION,
        organization_id=organization_id,
    )


async def require_team_create(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
) -> None:
    """Team create is org-admin (or proxy). Callers with no Agami tenant context yet
    (memberships not loaded on the key) fall through to the route's LiteLLM org checks.
    """
    actor: Final = await load_request_actor(user_api_key_dict, prisma_client)
    if is_view_only(actor):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="View-only roles cannot modify organizations or teams",
        )
    if not actor.is_super_admin and not actor.organization_roles and not actor.team_memberships:
        return
    resource: Final = Resource(
        resource_type=ResourceType.TEAM,
        organization_id=organization_id,
        team_id=None,
    )
    raise_for_decision(authorize(actor, Action.CREATE, resource), Action.CREATE, resource)


async def require_team_edit(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
    team_id: str,
) -> None:
    """Team rename / structural edit — org admin or proxy, not team admin."""
    await require_action(
        user_api_key_dict,
        prisma_client,
        Action.EDIT,
        resource_type=ResourceType.TEAM,
        organization_id=organization_id,
        team_id=team_id,
    )


async def require_team_delete(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
    team_id: str,
) -> None:
    await require_action(
        user_api_key_dict,
        prisma_client,
        Action.DELETE,
        resource_type=ResourceType.TEAM,
        organization_id=organization_id,
        team_id=team_id,
    )


async def require_manage_org_members(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
) -> None:
    await require_action(
        user_api_key_dict,
        prisma_client,
        Action.MANAGE_MEMBERS,
        resource_type=ResourceType.ORGANIZATION,
        organization_id=organization_id,
    )


async def require_manage_team_members(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient,
    organization_id: str,
    team_id: str,
) -> None:
    await require_action(
        user_api_key_dict,
        prisma_client,
        Action.MANAGE_MEMBERS,
        resource_type=ResourceType.TEAM,
        organization_id=organization_id,
        team_id=team_id,
    )


def decision_allowed(actor_decision: object) -> bool:
    return isinstance(actor_decision, Allowed)
