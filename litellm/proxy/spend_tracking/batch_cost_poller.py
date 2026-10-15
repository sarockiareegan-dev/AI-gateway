"""Background cost accounting for managed batches.

Every ``check_batch_cost`` run retrieves each managed batch that has not been charged yet.
The retrieve goes through the normal ``aretrieve_batch`` logging path with the creating
key's attribution, so the cost callback prices the output file and writes the batch's
single ``<batch id>_batch_cost`` spend row (the spend writer charges that row once, even
if a user's retrieve races the poller). Once the batch is final the row is retired with
``batch_processed=True``.
"""

import json
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, Protocol, cast

from agami.routing.org_models import EVERY_MODEL
from litellm._logging import verbose_proxy_logger
from litellm.batches.batch_utils import batch_cost_is_final
from litellm.proxy.openai_files_endpoints.common_utils import (
    _is_base64_encoded_unified_file_id,
    add_internal_model_credentials,
    get_batch_id_from_unified_batch_id,
    get_model_id_from_unified_batch_id,
    is_litellm_executed_batch,
)
from litellm.repositories.table_repositories import ManagedObjectRepository

if TYPE_CHECKING:
    from litellm.proxy.utils import PrismaClient
    from litellm.router import Router
    from litellm.types.utils import LiteLLMBatch

BATCH_COST_POLL_LIMIT: Final = 100


class _ManagedBatchRow(Protocol):
    unified_object_id: str
    created_by: str | None
    team_id: str | None
    org_id: str | None
    api_key: str | None
    request_tags: object


class _ManagedObjectTable(Protocol):
    async def update(self, where: Mapping[str, object], data: Mapping[str, object]) -> object: ...


class _BatchCostMetrics(Protocol):
    def record_check_batch_cost_run(
        self, jobs_polled: int, processed_models: list[tuple[str | None, str | None]] | None = None
    ) -> None: ...

    def record_check_batch_cost_error(self, error_type: str) -> None: ...


def _request_tags(raw: object) -> list[str]:
    tags: Final = json.loads(raw) if isinstance(raw, str) else raw
    return [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []


def _attribution_metadata(row: _ManagedBatchRow) -> dict[str, object]:
    return {
        "user_api_key": row.api_key,
        "user_api_key_hash": row.api_key,
        "user_api_key_user_id": row.created_by,
        "user_api_key_team_id": row.team_id,
        "user_api_key_org_id": row.org_id,
        "tags": _request_tags(row.request_tags),
    }


def _batch_cost_metrics() -> Sequence[_BatchCostMetrics]:
    import litellm

    return [cast(_BatchCostMetrics, cb) for cb in litellm.callbacks if hasattr(cb, "record_check_batch_cost_run")]


class CheckBatchCost:
    def __init__(
        self,
        prisma_client: "PrismaClient | None",
        llm_router: "Router",
        metrics: "Sequence[_BatchCostMetrics] | None" = None,
    ) -> None:
        self.prisma_client: Final = prisma_client
        self.llm_router: Final = llm_router
        self._metrics: Final = metrics
        self.batch_processed_support_confirmed = False

    async def confirm_batch_processed_support(self) -> bool:
        """The poller finds and retires batches by ``batch_processed``; without the column it stays off,
        and the retrieve path keeps accounting (see ``batch_cost_poller_is_active``)."""
        if self.prisma_client is None:
            return False
        try:
            await ManagedObjectRepository(self.prisma_client).table.count(where={"batch_processed": False})
        except Exception as e:  # noqa: BLE001  # an old schema raises a Prisma field error; any failure means the poller can't be relied on
            verbose_proxy_logger.warning("Batch cost poller disabled, batch_processed is not readable: %s", e)
            return False
        self.batch_processed_support_confirmed = True
        return True

    async def check_batch_cost(self) -> None:
        if self.prisma_client is None or not self.batch_processed_support_confirmed:
            return
        table: Final = ManagedObjectRepository(self.prisma_client).table
        rows: Final = await table.find_many(
            where={"file_purpose": "batch", "batch_processed": False},
            order={"created_at": "asc"},
            take=BATCH_COST_POLL_LIMIT,
        )
        processed: Final[list[tuple[str | None, str | None]]] = []
        for row in rows:
            try:
                retired = await self._account(row, table)
            except Exception as e:  # noqa: BLE001  # one unreachable deployment must not stop the rest of the poll
                verbose_proxy_logger.warning("Batch cost poller could not check %s: %s", row.unified_object_id, e)
                self._record_error(type(e).__name__)
                continue
            if retired is not None:
                processed.append(retired)
        for metrics in self._metrics if self._metrics is not None else _batch_cost_metrics():
            metrics.record_check_batch_cost_run(jobs_polled=len(rows), processed_models=processed)

    async def _account(
        self, row: _ManagedBatchRow, table: "_ManagedObjectTable"
    ) -> tuple[str | None, str | None] | None:
        decoded: Final = _is_base64_encoded_unified_file_id(row.unified_object_id)
        if not decoded or is_litellm_executed_batch(decoded):
            return None
        model_id: Final = get_model_id_from_unified_batch_id(decoded)
        if not model_id:
            return None
        data: Final[dict[str, object]] = {
            "batch_id": get_batch_id_from_unified_batch_id(decoded),
            "litellm_metadata": _attribution_metadata(row),
        }
        add_internal_model_credentials(data=data, llm_router=self.llm_router, model_id=model_id, visibility=EVERY_MODEL)
        response: Final[LiteLLMBatch] = await self.llm_router.aretrieve_batch(model=model_id, **data)
        final: Final = batch_cost_is_final(response)
        await table.update(
            where={"unified_object_id": row.unified_object_id},
            data={
                "status": "complete" if response.status == "completed" else response.status,
                **({"batch_processed": True} if final else {}),
            },
        )
        if not final:
            return None
        hidden: Final[Mapping[str, object]] = getattr(response, "_hidden_params", None) or {}
        provider: Final = hidden.get("custom_llm_provider")
        return model_id, provider if isinstance(provider, str) else None

    def _record_error(self, error_type: str) -> None:
        for metrics in self._metrics if self._metrics is not None else _batch_cost_metrics():
            metrics.record_check_batch_cost_error(error_type)
