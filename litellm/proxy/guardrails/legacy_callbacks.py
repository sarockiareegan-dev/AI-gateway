from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Annotated, Final, Literal, TypeAlias

from fastapi import HTTPException
from pydantic import Field, TypeAdapter

from litellm.caching.caching import DualCache
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import CommonProxyErrors, UserAPIKeyAuth
from litellm.proxy.auth.entitlements import LicenseFeature, is_licensed
from litellm.proxy.guardrails.google_text_moderation import DEFAULT_CONFIDENCE_THRESHOLD, GoogleTextModeration
from litellm.proxy.guardrails.guardrail_hooks.hide_secrets.hide_secrets import (
    HideSecretsGuardrail,
    load_secret_patterns,
)
from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import ContentFilterGuardrail
from litellm.proxy.guardrails.guardrail_hooks.openai.moderations import OpenAIModerationGuardrail
from litellm.proxy.guardrails.llamaguard_moderation import LlamaGuardModeration
from litellm.proxy.guardrails.llm_guard_moderation import LLMGuardMode, LLMGuardModeration
from litellm.secret_managers.main import get_secret_str
from litellm.types.guardrails import BlockedWord, ContentFilterAction, GuardrailEventHooks
from litellm.types.utils import CallTypesLiteral

REQUEST_AND_RESPONSE_HOOKS: Final = (GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call)
_SEQUENCE: Final = TypeAdapter(Sequence[object])
OpenAIModerationModel: TypeAlias = Literal["omni-moderation-latest", "text-moderation-latest"]
UnitInterval: TypeAlias = Annotated[float, Field(ge=0, le=1)]
_MODERATION_MODEL: Final = TypeAdapter[OpenAIModerationModel | None](OpenAIModerationModel | None)
_LLM_GUARD_MODE: Final = TypeAdapter[LLMGuardMode](LLMGuardMode)
_CONFIDENCE: Final = TypeAdapter[float | None](UnitInterval | None)


def require_guardrails_licence(name: str) -> None:
    if is_licensed(LicenseFeature.GUARDRAILS):
        return
    raise ValueError(
        f"'{name}' needs the 'guardrails' feature on the Agami license. {CommonProxyErrors.not_premium_user.value}"
    )


def _raw_entries(setting: str, value: object) -> Sequence[str]:
    if isinstance(value, str):
        return Path(value).read_text(encoding="utf-8").splitlines()
    if isinstance(value, Sequence):
        return tuple(entry for entry in _SEQUENCE.validate_python(value) if isinstance(entry, str))
    raise ValueError(f"Set litellm_settings.{setting} to a list, or to a .txt file with one entry per line")


def load_setting_entries(setting: str, value: object) -> tuple[str, ...]:
    cleaned: Final = tuple(entry.strip() for entry in _raw_entries(setting, value) if entry.strip())
    if not cleaned:
        raise ValueError(f"litellm_settings.{setting} has no entries")
    return cleaned


class BlockedUserGuardrail(CustomGuardrail):
    def __init__(self, blocked_users: frozenset[str]) -> None:
        self.blocked_users: Final = blocked_users
        super().__init__(
            guardrail_name="blocked_user_check",
            event_hook=GuardrailEventHooks.pre_call,
            default_on=True,
            required_license_feature=LicenseFeature.GUARDRAILS,
        )

    async def async_pre_call_hook(
        self,
        user_api_key_dict: UserAPIKeyAuth,
        cache: DualCache,
        data: dict[str, object],
        call_type: CallTypesLiteral,
    ) -> dict[str, object]:
        request_user: Final = data.get("user")
        candidates: Final = (user_api_key_dict.end_user_id, request_user if isinstance(request_user, str) else None)
        blocked: Final = next((user for user in candidates if user is not None and user in self.blocked_users), None)
        if blocked is None:
            return data
        raise HTTPException(status_code=403, detail={"error": f"End user '{blocked}' is blocked"})


