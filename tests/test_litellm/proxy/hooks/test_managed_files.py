import base64
import json
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from litellm.llms.base_llm.files.transformation import BaseFileEndpoints
from litellm.proxy._types import LitellmUserRoles, ProxyException, UserAPIKeyAuth
from litellm.proxy.batches_endpoints.litellm_executed_batches import ManagedBatchStore
from litellm.proxy.hooks import PROXY_HOOKS
from litellm.proxy.hooks.managed_files import _PROXY_LiteLLMManagedFiles
from litellm.proxy.openai_files_endpoints.common_utils import (
    BATCH_CREATE_HIDDEN_PARAM,
    ManagedResourceAccessChecker,
    _is_base64_encoded_unified_file_id,
)
from litellm.types.llms.openai import OpenAIFileObject
from litellm.types.passthrough_endpoints.managed_id_rewriter import ManagedFileIdReader, ManagedFileIdWriter
from litellm.types.utils import LiteLLMBatch, SpecialEnums
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)

_EPOCH = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _matches(row: SimpleNamespace, where: dict[str, Any]) -> bool:
    for key, expected in where.items():
        if key == "OR":
            if not any(_matches(row, clause) for clause in expected):
                return False
        elif isinstance(expected, dict) and "lt" in expected:
            if not getattr(row, key) < expected["lt"]:
                return False
        elif getattr(row, key, None) != expected:
            return False
    return True


class _FakeTable:
    def __init__(self, defaults: dict[str, Any]) -> None:
        self.rows: list[SimpleNamespace] = []
        self.defaults = defaults

    def _new_row(self, data: dict[str, Any]) -> SimpleNamespace:
        row = SimpleNamespace(**{**self.defaults, **data, "created_at": _EPOCH + timedelta(seconds=len(self.rows))})
        self.rows.append(row)
        return row

    async def create(self, data: dict[str, Any]) -> SimpleNamespace:
        return self._new_row(data)

    async def find_first(self, where: dict[str, Any]) -> SimpleNamespace | None:
        return next((row for row in self.rows if _matches(row, where)), None)

    async def find_many(self, where: dict[str, Any], order: dict[str, str] | None = None) -> list[SimpleNamespace]:
        found = [row for row in self.rows if _matches(row, where)]
        return sorted(found, key=lambda row: row.created_at, reverse=True)

    async def upsert(self, where: dict[str, Any], data: dict[str, dict[str, Any]]) -> SimpleNamespace:
        existing = await self.find_first(where)
        if existing is None:
            return self._new_row(data["create"])
        for key, value in data["update"].items():
            setattr(existing, key, value)
        return existing

    async def delete(self, where: dict[str, Any]) -> SimpleNamespace | None:
        existing = await self.find_first(where)
        if existing is not None:
            self.rows.remove(existing)
        return existing


def _file_table() -> _FakeTable:
    return _FakeTable(
        {
            "file_object": None,
            "storage_backend": None,
            "storage_url": None,
            "created_by": None,
            "team_id": None,
            "updated_by": None,
        }
    )


@pytest.fixture
def hook() -> _PROXY_LiteLLMManagedFiles:
    prisma = SimpleNamespace(
        db=SimpleNamespace(
            litellm_managedfiletable=_file_table(),
            litellm_managedobjecttable=_FakeTable({"created_by": None, "team_id": None}),
        )
    )
    return _PROXY_LiteLLMManagedFiles(internal_usage_cache=MagicMock(), prisma_client=prisma)


def _user(user_id: str = "alice", team_id: str | None = None, admin: bool = False) -> UserAPIKeyAuth:
    return UserAPIKeyAuth(
        api_key="sk-test",
        user_id=user_id,
        team_id=team_id,
        user_role=LitellmUserRoles.PROXY_ADMIN if admin else LitellmUserRoles.INTERNAL_USER,
    )


def _file(file_id: str = "file-provider-a", purpose: str = "batch") -> OpenAIFileObject:
    return OpenAIFileObject(
        id=file_id, bytes=3, created_at=1, filename="in.jsonl", object="file", purpose=purpose, status="uploaded"
    )


def _encoded(decoded: str) -> str:
    return base64.urlsafe_b64encode(decoded.encode()).decode().rstrip("=")


async def _stored_file(
    hook: _PROXY_LiteLLMManagedFiles, owner: UserAPIKeyAuth, mappings: dict[str, str] | None = None
) -> str:
    unified = _encoded(
        SpecialEnums.LITELLM_MANAGED_FILE_COMPLETE_STR.value.format(
            "application/jsonl", uuid.uuid4(), "gpt", None, None
        )
    )
    await hook.store_unified_file_id(
        file_id=unified,
        file_object=_file(),
        litellm_parent_otel_span=None,
        model_mappings=mappings or {"deploy-a": "file-provider-a", "deploy-b": "file-provider-b"},
        user_api_key_dict=owner,
    )
    return unified


