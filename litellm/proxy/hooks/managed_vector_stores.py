"""
Managed vector stores: one unified id for a vector store created on several deployments.

A vector store created with ``target_model_names`` is created once per model, and the caller gets back a
single base64 id. The provider ids behind it are kept in ``LiteLLM_ManagedVectorStoreTable``. Requests
that carry the unified id are checked against its owner and sent to the deployment the id names.
"""

import json
import uuid
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final

from fastapi import HTTPException

from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.base_llm.managed_resources.isolation import can_access_resource, resolve_resource_owner_id
from litellm.llms.base_llm.managed_resources.utils import (
    encode_unified_id,
    generate_unified_id_string,
    parse_unified_id,
)
from litellm.proxy._types import CommonProxyErrors, ProxyException, UserAPIKeyAuth
from litellm.proxy.auth.entitlements import LicenseFeature, is_licensed
from litellm.repositories.table_repositories import ManagedVectorStoreRepository
from litellm.types.vector_stores import VectorStoreCreateResponse

if TYPE_CHECKING:
    from litellm.caching.dual_cache import DualCache
    from litellm.proxy.utils import InternalUsageCache, PrismaClient
    from litellm.router import Router

_RESOURCE_TYPE: Final = "vector_store"
_ROUTING_MESSAGES: Final = ({"role": "user", "content": "vector-store-api-fake-text"},)


def parse_managed_vector_store_id(vector_store_id: object) -> Mapping[str, Any] | None:
    if not isinstance(vector_store_id, str):
        return None
    parsed: Final = parse_unified_id(vector_store_id)
    if parsed is None or parsed.get("resource_type") != _RESOURCE_TYPE:
        return None
    return parsed


def _caller_metadata(user_api_key_dict: UserAPIKeyAuth) -> dict[str, object]:
    return {
        "user_api_key_auth": user_api_key_dict,
        "user_api_key_org_id": user_api_key_dict.org_id,
        "user_api_key_team_id": user_api_key_dict.team_id,
    }


class _PROXY_LiteLLMManagedVectorStores(CustomLogger):
    def __init__(self, internal_usage_cache: "InternalUsageCache", prisma_client: "PrismaClient") -> None:
        super().__init__()
        self.internal_usage_cache = internal_usage_cache
        self.prisma_client = prisma_client

    @property
    def _table(self) -> Any:
        return ManagedVectorStoreRepository(self.prisma_client).table

    async def acreate_vector_store(
        self,
        create_request: Mapping[str, object],
        llm_router: "Router",
        target_model_names_list: Sequence[str],
        litellm_parent_otel_span: object,
        user_api_key_dict: UserAPIKeyAuth,
    ) -> VectorStoreCreateResponse:
        if not is_licensed(LicenseFeature.MANAGED_FILES):
            raise ProxyException(
                message="Managed vector stores need the 'managed_files' feature on the Agami license. "
                + CommonProxyErrors.not_premium_user.value,
                type="auth_error",
                param="target_model_names",
                code=403,
            )
        targets: Final = [name.strip() for name in target_model_names_list if isinstance(name, str) and name.strip()]
        request: Final = {**create_request, "litellm_metadata": _caller_metadata(user_api_key_dict)}
        model_mappings: Final[dict[str, str]] = {}
        first: tuple[str, VectorStoreCreateResponse] | None = None
        for model_name in targets:
            deployment = await llm_router.async_get_available_deployment(
                model=model_name, messages=list(_ROUTING_MESSAGES), request_kwargs=dict(request)
            )
            model_id = deployment["model_info"]["id"]
            response = await llm_router.avector_store_create(model=model_id, **request)
            model_mappings[model_id] = response["id"]
            first = first or (model_id, response)
        if first is None:
            raise ProxyException(
                message="target_model_names is empty",
                type="invalid_request_error",
                param="target_model_names",
                code=400,
            )
        first_model_id, first_response = first
        unified_id: Final = encode_unified_id(
            generate_unified_id_string(
                resource_type=_RESOURCE_TYPE,
                unified_uuid=str(uuid.uuid4()),
                target_model_names=targets,
                provider_resource_id=first_response["id"],
                model_id=first_model_id,
            )
        )
        stored: Final = VectorStoreCreateResponse(**{**first_response, "id": unified_id})
        await self._table.create(
            data={
                "unified_resource_id": unified_id,
                "resource_object": json.dumps(stored),
                "model_mappings": json.dumps(model_mappings),
                "flat_model_resource_ids": list(model_mappings.values()),
                "created_by": resolve_resource_owner_id(user_api_key_dict),
                "team_id": user_api_key_dict.team_id,
                "updated_by": user_api_key_dict.user_id,
            }
        )
        return stored

    async def assert_can_access(self, vector_store_id: str, user_api_key_dict: UserAPIKeyAuth) -> None:
        row: Final = await self._table.find_first(where={"unified_resource_id": vector_store_id})
        if row is None or not can_access_resource(user_api_key_dict, row.created_by, row.team_id):
            raise HTTPException(status_code=404, detail=f"Vector store {vector_store_id} not found")

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: "DualCache",
        data: dict,
        call_type: Any,
    ) -> dict:
        vector_store_id: Final = data.get("vector_store_id")
        parsed: Final = parse_managed_vector_store_id(vector_store_id)
        if parsed is None or not isinstance(vector_store_id, str):
            return data
        await self.assert_can_access(vector_store_id, user_api_key_dict)
        if parsed.get("model_id"):
            data["model"] = parsed["model_id"]
        if parsed.get("provider_resource_id"):
            data["vector_store_id"] = parsed["provider_resource_id"]
        return data
