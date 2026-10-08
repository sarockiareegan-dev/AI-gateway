from typing import TYPE_CHECKING, Final, Optional

import litellm
from litellm.proxy.guardrails.guardrail_hooks.hide_secrets.hide_secrets import (
    HideSecretsGuardrail,
    load_secret_patterns,
)
from litellm.types.guardrails import GuardrailEventHooks, Mode, SupportedGuardrailIntegrations

if TYPE_CHECKING:
    from litellm import Router
    from litellm.types.guardrails import Guardrail, LitellmParams


def _event_hook(mode: str | list[str] | Mode) -> GuardrailEventHooks | list[GuardrailEventHooks] | Mode:
    if isinstance(mode, str):
        return GuardrailEventHooks(mode)
    if isinstance(mode, list):
        return [GuardrailEventHooks(hook) for hook in mode]
    return mode


def initialize_guardrail(
    litellm_params: "LitellmParams",
    guardrail: "Guardrail",
    llm_router: Optional["Router"] = None,
) -> HideSecretsGuardrail:
    from litellm.proxy.guardrails.legacy_callbacks import require_guardrails_licence

    guardrail_name: Final = guardrail.get("guardrail_name") or SupportedGuardrailIntegrations.HIDE_SECRETS.value
    require_guardrails_licence(guardrail_name)
    callback: Final = HideSecretsGuardrail(
        guardrail_name=guardrail_name,
        patterns=load_secret_patterns(litellm_params.detect_secrets_config),
        event_hook=_event_hook(litellm_params.mode),
        default_on=litellm_params.default_on or False,
    )
    litellm.logging_callback_manager.add_litellm_callback(callback)
    return callback


guardrail_initializer_registry: Final = {
    SupportedGuardrailIntegrations.HIDE_SECRETS.value: initialize_guardrail,
}

guardrail_class_registry: Final = {
    SupportedGuardrailIntegrations.HIDE_SECRETS.value: HideSecretsGuardrail,
}