def _router(*file_responses: OpenAIFileObject) -> MagicMock:
    router = MagicMock()
    router.acreate_file = AsyncMock(side_effect=list(file_responses))
    return router


def test_hook_is_registered_and_satisfies_every_caller_protocol(hook):
    assert PROXY_HOOKS["managed_files"] is _PROXY_LiteLLMManagedFiles
    assert _PROXY_LiteLLMManagedFiles.enforces_request_content is False
    for protocol in (
        BaseFileEndpoints,
        ManagedResourceAccessChecker,
        ManagedFileIdReader,
        ManagedFileIdWriter,
        ManagedBatchStore,
    ):
        assert isinstance(hook, protocol), protocol


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "service",
    [unlicensed_entitlements(), licensed_entitlements(features=("budgets",))],
    ids=["unlicensed", "other_feature_only"],
)
async def test_create_refused_without_the_managed_files_feature(hook, monkeypatch, service):
    install_entitlements(monkeypatch, service)
    router = _router(_file())

    with pytest.raises(ProxyException) as exc:
        await hook.acreate_file(
            create_file_request={"file": ("in.jsonl", b"{}", "application/jsonl"), "purpose": "batch"},
            llm_router=router,
            target_model_names_list=["gpt"],
            litellm_parent_otel_span=None,
            user_api_key_dict=_user(),
        )

    assert exc.value.code == "403"
    router.acreate_file.assert_not_called()


@pytest.mark.asyncio
async def test_create_with_only_managed_files_licensed_stores_one_unified_id_across_models(hook, monkeypatch):
    install_entitlements(monkeypatch, licensed_entitlements(features=("managed_files",)))
    first, second = _file("file-a"), _file("file-b")
    first._hidden_params = {"model_file_id_mapping": {"deploy-a": "file-a"}}
    second._hidden_params = {"model_file_id_mapping": {"deploy-b": "file-b"}}

    response = await hook.acreate_file(
        create_file_request={"file": ("in.jsonl", b"{}", "application/jsonl"), "purpose": "batch"},
        llm_router=_router(first, second),
        target_model_names_list=["gpt", "claude"],
        litellm_parent_otel_span=None,
        user_api_key_dict=_user(team_id="team-1"),
    )

    decoded = _is_base64_encoded_unified_file_id(response.id)
    assert (
        decoded and "target_model_names,gpt,claude" in decoded and decoded.startswith("litellm_proxy:application/jsonl")
    )
    row = await hook.get_unified_file_id(response.id)
    assert row is not None
    assert row.model_mappings == {"deploy-a": "file-a", "deploy-b": "file-b"}
    assert row.created_by == "alice" and row.team_id == "team-1"
    assert row.file_object is not None and row.file_object.id == response.id


@pytest.mark.asyncio
async def test_only_the_owner_team_or_an_admin_may_use_a_managed_file(hook):
    unified = await _stored_file(hook, _user("alice", team_id="team-1"))

    assert await hook.can_user_call_unified_file_id(unified, _user("alice"))
    assert await hook.can_user_call_unified_file_id(unified, _user("bob", team_id="team-1"))
    assert await hook.can_user_call_unified_file_id(unified, _user("root", admin=True))
    assert not await hook.can_user_call_unified_file_id(unified, _user("mallory", team_id="team-2"))
    assert not await hook.can_user_call_unified_file_id("unknown", _user("alice"))


@pytest.mark.asyncio
async def test_chat_request_gets_the_provider_ids_of_its_files(hook):
    unified = await _stored_file(hook, _user("alice"))
    data = {"messages": [{"role": "user", "content": [{"type": "file", "file": {"file_id": unified}}]}]}

    out = await hook.async_pre_call_hook(_user("alice"), MagicMock(), data, "acompletion")

    assert out["model_file_id_mapping"] == {unified: {"deploy-a": "file-provider-a", "deploy-b": "file-provider-b"}}


@pytest.mark.asyncio
async def test_chat_request_with_another_users_file_is_refused(hook):
    unified = await _stored_file(hook, _user("alice"))
    data = {"input": [{"type": "input_file", "file_id": unified}]}

    with pytest.raises(HTTPException) as exc:
        await hook.async_pre_call_hook(_user("mallory"), MagicMock(), data, "aresponses")

    assert exc.value.status_code == 403


