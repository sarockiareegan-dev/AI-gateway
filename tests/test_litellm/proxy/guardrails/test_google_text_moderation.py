import json
from collections.abc import Iterator
from typing import Final

import httpx
import pytest
import respx
from fastapi import HTTPException

import litellm
from litellm.caching.caching import DualCache
from litellm.caching.llm_caching_handler import LLMClientCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.guardrails.google_text_moderation import MODERATE_TEXT_URL, GoogleTextModeration
from litellm.proxy.utils import ProxyLogging
from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements


@pytest.fixture(autouse=True)
def licensed_and_isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    monkeypatch.setattr(litellm, "disable_aiohttp_transport", True)
    monkeypatch.setattr(litellm, "in_memory_llm_clients_cache", LLMClientCache())
    original: Final = list(litellm.callbacks)
    ProxyLogging._callback_capabilities_cache.clear()
    yield
    litellm.callbacks = original
    ProxyLogging._callback_capabilities_cache.clear()


async def _fake_token() -> tuple[str, str]:
    return "ya29.test-token", "agami-project"


def _install(threshold: float) -> None:
    litellm.callbacks = [GoogleTextModeration(confidence_threshold=threshold, access_token=_fake_token)]


async def _during_call() -> object:
    return await ProxyLogging(user_api_key_cache=DualCache()).during_call_hook(
        data={"model": "gpt-4", "messages": [{"role": "user", "content": "you are awful"}], "metadata": {}},
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-test"),
        call_type="acompletion",
    )


def _moderation(toxic: float) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "moderationCategories": [{"name": "Toxic", "confidence": toxic}, {"name": "Insult", "confidence": 0.1}],
            "languageCode": "en",
        },
    )


@pytest.mark.asyncio
@respx.mock
async def test_categories_at_or_above_the_threshold_block_the_request() -> None:
    route: Final = respx.post(MODERATE_TEXT_URL).mock(return_value=_moderation(0.8))
    _install(0.8)

    with pytest.raises(HTTPException) as exc_info:
        await _during_call()

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["categories"] == {"Toxic": 0.8}
    request: Final = route.calls.last.request
    assert request.headers["authorization"] == "Bearer ya29.test-token"
    assert request.headers["x-goog-user-project"] == "agami-project"
    assert json.loads(request.content) == {"document": {"type": "PLAIN_TEXT", "content": "you are awful"}}


@pytest.mark.asyncio
@respx.mock
async def test_categories_below_the_threshold_pass() -> None:
    route: Final = respx.post(MODERATE_TEXT_URL).mock(return_value=_moderation(0.79))
    _install(0.8)

    await _during_call()

    assert route.call_count == 1
