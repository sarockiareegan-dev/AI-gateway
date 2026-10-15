import base64
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from litellm.proxy.openai_files_endpoints.common_utils import batch_cost_poller_is_active
from litellm.proxy.spend_tracking.batch_cost_poller import CheckBatchCost
from litellm.types.utils import LiteLLMBatch, SpecialEnums


def _unified_batch_id(model_id: str, batch_id: str) -> str:
    raw = SpecialEnums.LITELLM_MANAGED_BATCH_COMPLETE_STR.value.format(model_id, batch_id)
    return base64.urlsafe_b64encode(raw.encode()).decode().rstrip("=")


def _row(unified_object_id: str, **attribution):
    return SimpleNamespace(
        unified_object_id=unified_object_id,
        created_by=attribution.get("created_by"),
        team_id=attribution.get("team_id"),
        org_id=attribution.get("org_id"),
        api_key=attribution.get("api_key"),
        request_tags=attribution.get("request_tags", "[]"),
    )


def _batch(status: str, output_file_id: str | None = "file-out") -> LiteLLMBatch:
    batch = LiteLLMBatch(
        id="batch_abc",
        object="batch",
        endpoint="/v1/chat/completions",
        input_file_id="file-in",
        completion_window="24h",
        status=status,
        created_at=0,
        output_file_id=output_file_id,
    )
    batch._hidden_params = {"custom_llm_provider": "openai"}
    return batch


class _FakeTable:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list[tuple[dict, dict]] = []
        self.find_many_calls: list[dict] = []
        self.count = AsyncMock(return_value=len(rows))

    async def find_many(self, **kwargs):
        self.find_many_calls.append(kwargs)
        return self.rows

    async def update(self, where, data):
        self.updates.append((where, data))


@pytest.fixture
def table(monkeypatch):
    fake = _FakeTable([])
    monkeypatch.setattr(
        "litellm.proxy.spend_tracking.batch_cost_poller.ManagedObjectRepository",
        lambda prisma_client: SimpleNamespace(table=fake),
    )
    return fake


def _router(*responses):
    router = MagicMock()
    router.aretrieve_batch = AsyncMock(side_effect=list(responses))
    router.get_deployment_credentials_with_provider.return_value = {"api_key": "sk-deployment"}
    return router


async def _confirmed_poller(router, metrics=None):
    poller = CheckBatchCost(prisma_client=MagicMock(), llm_router=router, metrics=metrics or [MagicMock()])
    assert await poller.confirm_batch_processed_support() is True
    return poller


@pytest.mark.asyncio
async def test_completed_batch_is_retrieved_with_the_creators_attribution_and_retired(table):
    unified_id = _unified_batch_id("deployment-1", "batch_abc")
    table.rows = [
        _row(
            unified_id,
            created_by="user-1",
            team_id="team-1",
            org_id="org-a",
            api_key="hashed-key",
            request_tags='["prod"]',
        )
    ]
    router = _router(_batch("completed"))
    metrics = MagicMock()

    await (await _confirmed_poller(router, [metrics])).check_batch_cost()

    router.aretrieve_batch.assert_awaited_once()
    call = router.aretrieve_batch.await_args.kwargs
    assert (call["model"], call["batch_id"]) == ("deployment-1", "batch_abc")
    assert call["litellm_metadata"] == {
        "user_api_key": "hashed-key",
        "user_api_key_hash": "hashed-key",
        "user_api_key_user_id": "user-1",
        "user_api_key_team_id": "team-1",
        "user_api_key_org_id": "org-a",
        "tags": ["prod"],
    }
    assert dict(call["_litellm_internal_model_credentials"]) == {"api_key": "sk-deployment"}
    assert table.updates == [({"unified_object_id": unified_id}, {"status": "complete", "batch_processed": True})]
    metrics.record_check_batch_cost_run.assert_called_once_with(
        jobs_polled=1, processed_models=[("deployment-1", "openai")]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "batch",
    [
        pytest.param(_batch("in_progress", output_file_id=None), id="still-running"),
        pytest.param(_batch("completed", output_file_id=None), id="completed-before-output-arrives"),
    ],
)
async def test_batch_that_is_not_final_keeps_its_status_and_stays_eligible(table, batch):
    unified_id = _unified_batch_id("deployment-1", "batch_abc")
    table.rows = [_row(unified_id)]

    await (await _confirmed_poller(_router(batch))).check_batch_cost()

    expected_status = "complete" if batch.status == "completed" else batch.status
    assert table.updates == [({"unified_object_id": unified_id}, {"status": expected_status})]


@pytest.mark.asyncio
async def test_rows_without_a_managed_batch_id_are_skipped(table):
    executed = _unified_batch_id("deployment-1", "litellm_batch_local")
    table.rows = [_row("not-a-unified-id"), _row(executed)]
    router = _router()

    await (await _confirmed_poller(router)).check_batch_cost()

    router.aretrieve_batch.assert_not_awaited()
    assert table.updates == []


@pytest.mark.asyncio
async def test_one_failing_batch_does_not_stop_the_poll(table):
    first = _unified_batch_id("deployment-gone", "batch_1")
    second = _unified_batch_id("deployment-1", "batch_2")
    table.rows = [_row(first), _row(second)]
    metrics = MagicMock()

    await (await _confirmed_poller(_router(ConnectionError("down"), _batch("completed")), [metrics])).check_batch_cost()

    assert [where["unified_object_id"] for where, _ in table.updates] == [second]
    metrics.record_check_batch_cost_error.assert_called_once_with("ConnectionError")


@pytest.mark.asyncio
async def test_poller_stays_off_without_the_batch_processed_column(table):
    table.count.side_effect = Exception("Could not find field batch_processed")
    poller = CheckBatchCost(prisma_client=MagicMock(), llm_router=_router(), metrics=[])

    assert await poller.confirm_batch_processed_support() is False
    await poller.check_batch_cost()

    assert table.find_many_calls == []


@pytest.mark.asyncio
async def test_retrieve_path_hands_accounting_to_a_registered_confirmed_poller(table, monkeypatch):
    poller = await _confirmed_poller(_router())
    scheduler = MagicMock()
    scheduler.get_job.side_effect = lambda job_id: (
        SimpleNamespace(func=poller.check_batch_cost) if job_id == "check_batch_cost_job" else None
    )
    monkeypatch.setattr("litellm.proxy.proxy_server.scheduler", scheduler, raising=False)
    monkeypatch.setattr("litellm.constants.PROXY_BATCH_POLLING_ENABLED", True)

    assert batch_cost_poller_is_active() is True

    poller.batch_processed_support_confirmed = False
    assert batch_cost_poller_is_active() is False
