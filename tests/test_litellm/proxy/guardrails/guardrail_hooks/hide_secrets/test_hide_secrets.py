from collections.abc import Iterator, Mapping
from typing import Final

import pytest
from fastapi import HTTPException

import litellm
from litellm.caching.caching import DualCache
from litellm.proxy._types import UserAPIKeyAuth
from litellm.proxy.common_utils.callback_utils import initialize_callbacks_on_proxy
from litellm.proxy.guardrails.guardrail_registry import guardrail_initializer_registry
from litellm.proxy.utils import ProxyLogging
from litellm.types.guardrails import LitellmParams
from tests.test_litellm.proxy.auth.license_test_helpers import (
    install_entitlements,
    licensed_entitlements,
    unlicensed_entitlements,
)

AWS_KEY: Final = "AKIAIOSFODNN7EXAMPLE"
GITHUB_TOKEN: Final = "ghp_" + "a1B2c3D4e5" * 3 + "f6G7h8"
PUBLIC_IP: Final = "8.8.8.8"


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


def _add_hide_secrets_guardrail(detect_secrets_config: Mapping[str, object] | None = None) -> None:
    guardrail_initializer_registry["hide-secrets"](
        LitellmParams(
            guardrail="hide-secrets",
            mode="pre_call",
            default_on=True,
            detect_secrets_config=detect_secrets_config,
        ),
        {"guardrail_name": "mask-secrets"},
    )


async def _sent_content(content: str) -> str:
    data: Final = await ProxyLogging(user_api_key_cache=DualCache()).pre_call_hook(
        user_api_key_dict=UserAPIKeyAuth(api_key="sk-test"),
        data={"model": "gpt-4", "messages": [{"role": "user", "content": content}], "metadata": {}},
        call_type="acompletion",
    )
    return data["messages"][0]["content"]


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_secrets_in_the_request_are_masked_and_the_rest_is_kept() -> None:
    _add_hide_secrets_guardrail()

    sent: Final = await _sent_content(f"deploy with {AWS_KEY} and {GITHUB_TOKEN} from {PUBLIC_IP} today")

    assert AWS_KEY not in sent
    assert GITHUB_TOKEN not in sent
    assert "[REDACTED AWS Access Key]" in sent
    assert "[REDACTED GitHub Token]" in sent
    assert sent.startswith("deploy with ")
    assert sent.endswith(f"from {PUBLIC_IP} today")


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_text_without_secrets_is_sent_unchanged() -> None:
    _add_hide_secrets_guardrail()

    assert await _sent_content("what is the weather in Chennai") == "what is the weather in Chennai"


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_detect_secrets_config_limits_which_detectors_run() -> None:
    _add_hide_secrets_guardrail({"plugins_used": [{"name": "AWSKeyDetector"}]})

    sent: Final = await _sent_content(f"{AWS_KEY} {GITHUB_TOKEN}")

    assert AWS_KEY not in sent
    assert GITHUB_TOKEN in sent


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.parametrize("plugin", ["NoSuchDetector", "Base64HighEntropyString", "KeywordDetector"])
def test_detectors_that_cannot_mask_are_rejected_at_startup(plugin: str) -> None:
    with pytest.raises(ValueError, match=plugin):
        _add_hide_secrets_guardrail({"plugins_used": [{"name": plugin}]})

    assert litellm.callbacks == []


@pytest.mark.usefixtures("guardrails_licence")
def test_unknown_detect_secrets_config_keys_are_rejected_at_startup() -> None:
    with pytest.raises(ValueError, match="filters_used"):
        _add_hide_secrets_guardrail({"plugins_used": [{"name": "AWSKeyDetector"}], "filters_used": []})


def test_hide_secrets_guardrail_without_the_guardrails_feature_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("sso",)))

    with pytest.raises(ValueError, match="'mask-secrets' needs the 'guardrails' feature"):
        _add_hide_secrets_guardrail()

    assert litellm.callbacks == []


@pytest.mark.asyncio
async def test_licence_lapsing_after_startup_forbids_requests(monkeypatch: pytest.MonkeyPatch) -> None:
    install_entitlements(monkeypatch, licensed_entitlements(features=("guardrails",)))
    _add_hide_secrets_guardrail()
    install_entitlements(monkeypatch, unlicensed_entitlements())

    with pytest.raises(HTTPException) as exc_info:
        await _sent_content(AWS_KEY)

    assert exc_info.value.status_code == 403


@pytest.mark.usefixtures("guardrails_licence")
@pytest.mark.asyncio
async def test_legacy_hide_secrets_callback_masks_requests() -> None:
    initialize_callbacks_on_proxy(
        value=["hide_secrets"], config_file_path="", litellm_settings={}, callback_specific_params={}
    )

    sent: Final = await _sent_content(f"key {AWS_KEY}")

    assert sent == "key [REDACTED AWS Access Key]"


def test_legacy_hide_secrets_callback_without_the_guardrails_feature_fails_at_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_entitlements(monkeypatch, unlicensed_entitlements())

    with pytest.raises(ValueError, match="'hide_secrets' needs the 'guardrails' feature"):
        initialize_callbacks_on_proxy(
            value=["hide_secrets"], config_file_path="", litellm_settings={}, callback_specific_params={}
        )