@pytest.mark.asyncio
async def test_batch_output_file_content_is_routed_to_its_deployment(hook):
    output_id = _encoded(
        SpecialEnums.LITELLM_MANAGED_FILE_COMPLETE_STR.value.format(
            "application/json", "u", "gpt", "file-out", "deploy-a"
        )
    )
    data = {"file_id": output_id}

    await hook.async_pre_call_hook(_user(), MagicMock(), data, "afile_content")

    assert data == {"file_id": "file-out", "model": "deploy-a"}


@pytest.mark.asyncio
async def test_batch_retrieve_is_decoded_to_its_deployment_and_provider_id(hook):
    batch_id = hook.get_unified_batch_id("batch_123", "deploy-a")
    data = {"batch_id": batch_id}

    await hook.async_pre_call_hook(_user(), MagicMock(), data, "aretrieve_batch")

    assert data == {"batch_id": "batch_123", "model": "deploy-a"}


@pytest.mark.asyncio
async def test_router_keeps_only_deployments_holding_the_file(hook):
    unified = await _stored_file(hook, _user(), {"deploy-b": "file-provider-b"})
    deployments = [{"model_info": {"id": "deploy-a"}}, {"model_info": {"id": "deploy-b"}}]

    kept = await hook.async_filter_deployments("gpt", deployments, None, request_kwargs={"input_file_id": unified})

    assert kept == [{"model_info": {"id": "deploy-b"}}]


@pytest.mark.asyncio
async def test_chosen_deployment_receives_its_own_provider_file_id(hook):
    unified = await _stored_file(hook, _user())
    kwargs = {"input_file_id": unified, "litellm_metadata": {"model_info": {"id": "deploy-b"}}}

    out = await hook.async_pre_call_deployment_hook(kwargs, None)

    assert out is not None and out["input_file_id"] == "file-provider-b"


@pytest.mark.asyncio
async def test_batch_created_from_a_managed_file_gets_a_unified_id_and_a_row(hook):
    unified_file = await _stored_file(hook, _user("alice", team_id="team-1"))
    batch = LiteLLMBatch(
        id="batch_123",
        completion_window="24h",
        created_at=1,
        endpoint="/v1/chat/completions",
        input_file_id=unified_file,
        object="batch",
        status="validating",
    )
    batch._hidden_params = {BATCH_CREATE_HIDDEN_PARAM: True, "unified_file_id": "decoded", "model_id": "deploy-a"}

    out = await hook.async_post_call_success_hook(
        {"metadata": {"tags": ["demo"]}}, _user("alice", team_id="team-1"), batch
    )

    assert out.id == hook.get_unified_batch_id("batch_123", "deploy-a")
    row = hook.prisma_client.db.litellm_managedobjecttable.rows[0]
    assert row.unified_object_id == out.id and row.model_object_id == "batch_123"
    assert row.file_purpose == "batch" and row.created_by == "alice" and row.team_id == "team-1"
    assert json.loads(row.request_tags) == ["demo"]
    assert await hook.can_user_call_unified_object_id(out.id, _user("alice"))
    assert not await hook.can_user_call_unified_object_id(out.id, _user("mallory"))


@pytest.mark.asyncio
async def test_file_list_is_scoped_to_the_caller_and_paginates(hook):
    first = await _stored_file(hook, _user("alice"))
    second = await _stored_file(hook, _user("alice"))
    await _stored_file(hook, _user("mallory"))

    page = await hook.afile_list(
        purpose="batch", litellm_parent_otel_span=None, user_api_key_dict=_user("alice"), limit=1
    )
    assert [f.id for f in page.data] == [second] and page.has_more

    rest = await hook.afile_list(
        purpose="batch", litellm_parent_otel_span=None, user_api_key_dict=_user("alice"), limit=1, after=second
    )
    assert [f.id for f in rest.data] == [first] and not rest.has_more


@pytest.mark.asyncio
async def test_file_list_rejects_a_cursor_the_caller_cannot_see(hook):
    foreign = await _stored_file(hook, _user("mallory"))

    with pytest.raises(ProxyException) as exc:
        await hook.afile_list(
            purpose=None, litellm_parent_otel_span=None, user_api_key_dict=_user("alice"), after=foreign
        )

    assert exc.value.code == "400" and exc.value.param == "after"


@pytest.mark.asyncio
async def test_delete_removes_the_file_from_every_deployment_and_the_row(hook):
    unified = await _stored_file(hook, _user())
    router = MagicMock()
    router.get_deployment = MagicMock(return_value=object())
    router.afile_delete = AsyncMock()

    deleted = await hook.afile_delete(unified, None, router)

    assert deleted.id == unified and deleted.deleted
    assert {call.kwargs["file_id"] for call in router.afile_delete.await_args_list} == {
        "file-provider-a",
        "file-provider-b",
    }
    assert await hook.get_unified_file_id(unified) is None
