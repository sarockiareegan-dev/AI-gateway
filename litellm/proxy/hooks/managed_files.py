"""
Managed files: one unified id for a file, batch or fine-tuning job that lives on several deployments.

Files uploaded with ``target_model_names`` are created on every deployment of those models, and the
caller gets back a single base64 id. The provider ids behind it are kept in
``LiteLLM_ManagedFileTable``; batches and fine-tuning jobs created from such a file are kept in
``LiteLLM_ManagedObjectTable``. Requests that carry a unified id are routed to a deployment that holds
the file, and the id is swapped for that deployment's provider id just before the call.
"""

import base64
import json
import uuid
from collections.abc import Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any, Final, Literal

from fastapi import HTTPException

from agami.routing.org_models import EVERY_MODEL
from litellm._logging import verbose_proxy_logger
from litellm.constants import MAX_FILE_LIST_LIMIT
from litellm.integrations.custom_logger import CustomLogger
from litellm.llms.base_llm.files.transformation import BaseFileEndpoints
from litellm.llms.base_llm.managed_resources.isolation import (
    build_list_page,
    build_owner_filter,
    can_access_resource,
    resolve_resource_owner_id,
)
from litellm.models.managed_files import LiteLLM_ManagedFileTable
from litellm.proxy._types import CommonProxyErrors, ProxyException, UserAPIKeyAuth, user_api_key_has_admin_view
from litellm.proxy.auth.entitlements import LicenseFeature, is_licensed
from litellm.proxy.openai_files_endpoints.common_utils import (
    BATCH_CREATE_HIDDEN_PARAM,
    _is_base64_encoded_unified_file_id,
    add_internal_model_credentials,
    get_batch_id_from_unified_batch_id,
    get_model_id_from_unified_batch_id,
    is_litellm_executed_batch,
    validate_file_list_purpose,
)
from litellm.repositories.table_repositories import ManagedFileRepository, ManagedObjectRepository
from litellm.types.llms.openai import FileListPage, OpenAIFileObject
from litellm.types.utils import LiteLLMBatch, LiteLLMFineTuningJob, SpecialEnums

if TYPE_CHECKING:
    from openai.types.file_deleted import FileDeleted
    from opentelemetry.trace import Span

    from litellm.caching.dual_cache import DualCache
    from litellm.proxy.utils import InternalUsageCache, PrismaClient
    from litellm.router import Router
    from litellm.types.llms.openai import CreateFileRequest, HttpxBinaryResponseContent

_FILE_ID_KEYS: Final = ("input_file_id", "training_file", "validation_file")
_MAX_FILE_ID_SEARCH_DEPTH: Final = 8
_DEFAULT_BATCH_LIST_LIMIT: Final = 20


def _encode(unified_id: str) -> str:
    return base64.urlsafe_b64encode(unified_id.encode()).decode().rstrip("=")


def _unified_field(decoded: str, name: str) -> str | None:
    marker: Final = f";{name},"
    if marker not in decoded:
        return None
    value: Final = decoded.split(marker, 1)[1].split(";", 1)[0]
    return value if value and value != "None" else None


def _json_mapping(value: object) -> dict[str, str]:
    parsed: Final = json.loads(value) if isinstance(value, str) else value
    if not isinstance(parsed, Mapping):
        return {}
    return {str(key): str(val) for key, val in parsed.items()}


def _json_object(value: object) -> dict[str, Any] | None:
    parsed: Final = json.loads(value) if isinstance(value, str) else value
    return dict(parsed) if isinstance(parsed, Mapping) else None


def _file_object_from_row(row: Any) -> OpenAIFileObject | None:
    raw: Final = _json_object(row.file_object)
    if raw is None:
        return None
    file_object: Final = OpenAIFileObject.model_validate(raw)
    file_object.id = row.unified_file_id
    return file_object


def _unified_file_ids_in(value: object, depth: int = 0) -> set[str]:
    if depth > _MAX_FILE_ID_SEARCH_DEPTH:
        return set()
    if isinstance(value, Mapping):
        found: Final = set[str]()
        file_id: Final = value.get("file_id")
        if isinstance(file_id, str) and _is_base64_encoded_unified_file_id(file_id):
            found.add(file_id)
        for child in value.values():
            found.update(_unified_file_ids_in(child, depth + 1))
        return found
    if isinstance(value, list | tuple):
        return set().union(*(_unified_file_ids_in(child, depth + 1) for child in value)) if value else set()
    return set()


