from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from litellm.proxy._types import CommonProxyErrors
from litellm.proxy.auth.entitlements import LicenseFeature, is_licensed
from litellm.proxy.guardrails.guardrail_hooks.litellm_content_filter.content_filter import ContentFilterGuardrail
from litellm.types.guardrails import BlockedWord, ContentFilterAction, GuardrailEventHooks

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
