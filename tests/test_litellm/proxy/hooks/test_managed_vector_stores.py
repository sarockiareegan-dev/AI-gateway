import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from litellm.llms.base_llm.managed_resources.utils import parse_unified_id
from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth
from litellm.proxy.hooks import PROXY_HOOKS
from litellm.proxy.hooks.managed_vector_stores import _PROXY_LiteLLMManagedVectorStores
from litellm.proxy.vector_store_endpoints import utils as vector_store_utils
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)


class _FakeTable:
    def __init__(self) -> None:
        self.rows: list[SimpleNamespace] = []

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        row = SimpleNamespace(**data)
        self.rows.append(row)
        return row

    async def find_first(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return next((row for row in self.rows if row.unified_resource_id == where["unified_resource_id"]), None)


def _hook() -> tuple[_PROXY_LiteLLMManagedVectorStores, _FakeTable]:
    table = _FakeTable()
    prisma_client = SimpleNamespace(db=SimpleNamespace(litellm_managedvectorstoretable=table))
    return _PROXY_LiteLLMManagedVectorStores(internal_usage_cache=MagicMock(), prisma_client=prisma_client), table


def _router() -> MagicMock:
    router = MagicMock()
    router.async_get_available_deployment = AsyncMock(
        side_effect=lambda model, **_: {"model_info": {"id": f"{model}-deployment"}}
    )
    router.avector_store_create = AsyncMock(
        side_effect=lambda model, **_: {"id": f"vs_{model}", "object": "vector_store", "name": "docs"}
    )
    return router


ALICE = UserAPIKeyAuth(user_id="alice", team_id="team-a", api_key="sk-alice")
BOB = UserAPIKeyAuth(user_id="bob", team_id="team-b", api_key="sk-bob")


async def _create(hook: _PROXY_LiteLLMManagedVectorStores, router: MagicMock) -> str:
    response = await hook.acreate_vector_store(
        create_request={"name": "docs"},
        llm_router=router,
        target_model_names_list=["gpt-4o", "gemini"],
        litellm_parent_otel_span=None,
        user_api_key_dict=ALICE,
    )
    return response["id"]


def test_the_hook_is_registered():
    assert PROXY_HOOKS["managed_vector_stores"] is _PROXY_LiteLLMManagedVectorStores


@pytest.mark.asyncio
async def test_create_makes_one_store_per_model_behind_one_owned_id(monkeypatch: pytest.MonkeyPatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("managed_files",)))
    hook, table = _hook()
    router = _router()

    unified_id = await _create(hook, router)

    parsed = parse_unified_id(unified_id)
    assert parsed is not None
    assert parsed["resource_type"] == "vector_store"
    assert parsed["model_id"] == "gpt-4o-deployment"
    assert parsed["provider_resource_id"] == "vs_gpt-4o-deployment"
    assert parsed["target_model_names"] == ["gpt-4o", "gemini"]
    [row] = table.rows
    assert json.loads(row.model_mappings) == {
        "gpt-4o-deployment": "vs_gpt-4o-deployment",
        "gemini-deployment": "vs_gemini-deployment",
    }
    assert (row.created_by, row.team_id) == ("alice", "team-a")
    assert json.loads(row.resource_object)["id"] == unified_id
    sent_metadata = router.avector_store_create.await_args.kwargs["litellm_metadata"]
    assert sent_metadata["user_api_key_team_id"] == "team-a"


@pytest.mark.asyncio
async def test_create_needs_the_managed_files_feature(monkeypatch: pytest.MonkeyPatch):
    install_entitlements(monkeypatch, unlicensed_entitlements())
    hook, table = _hook()

    with pytest.raises(ProxyException) as excinfo:
        await _create(hook, _router())

    assert excinfo.value.code == "403"
    assert table.rows == []


@pytest.mark.asyncio
async def test_pre_call_routes_the_owners_id_to_its_deployment(monkeypatch: pytest.MonkeyPatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("managed_files",)))
    hook, _ = _hook()
    unified_id = await _create(hook, _router())

    data = await hook.async_pre_call_hook(ALICE, MagicMock(), {"vector_store_id": unified_id}, "avector_store_search")

    assert data == {"vector_store_id": "vs_gpt-4o-deployment", "model": "gpt-4o-deployment"}


@pytest.mark.asyncio
async def test_pre_call_hides_another_teams_store(monkeypatch: pytest.MonkeyPatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("managed_files",)))
    hook, _ = _hook()
    unified_id = await _create(hook, _router())

    with pytest.raises(HTTPException) as excinfo:
        await hook.async_pre_call_hook(BOB, MagicMock(), {"vector_store_id": unified_id}, "avector_store_delete")
    admin = UserAPIKeyAuth(user_role=LitellmUserRoles.PROXY_ADMIN, api_key="sk-admin")
    admin_data = await hook.async_pre_call_hook(
        admin, MagicMock(), {"vector_store_id": unified_id}, "avector_store_retrieve"
    )

    assert excinfo.value.status_code == 404
    assert admin_data["vector_store_id"] == "vs_gpt-4o-deployment"


@pytest.mark.asyncio
async def test_pre_call_leaves_provider_ids_alone():
    hook, _ = _hook()
    data = {"vector_store_id": "vs_native"}

    assert await hook.async_pre_call_hook(BOB, MagicMock(), data, "avector_store_search") == {
        "vector_store_id": "vs_native"
    }


@pytest.mark.asyncio
async def test_vector_store_file_routes_check_the_managed_owner(monkeypatch: pytest.MonkeyPatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("managed_files",)))
    hook, _ = _hook()
    unified_id = await _create(hook, _router())
    import litellm.proxy.proxy_server as proxy_server

    monkeypatch.setattr(
        proxy_server, "proxy_logging_obj", SimpleNamespace(get_proxy_hook=lambda name: hook), raising=False
    )

    assert await vector_store_utils.assert_user_can_access_vector_store_id(unified_id, ALICE) is None
    with pytest.raises(HTTPException) as excinfo:
        await vector_store_utils.assert_user_can_access_vector_store_id(unified_id, BOB)
    assert excinfo.value.status_code == 404