def _deployment_model_id(kwargs: Mapping[str, object]) -> str | None:
    for container in (kwargs, kwargs.get("litellm_metadata"), kwargs.get("metadata")):
        if not isinstance(container, Mapping):
            continue
        model_info = container.get("model_info")
        if isinstance(model_info, Mapping) and isinstance(model_info.get("id"), str):
            return model_info["id"]
    return None


def _content_type(create_file_request: Mapping[str, object]) -> str:
    from litellm.proxy.openai_files_endpoints.common_utils import detect_content_type_from_filename

    file: Final = create_file_request.get("file")
    if isinstance(file, tuple):
        if len(file) >= 3 and isinstance(file[2], str) and file[2]:
            return file[2]
        if file and isinstance(file[0], str) and file[0]:
            return detect_content_type_from_filename(file[0])
    return "application/octet-stream"


def _rewind(create_file_request: Mapping[str, object]) -> None:
    file: Final = create_file_request.get("file")
    source: Final = file[1] if isinstance(file, tuple) and len(file) >= 2 else file
    seek: Final = getattr(source, "seek", None)
    if callable(seek):
        seek(0)


def _not_found(kind: str, resource_id: str) -> ProxyException:
    return ProxyException(
        message=f"No managed {kind} found with id '{resource_id}'",
        type="invalid_request_error",
        param="file_id" if kind == "file" else "id",
        code=404,
    )


def _request_tags(data: Mapping[str, object]) -> list[str]:
    for key in ("metadata", "litellm_metadata"):
        bucket = data.get(key)
        if isinstance(bucket, Mapping) and isinstance(bucket.get("tags"), list):
            return [tag for tag in bucket["tags"] if isinstance(tag, str)]
    return []


