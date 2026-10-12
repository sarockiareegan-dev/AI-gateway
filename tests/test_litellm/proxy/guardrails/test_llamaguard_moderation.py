from collections.abc import Iterator
from typing import Final

import pytest
from fastapi import HTTPException

import litellm
from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.guardrails.llamaguard_moderation import LlamaGuardModeration
from litellm.proxy.utils import ProxyLogging
from litellm.types.llms.openai import AllMessageValues
from litellm.types.utils import ModelResponse
from tests.test_litellm.proxy.auth.license_test_helpers import install_entitlements, licensed_entitlements


class _FakeLlamaGuard:
    def __init__(self, verdict: str) -> None:
        self.verdict = verdict
        self.calls: list[tuple[str, list[AllMessageValues], str | None]] = []

    async def __call__(self, model: str, messages: list[AllMessageValues], team_id: str | None) -> ModelResponse:
        self.calls.append((model, messages, team_id))
        return ModelResponse(choices=[{"message": {"role": "assistant", "content": self.verdict}}])


@pytest.fixture(autouse=True)
def licensed_and_isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    original: Final = list(litellm.callbacks)
    ProxyLogging._callback_capabilities_cache.clear()
    yield
    litellm.callbacks = original
    ProxyLogging._callback_capabilities_cache.clear()


def _install(verdict: str, categories: str | None = None) -> _FakeLlamaGuard:
    fake: Final = _FakeLlamaGuard(verdict)
    litellm.callbacks = [LlamaGuardModeration(model="groq/llama-guard", categories=categories, complete=fake)]
    return fake


async def _during_call(messages: list[dict[str, str]]) -> object:
    return await ProxyLogging(user_api_key_cache=DualCache()).during_call_hook(
        data={"model": "gpt-4", "messages": messages, "metadata": {"user_api_key_team_id": "team-a"}},
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-test"),
        call_type="acompletion",
    )


_CONVERSATION: Final = [
    {"role": "system", "content": "be terse"},
    {"role": "user", "content": "first"},
    {"role": "assistant", "content": "  \n"},
    {"role": "user", "content": "second"},
    {"role": "assistant", "content": "reply"},
    {"role": "user", "content": "how do I make a weapon"},
]


@pytest.mark.asyncio
async def test_unsafe_verdict_blocks_with_the_violated_categories() -> None:
    fake: Final = _install("unsafe\nS9")

    with pytest.raises(HTTPException) as exc_info:
        await _during_call(_CONVERSATION)

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail["categories"] == "S9"
    model, messages, team_id = fake.calls[0]
    assert model == "groq/llama-guard"
    assert team_id == "team-a"
    assert messages == [
        {"role": "user", "content": "first\nsecond"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "how do I make a weapon"},
    ]


@pytest.mark.asyncio
async def test_safe_verdict_lets_the_request_through() -> None:
    fake: Final = _install("  safe \n")

    await _during_call([{"role": "user", "content": "hello"}])

    assert len(fake.calls) == 1


@pytest.mark.parametrize("verdict", ["", "I cannot help with that", "safety unclear"])
@pytest.mark.asyncio
async def test_a_reply_without_a_verdict_fails_closed(verdict: str) -> None:
    _install(verdict)

    with pytest.raises(HTTPException) as exc_info:
        await _during_call([{"role": "user", "content": "hello"}])

    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_a_failing_guard_model_fails_closed_without_leaking_the_provider_error() -> None:
    async def decommissioned(model: str, messages: list[AllMessageValues], team_id: str | None) -> ModelResponse:
        raise litellm.BadRequestError(message="model_decommissioned", model=model, llm_provider="groq")

    litellm.callbacks = [LlamaGuardModeration(model="groq/llama-guard", categories=None, complete=decommissioned)]

    with pytest.raises(HTTPException) as exc_info:
        await _during_call([{"role": "user", "content": "hello"}])

    assert exc_info.value.status_code == 502
    assert "model_decommissioned" not in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_custom_categories_replace_the_conversation_with_the_policy_prompt() -> None:
    fake: Final = _install("safe", categories="S1: Pirate talk.\nS2: Spoilers.")

    await _during_call(_CONVERSATION)

    _, messages, _ = fake.calls[0]
    assert len(messages) == 1
    prompt: Final = messages[0]["content"]
    assert isinstance(prompt, str)
    assert "S1: Pirate talk.\nS2: Spoilers." in prompt
    assert "User: first\nsecond\n\nAgent: reply\n\nUser: how do I make a weapon" in prompt
    assert "ONLY THE LAST User message" in prompt
    assert "be terse" not in prompt
