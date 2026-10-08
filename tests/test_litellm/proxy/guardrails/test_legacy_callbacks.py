from collections.abc import Iterator
from pathlib import Path
from typing import Final

import pytest
from fastapi import HTTPException

import litellm
from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.callback_utils import initialize_callbacks_on_proxy
from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import ContentFilterGuardrail
from litellm.proxy.guardrails.legacy_callbacks import load_setting_entries
from litellm.proxy.utils import ProxyLogging
from litellm.types.utils import ModelResponse
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)


@pytest.fixture(autouse=True)
def isolated_callbacks() -> Iterator[None]:
    original: Final = list(litellm.callbacks)
    litellm.callbacks = []
    ProxyLogging._callback_capabilities_cache.clear()
    yield
    litellm.callbacks = original
    ProxyLogging._callback_capabilities_cache.clear()


@pytest.fixture
def guardrails_licence(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))


def _register_banned_keywords(keywords: object) -> None:
    initialize_callbacks_on_proxy(
        value=["banned_keywords"],
        config_file_path="",
        litellm_settings={"banned_keywords_list": keywords},
        callback_specific_params={},
    )


def _register_blocked_users(users: object) -> None:
    initialize_callbacks_on_proxy(
        value=["blocked_user_check"],
        config_file_path="",
        litellm_settings={"blocked_user_list": users},
        callback_specific_params={},
    )


async def _pre_call(content: str, user: str | None = None, end_user_id: str | None = None) -> dict:
    return await ProxyLogging(user_api_key_cache=DualCache()).pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-test", end_user_id=end_user_id),
        data={
            "model": "gpt-4",
            "messages": [{"role": "user", "content": content}],
            "metadata": {},
            **({"user": user} if user is not None else {}),
        },
        call_type="acompletion",
    )


async def _post_call(content: str) -> object:
    return await ProxyLogging(user_api_key_cache=DualCache()).post_call_success_hook(
        data={"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}], "metadata": {}},
        response=ModelResponse(choices=[{"message": {"role": "assistant", "content": content}}]),
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-test"),
    )


def test_banned_keywords_without_the_guardrails_feature_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("sso",)))

    with pytest.raises(ValueError, match="'guardrails' feature"):
        _register_banned_keywords(["secret-project"])

    assert litellm.callbacks == []


@pytest.mark.usefixtures("guardrails_licence")
def test_banned_keywords_registers_a_content_filter_guardrail() -> None:
    _register_banned_keywords(["secret-project"])

    assert len(litellm.callbacks) == 1
    guardrail: Final = litellm.callbacks[0]
    assert isinstance(guardrail, ContentFilterGuardrail)
    assert guardrail.guardrail_name == "banned_keywords"


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_banned_keyword_in_the_request_is_blocked_case_insensitively() -> None:
    _register_banned_keywords(["secret-project"])

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("Tell me about SECRET-PROJECT please")

    assert exc_info.value.status_code == 400
    assert "secret-project" in str(exc_info.value.detail).lower()


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_clean_request_passes_through_unchanged() -> None:
    _register_banned_keywords(["secret-project"])

    data: Final = await _pre_call("Tell me about the weather")

    assert data["messages"][0]["content"] == "Tell me about the weather"


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_banned_keyword_in_the_response_is_blocked() -> None:
    _register_banned_keywords(["secret-project"])

    with pytest.raises(HTTPException) as exc_info:
        await _post_call("The secret-project launches tomorrow")

    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_licence_lapsing_after_startup_forbids_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    _register_banned_keywords(["secret-project"])
    install_entitlements(monkeypatch, unlicensed_entitlements())

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("Tell me about the weather")

    assert exc_info.value.status_code == 403
    assert "guardrails" in str(exc_info.value.detail)


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_global_banned_keywords_list_is_used_when_the_setting_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "banned_keywords_list", ["from-global"])
    initialize_callbacks_on_proxy(
        value=["banned_keywords"], config_file_path="", litellm_settings={}, callback_specific_params={}
    )

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("this mentions from-global")

    assert exc_info.value.status_code == 400


def test_entries_load_from_a_text_file_and_skip_blank_lines(tmp_path: Path) -> None:
    path: Final = tmp_path / "banned.txt"
    path.write_text("alpha\n\n  beta  \n", encoding="utf-8")

    assert load_setting_entries("banned_keywords_list", str(path)) == ("alpha", "beta")


@pytest.mark.parametrize("value", [None, [], ["  ", ""]])
def test_missing_or_empty_entries_are_rejected(value: object) -> None:
    with pytest.raises(ValueError, match="banned_keywords_list"):
        load_setting_entries("banned_keywords_list", value)


def test_blocked_user_check_without_the_guardrails_feature_fails_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("sso",)))

    with pytest.raises(ValueError, match="'blocked_user_check' needs the 'guardrails' feature"):
        _register_blocked_users(["mallory"])

    assert litellm.callbacks == []


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.parametrize(
    "user, end_user_id",
    [("mallory", None), (None, "mallory"), ("alice", "mallory"), ("mallory", "alice")],
    ids=["request-user", "auth-end-user", "auth-end-user-wins-over-clean-user", "request-user-with-clean-end-user"],
)
@pytest.mark.asyncio
async def test_blocked_end_user_is_forbidden(user: str | None, end_user_id: str | None) -> None:
    _register_blocked_users(["mallory", "trudy"])

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("hello", user=user, end_user_id=end_user_id)

    assert exc_info.value.status_code == 403
    assert "mallory" in str(exc_info.value.detail)


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.parametrize("user, end_user_id", [("alice", "bob"), (None, None), ("Mallory", None)])
@pytest.mark.asyncio
async def test_unlisted_end_users_pass_through(user: str | None, end_user_id: str | None) -> None:
    _register_blocked_users(["mallory"])

    data: Final = await _pre_call("hello", user=user, end_user_id=end_user_id)

    assert data["messages"][0]["content"] == "hello"


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_global_blocked_user_list_is_used_when_the_setting_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(litellm, "blocked_user_list", ["from-global"])
    initialize_callbacks_on_proxy(
        value=["blocked_user_check"], config_file_path="", litellm_settings={}, callback_specific_params={}
    )

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("hello", user="from-global")

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_blocked_user_check_forbids_requests_once_the_licence_lapses(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    _register_blocked_users(["mallory"])
    install_entitlements(monkeypatch, unlicensed_entitlements())

    with pytest.raises(HTTPException) as exc_info:
        await _pre_call("hello", user="alice")

    assert exc_info.value.status_code == 403
    assert "guardrails" in str(exc_info.value.detail)