class _PROXY_LiteLLMManagedFiles(CustomLogger, BaseFileEndpoints):
    def __init__(self, internal_usage_cache: "InternalUsageCache", prisma_client: "PrismaClient") -> None:
        super().__init__()
        self.internal_usage_cache = internal_usage_cache
        self.prisma_client = prisma_client

    @property
    def _files(self) -> Any:
        return ManagedFileRepository(self.prisma_client).table

    @property
    def _objects(self) -> Any:
        return ManagedObjectRepository(self.prisma_client).table

    # ---- ids ----

    def get_unified_batch_id(self, batch_id: str, model_id: str) -> str:
        return _encode(SpecialEnums.LITELLM_MANAGED_BATCH_COMPLETE_STR.value.format(model_id, batch_id))

    def get_unified_generic_response_id(self, response_id: str, model_id: str) -> str:
        return _encode(SpecialEnums.LITELLM_MANAGED_GENERIC_RESPONSE_COMPLETE_STR.value.format(model_id, response_id))

    def get_unified_output_file_id(self, output_file_id: str, model_id: str, model_name: str | None) -> str:
        return _encode(
            SpecialEnums.LITELLM_MANAGED_FILE_COMPLETE_STR.value.format(
                "application/json", str(uuid.uuid4()), model_name or "", output_file_id, model_id
            )
        )

    # ---- managed file rows ----

    async def get_unified_file_id(
        self, file_id: str, litellm_parent_otel_span: object = None
    ) -> LiteLLM_ManagedFileTable | None:
        row: Final = await self._files.find_first(where={"unified_file_id": file_id})
        if row is None:
            return None
        return LiteLLM_ManagedFileTable(
            unified_file_id=row.unified_file_id,
            file_object=_file_object_from_row(row),
            model_mappings=_json_mapping(row.model_mappings),
            flat_model_file_ids=list(row.flat_model_file_ids or []),
            created_by=row.created_by,
            team_id=row.team_id,
            updated_by=row.updated_by,
            storage_backend=row.storage_backend,
            storage_url=row.storage_url,
        )

    async def store_unified_file_id(
        self,
        file_id: str,
        file_object: OpenAIFileObject | None,
        litellm_parent_otel_span: object,
        model_mappings: dict[str, str],
        user_api_key_dict: UserAPIKeyAuth,
    ) -> None:
        row: Final[dict[str, object]] = {
            "unified_file_id": file_id,
            "model_mappings": json.dumps(model_mappings),
            "flat_model_file_ids": list(model_mappings.values()),
            "created_by": resolve_resource_owner_id(user_api_key_dict),
            "team_id": user_api_key_dict.team_id,
            "updated_by": user_api_key_dict.user_id,
        }
        if file_object is not None:
            row["file_object"] = file_object.model_dump_json()
            hidden_params: Final = getattr(file_object, "_hidden_params", None) or {}
            for key in ("storage_backend", "storage_url"):
                if hidden_params.get(key):
                    row[key] = hidden_params[key]
        await self._files.create(data=row)

    async def can_user_call_unified_file_id(self, unified_file_id: str, user_api_key_dict: UserAPIKeyAuth) -> bool:
        row: Final = await self._files.find_first(where={"unified_file_id": unified_file_id})
        if row is None:
            return user_api_key_has_admin_view(user_api_key_dict)
        return can_access_resource(user_api_key_dict, row.created_by, row.team_id)

    # ---- managed object rows (batches, fine-tuning jobs) ----

    async def can_user_call_unified_object_id(self, unified_object_id: str, user_api_key_dict: UserAPIKeyAuth) -> bool:
        row: Final = await self._objects.find_first(where={"unified_object_id": unified_object_id})
        if row is None:
            return user_api_key_has_admin_view(user_api_key_dict)
        return can_access_resource(user_api_key_dict, row.created_by, row.team_id)

    async def store_unified_object_id(
        self,
        unified_object_id: str,
        file_object: Any,
        litellm_parent_otel_span: object | None,
        model_object_id: str,
        file_purpose: Literal["batch", "fine-tune", "response"],
        user_api_key_dict: UserAPIKeyAuth,
        request_tags: Sequence[str] | None = None,
        persist_attribution: bool = False,
        batch_processed: bool = False,
        create_if_missing: bool = False,
    ) -> None:
        status: Final = getattr(file_object, "status", None)
        update: Final[dict[str, object]] = {
            "file_object": file_object.model_dump_json(),
            "updated_by": user_api_key_dict.user_id,
            **({"status": "complete" if status == "completed" else status} if isinstance(status, str) else {}),
            **({"batch_processed": True} if batch_processed else {}),
        }
        attribution: Final[dict[str, object]] = (
            {
                "api_key": user_api_key_dict.token,
                "org_id": user_api_key_dict.org_id,
                "request_tags": json.dumps(list(request_tags or [])),
            }
            if persist_attribution
            else {}
        )
        await self._objects.upsert(
            where={"unified_object_id": unified_object_id},
            data={
                "create": {
                    "unified_object_id": unified_object_id,
                    "model_object_id": model_object_id,
                    "file_purpose": file_purpose,
                    "created_by": resolve_resource_owner_id(user_api_key_dict),
                    "team_id": user_api_key_dict.team_id,
                    **update,
                    **attribution,
                },
                "update": {**update, **attribution},
            },
        )

    async def list_user_batches(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        limit: int | None = None,
        after: str | None = None,
        provider: str | None = None,
        target_model_names: str | None = None,
        llm_router: "Router | None" = None,
    ) -> dict[str, Any]:
        owner_filter: Final = build_owner_filter(user_api_key_dict)
        if owner_filter is None:
            return build_list_page([])
        where: Final[dict[str, object]] = {"file_purpose": "batch", **owner_filter}
        if after:
            cursor: Final = await self._objects.find_first(where={"unified_object_id": after, **owner_filter})
            if cursor is None:
                raise ProxyException(
                    message=f"Invalid 'after': no batch with id '{after}'",
                    type="invalid_request_error",
                    param="after",
                    code=400,
                    openai_code="invalid_value",
                )
            where["created_at"] = {"lt": cursor.created_at}
        page_size: Final = limit or _DEFAULT_BATCH_LIST_LIMIT
        allowed_model_ids: Final = self._model_ids_for(target_model_names, llm_router)
        rows: Final = await self._objects.find_many(where=where, order={"created_at": "desc"})
        batches: Final[list[LiteLLMBatch]] = []
        for row in rows:
            if allowed_model_ids is not None:
                decoded = _is_base64_encoded_unified_file_id(row.unified_object_id)
                if not decoded or get_model_id_from_unified_batch_id(decoded) not in allowed_model_ids:
                    continue
            raw = _json_object(row.file_object)
            if raw is None:
                continue
            batch = LiteLLMBatch.model_validate(raw)
            batch.id = row.unified_object_id
            batches.append(batch)
            if len(batches) > page_size:
                break
        return build_list_page(batches[:page_size], has_more=len(batches) > page_size)

    @staticmethod
    def _model_ids_for(target_model_names: str | None, llm_router: "Router | None") -> frozenset[str] | None:
        if not target_model_names or llm_router is None:
            return None
        return frozenset(
            model_id
            for name in target_model_names.split(",")
            for model_id in llm_router.get_model_ids(model_name=name.strip())
        )

    # ---- /v1/files ----

    async def acreate_file(
        self,
        create_file_request: "CreateFileRequest",
        llm_router: "Router",
        target_model_names_list: list[str],
        litellm_parent_otel_span: "Span",
        user_api_key_dict: UserAPIKeyAuth,
    ) -> OpenAIFileObject:
        if not is_licensed(LicenseFeature.MANAGED_FILES):
            raise ProxyException(
                message="Managed files need the 'managed_files' feature on the Iagami license. "
                + CommonProxyErrors.not_premium_user.value,
                type="auth_error",
                param="target_model_names",
                code=403,
            )
        model_mappings: Final[dict[str, str]] = {}
        first_response: OpenAIFileObject | None = None
        for model_name in target_model_names_list:
            _rewind(create_file_request)
            response = await llm_router.acreate_file(model=model_name, **create_file_request)
            model_mappings.update(response._hidden_params.get("model_file_id_mapping") or {})
            first_response = first_response or response
        if first_response is None:
            raise ProxyException(
                message="target_model_names is empty",
                type="invalid_request_error",
                param="target_model_names",
                code=400,
            )
        unified_file_id: Final = _encode(
            SpecialEnums.LITELLM_MANAGED_FILE_COMPLETE_STR.value.format(
                _content_type(create_file_request),
                str(uuid.uuid4()),
                ",".join(target_model_names_list),
                None,
                None,
            )
        )
        first_response.id = unified_file_id
        await self.store_unified_file_id(
            file_id=unified_file_id,
            file_object=first_response,
            litellm_parent_otel_span=litellm_parent_otel_span,
            model_mappings=model_mappings,
            user_api_key_dict=user_api_key_dict,
        )
        return first_response

    async def afile_retrieve(
        self, file_id: str, litellm_parent_otel_span: "Span | None", llm_router: "Router | None" = None
    ) -> OpenAIFileObject:
        row: Final = await self.get_unified_file_id(file_id)
        if row is None:
            raise _not_found("file", file_id)
        if row.file_object is not None:
            return row.file_object
        if llm_router is None:
            raise _not_found("file", file_id)
        import litellm

        for model_id, provider_file_id in self._routable(row, llm_router):
            credentials = llm_router.get_deployment_credentials_with_provider(model_id=model_id, visibility=EVERY_MODEL)
            if credentials is None:
                continue
            response = await litellm.afile_retrieve(file_id=provider_file_id, **dict(credentials))
            response.id = file_id
            return response
        raise _not_found("file", file_id)

    async def afile_list(
        self,
        purpose: str | None,
        litellm_parent_otel_span: "Span | None",
        user_api_key_dict: UserAPIKeyAuth,
        limit: int | None = None,
        after: str | None = None,
        **data: dict,
    ) -> FileListPage:
        validate_file_list_purpose(purpose)
        owner_filter: Final = build_owner_filter(user_api_key_dict)
        if owner_filter is None:
            return FileListPage()
        where: Final[dict[str, object]] = dict(owner_filter)
        if after:
            cursor: Final = await self._files.find_first(where={"unified_file_id": after, **owner_filter})
            if cursor is None:
                raise ProxyException(
                    message=f"Invalid 'after': no file with id '{after}'",
                    type="invalid_request_error",
                    param="after",
                    code=400,
                    openai_code="invalid_value",
                )
            where["created_at"] = {"lt": cursor.created_at}
        page_size: Final = limit or MAX_FILE_LIST_LIMIT
        rows: Final = await self._files.find_many(where=where, order={"created_at": "desc"})
        files: Final[list[OpenAIFileObject]] = []
        for row in rows:
            file_object = _file_object_from_row(row)
            if file_object is None or (purpose is not None and file_object.purpose != purpose):
                continue
            files.append(file_object)
            if len(files) > page_size:
                break
        page: Final = files[:page_size]
        return FileListPage(
            data=page,
            first_id=page[0].id if page else None,
            last_id=page[-1].id if page else None,
            has_more=len(files) > page_size,
        )

    async def afile_delete(
        self, file_id: str, litellm_parent_otel_span: "Span | None", llm_router: "Router", **data: dict
    ) -> "FileDeleted":
        from openai.types.file_deleted import FileDeleted

        row: Final = await self.get_unified_file_id(file_id)
        if row is None:
            raise _not_found("file", file_id)
        if row.storage_backend and row.storage_url:
            from litellm.llms.base_llm.files.storage_backend_factory import get_storage_backend

            await get_storage_backend(row.storage_backend, prisma_client=self.prisma_client).delete_file(
                row.storage_url
            )
        for model_id, provider_file_id in self._routable(row, llm_router):
            try:
                await llm_router.afile_delete(model=model_id, file_id=provider_file_id)
            except Exception as e:  # noqa: BLE001  # one deployment already missing the file must not block removing the rest
                verbose_proxy_logger.warning(
                    "managed files: deleting %s on deployment %s failed: %s", provider_file_id, model_id, e
                )
        await self._files.delete(where={"unified_file_id": file_id})
        return FileDeleted(id=file_id, deleted=True, object="file")

    async def afile_content(
        self, file_id: str, litellm_parent_otel_span: "Span | None", llm_router: "Router", **data: dict
    ) -> "HttpxBinaryResponseContent":
        row: Final = await self.get_unified_file_id(file_id)
        if row is None:
            raise _not_found("file", file_id)
        for model_id, provider_file_id in self._routable(row, llm_router):
            request: dict[str, object] = {"model": model_id, "file_id": provider_file_id}
            add_internal_model_credentials(
                data=request, llm_router=llm_router, model_id=model_id, visibility=EVERY_MODEL
            )
            return await llm_router.afile_content(**request)
        raise _not_found("file", file_id)

    @staticmethod
    def _routable(row: LiteLLM_ManagedFileTable, llm_router: "Router") -> Iterable[tuple[str, str]]:
        decoded: Final = _is_base64_encoded_unified_file_id(row.unified_file_id)
        output_file_id: Final = _unified_field(decoded, "llm_output_file_id") if decoded else None
        output_model_id: Final = _unified_field(decoded, "llm_output_file_model_id") if decoded else None
        if output_file_id and output_model_id:
            yield output_model_id, output_file_id
        for model_id, provider_file_id in row.model_mappings.items():
            if llm_router.get_deployment(model_id=model_id) is not None:
                yield model_id, provider_file_id

    # ---- request hooks ----

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: "DualCache",
        data: dict,
        call_type: Any,
    ) -> dict:
        call: Final = str(getattr(call_type, "value", call_type))
        if call == "afile_content":
            self._route_output_file(data)
        elif call == "aretrieve_batch":
            self._decode_object_id(data, "batch_id")
        elif call in ("aretrieve_fine_tuning_job", "acancel_fine_tuning_job"):
            self._decode_object_id(data, "fine_tuning_job_id")
        else:
            await self._map_message_file_ids(data, user_api_key_dict)
        return data

    @staticmethod
    def _route_output_file(data: dict) -> None:
        file_id: Final = data.get("file_id")
        decoded: Final = _is_base64_encoded_unified_file_id(file_id) if isinstance(file_id, str) else False
        if not decoded:
            return
        output_file_id: Final = _unified_field(decoded, "llm_output_file_id")
        output_model_id: Final = _unified_field(decoded, "llm_output_file_model_id")
        if output_file_id and output_model_id:
            data["model"] = output_model_id
            data["file_id"] = output_file_id

    @staticmethod
    def _decode_object_id(data: dict, key: str) -> None:
        object_id: Final = data.get(key)
        decoded: Final = _is_base64_encoded_unified_file_id(object_id) if isinstance(object_id, str) else False
        if not decoded or is_litellm_executed_batch(decoded):
            return
        model_id: Final = get_model_id_from_unified_batch_id(decoded)
        if model_id:
            data["model"] = model_id
            data[key] = get_batch_id_from_unified_batch_id(decoded)

    async def _map_message_file_ids(self, data: dict, user_api_key_dict: UserAPIKeyAuth) -> None:
        file_ids: Final = _unified_file_ids_in(data.get("messages")) | _unified_file_ids_in(data.get("input"))
        if not file_ids:
            return
        mapping: Final[dict[str, dict[str, str]]] = {}
        for file_id in sorted(file_ids):
            row = await self.get_unified_file_id(file_id)
            if row is None:
                if not user_api_key_has_admin_view(user_api_key_dict):
                    raise HTTPException(
                        status_code=403, detail="The caller does not have access to this managed file id."
                    )
                continue
            if not can_access_resource(user_api_key_dict, row.created_by, row.team_id):
                raise HTTPException(status_code=403, detail="The caller does not have access to this managed file id.")
            mapping[file_id] = row.model_mappings
        if mapping:
            data["model_file_id_mapping"] = mapping

    async def async_filter_deployments(
        self,
        model: str,
        healthy_deployments: list,
        messages: list | None,
        request_kwargs: dict | None = None,
        parent_otel_span: "Span | None" = None,
    ) -> list[dict]:
        if not request_kwargs:
            return healthy_deployments
        holders: Final[list[frozenset[str]]] = []
        mapping: Final = request_kwargs.get("model_file_id_mapping")
        if isinstance(mapping, Mapping):
            holders.extend(frozenset(models) for models in mapping.values() if isinstance(models, Mapping))
        for key in _FILE_ID_KEYS:
            file_id = request_kwargs.get(key)
            if isinstance(file_id, str) and _is_base64_encoded_unified_file_id(file_id):
                row = await self.get_unified_file_id(file_id)
                if row is not None:
                    holders.append(frozenset(row.model_mappings))
        if not holders:
            return healthy_deployments
        allowed: Final = frozenset.intersection(*holders)
        filtered: Final = [d for d in healthy_deployments if (d.get("model_info") or {}).get("id") in allowed]
        return filtered or healthy_deployments

    async def async_pre_call_deployment_hook(self, kwargs: dict[str, Any], call_type: Any) -> dict | None:
        model_id: Final = _deployment_model_id(kwargs)
        if model_id is None:
            return None
        changed = False
        for key in _FILE_ID_KEYS:
            file_id = kwargs.get(key)
            if not isinstance(file_id, str) or not _is_base64_encoded_unified_file_id(file_id):
                continue
            row = await self.get_unified_file_id(file_id)
            provider_file_id = row.model_mappings.get(model_id) if row is not None else None
            if provider_file_id:
                kwargs[key] = provider_file_id
                changed = True
        return kwargs if changed else None

    async def async_post_call_success_hook(self, data: dict, user_api_key_dict: UserAPIKeyAuth, response: Any) -> Any:
        hidden_params: Final = getattr(response, "_hidden_params", None) or {}
        if isinstance(response, LiteLLMBatch):
            if hidden_params.get(BATCH_CREATE_HIDDEN_PARAM) and hidden_params.get("unified_file_id"):
                await self._mint(response, "batch", data, user_api_key_dict)
            elif isinstance(hidden_params.get("unified_batch_id"), str):
                self._restore_id(response, hidden_params["unified_batch_id"])
        elif isinstance(response, LiteLLMFineTuningJob):
            if hidden_params.get("unified_file_id") and not hidden_params.get("unified_finetuning_job_id"):
                await self._mint(response, "fine-tune", data, user_api_key_dict)
            elif isinstance(hidden_params.get("unified_finetuning_job_id"), str):
                self._restore_id(response, hidden_params["unified_finetuning_job_id"])
        return response

    @staticmethod
    def _restore_id(response: Any, decoded_id: str) -> None:
        if not _is_base64_encoded_unified_file_id(response.id):
            response.id = _encode(decoded_id)

    async def _mint(
        self,
        response: Any,
        purpose: Literal["batch", "fine-tune"],
        data: Mapping[str, object],
        user_api_key_dict: UserAPIKeyAuth,
    ) -> None:
        if _is_base64_encoded_unified_file_id(response.id):
            return
        model_id: Final = response._hidden_params.get("model_id")
        if not isinstance(model_id, str) or not model_id:
            verbose_proxy_logger.warning(
                "managed files: no deployment id on %s %s, leaving it unmanaged", purpose, response.id
            )
            return
        provider_id: Final = response.id
        response.id = (
            self.get_unified_batch_id(provider_id, model_id)
            if purpose == "batch"
            else self.get_unified_generic_response_id(provider_id, model_id)
        )
        await self.store_unified_object_id(
            unified_object_id=response.id,
            file_object=response,
            litellm_parent_otel_span=user_api_key_dict.parent_otel_span,
            model_object_id=provider_id,
            file_purpose=purpose,
            user_api_key_dict=user_api_key_dict,
            request_tags=_request_tags(data),
            persist_attribution=True,
        )
