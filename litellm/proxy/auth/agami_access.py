from collections.abc import Iterable, Mapping, Sequence
from typing import Final

from agami.adapters.litellm_compat import load_actor
from agami.auth.context import Actor
from agami.routing.org_models import ModelVisibility, deployment_organization_id
from litellm.proxy._types import LiteLLM_TeamTable, LiteLLM_UserTable, LitellmUserRoles, UserAPIKeyAuth
from litellm.proxy.utils import PrismaClient
from litellm.repositories.team_repository import TeamRepository


async def load_request_actor(user_api_key_dict: UserAPIKeyAuth, prisma_client: PrismaClient) -> Actor:
    from litellm.proxy.auth.auth_checks import get_user_object
    from litellm.proxy.proxy_server import user_api_key_cache
    from litellm.types.proxy.auth.auth_checks import UserNotFoundError

    async def fetch_user(user_id: str) -> LiteLLM_UserTable | None:
        try:
            return await get_user_object(
                user_id=user_id,
                prisma_client=prisma_client,
                user_api_key_cache=user_api_key_cache,
                user_id_upsert=False,
            )
        except UserNotFoundError:
            return None

    async def fetch_teams(team_ids: Sequence[str]) -> Sequence[LiteLLM_TeamTable]:
        rows: Final = await TeamRepository(prisma_client).table.find_many(where={"team_id": {"in": list(team_ids)}})
        return tuple(LiteLLM_TeamTable.model_validate(row.model_dump()) for row in rows)

    return await load_actor(
        user_id=user_api_key_dict.user_id,
        key_user_role=user_api_key_dict.user_role,
        fetch_user=fetch_user,
        fetch_teams=fetch_teams,
    )


async def admin_model_visibility(
    user_api_key_dict: UserAPIKeyAuth,
    prisma_client: PrismaClient | None,
    deployments: Iterable[Mapping[str, object]],
) -> ModelVisibility:
    """Model visibility for admin views. The caller's memberships are only loaded when some deployment is
    organization-owned; otherwise the key's own organization is all the filter can ever need."""
    key_visibility: Final = ModelVisibility.for_key(
        user_api_key_dict.org_id, user_api_key_dict.user_role == LitellmUserRoles.PROXY_ADMIN
    )
    if prisma_client is None or key_visibility.sees_every_organization:
        return key_visibility
    if not any(deployment_organization_id(deployment) is not None for deployment in deployments):
        return key_visibility
    actor: Final = await load_request_actor(user_api_key_dict, prisma_client)
    return ModelVisibility.for_actor(actor, user_api_key_dict.org_id)