def build_blocked_user_guardrail(litellm_settings: Mapping[str, object], fallback: object) -> BlockedUserGuardrail:
    require_guardrails_licence("blocked_user_check")
    users: Final = load_setting_entries("blocked_user_list", litellm_settings.get("blocked_user_list", fallback))
    return BlockedUserGuardrail(blocked_users=frozenset(users))


def build_hide_secrets_guardrail() -> HideSecretsGuardrail:
    require_guardrails_licence("hide_secrets")
    return HideSecretsGuardrail(guardrail_name="hide_secrets", patterns=load_secret_patterns(None), default_on=True)


def build_openai_moderation_guardrail(
    litellm_settings: Mapping[str, object], fallback: object
) -> OpenAIModerationGuardrail:
    require_guardrails_licence("openai_moderations")
    model: Final = _MODERATION_MODEL.validate_python(litellm_settings.get("openai_moderations_model_name", fallback))
    return OpenAIModerationGuardrail(
        guardrail_name="openai_moderations",
        model=model,
        event_hook=GuardrailEventHooks.during_call,
        default_on=True,
        required_license_feature=LicenseFeature.GUARDRAILS,
    )


def _read_text_file(setting: str, value: object) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Set litellm_settings.{setting} to the path of a text file")
    text: Final = Path(value).read_text(encoding="utf-8")
    if not text.strip():
        raise ValueError(f"litellm_settings.{setting} points to an empty file")
    return text


def build_llamaguard_guardrail(
    litellm_settings: Mapping[str, object], model_fallback: object, categories_fallback: object
) -> LlamaGuardModeration:
    require_guardrails_licence("llamaguard_moderations")
    model: Final = litellm_settings.get("llamaguard_model_name", model_fallback)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("Set litellm_settings.llamaguard_model_name to the Llama Guard model to call")
    categories: Final = litellm_settings.get("llamaguard_unsafe_content_categories", categories_fallback)
    return LlamaGuardModeration(
        model=model,
        categories=None if categories is None else _read_text_file("llamaguard_unsafe_content_categories", categories),
    )


def build_llm_guard_guardrail(litellm_settings: Mapping[str, object], mode_fallback: object) -> LLMGuardModeration:
    require_guardrails_licence("llmguard_moderations")
    api_base: Final = get_secret_str("LLM_GUARD_API_BASE")
    if not api_base:
        raise ValueError("Set LLM_GUARD_API_BASE to the URL of your LLM Guard API server")
    return LLMGuardModeration(
        api_base=api_base,
        api_key=get_secret_str("LLM_GUARD_API_KEY"),
        mode=_LLM_GUARD_MODE.validate_python(litellm_settings.get("llm_guard_mode", mode_fallback)),
    )


def build_google_text_moderation_guardrail(
    litellm_settings: Mapping[str, object], fallback: object
) -> GoogleTextModeration:
    require_guardrails_licence("google_text_moderation")
    threshold: Final = _CONFIDENCE.validate_python(
        litellm_settings.get("google_moderation_confidence_threshold", fallback)
    )
    return GoogleTextModeration(confidence_threshold=DEFAULT_CONFIDENCE_THRESHOLD if threshold is None else threshold)


def build_banned_keywords_guardrail(litellm_settings: Mapping[str, object], fallback: object) -> ContentFilterGuardrail:
    require_guardrails_licence("banned_keywords")
    keywords: Final = load_setting_entries(
        "banned_keywords_list", litellm_settings.get("banned_keywords_list", fallback)
    )
    return ContentFilterGuardrail(
        guardrail_name="banned_keywords",
        blocked_words=[BlockedWord(keyword=keyword, action=ContentFilterAction.BLOCK) for keyword in keywords],
        event_hook=list(REQUEST_AND_RESPONSE_HOOKS),
        default_on=True,
        required_license_feature=LicenseFeature.GUARDRAILS,
    )
