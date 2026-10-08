from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from fastapi import HTTPException

from litellm.caching.caching import DualCache
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.proxy._types import CommonProxyErrors, UserAPIKeyAuth
from litellm.proxy.auth.entitlements import LicenseFeature, is_licensed
from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import ContentFilterGuardrail
from litellm.types.guardrails import BlockedWord, ContentFilterAction, GuardrailEventHooks
from litellm.types.utils import CallTypesLiteral

REQUEST_AND_RESPONSE_HOOKS: Final = (GuardrailEventHooks.pre_call, GuardrailEventHooks.post_call)


def require_guardrails_licence(callback: str) -> None:
    if is_licensed(LicenseFeature.GUARDRAILS):
        return
    raise ValueError(
        f"The '{callback}' callback needs the 'guardrails' feature on the Agami license. "
        f"{CommonProxyErrors.not_premium_user.value}"
    )


def _raw_entries(setting: str, value: object) -> Sequence[object]:
    if isinstance(value, str):
        return Path(value).read_text(encoding="utf-8").splitlines()
    if isinstance(value, Sequence):
        return value
    raise ValueError(f"Set litellm_settings.{setting} to a list, or to a .txt file with one entry per line")


def load_setting_entries(setting: str, value: object) -> tuple[str, ...]:
    cleaned: Final = tuple(
        entry.strip() for entry in _raw_entries(setting, value) if isinstance(entry, str) and entry.strip()
    )
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
        data: dict,
        call_type: CallTypesLiteral,
    ) -> dict:
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
