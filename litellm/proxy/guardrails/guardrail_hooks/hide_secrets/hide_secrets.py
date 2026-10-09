import re
from collections.abc import Mapping
from dataclasses import dataclass
from functools import reduce
from typing import TYPE_CHECKING, Final, Literal, Optional

from pydantic import BaseModel, ConfigDict, TypeAdapter

from litellm.integrations.custom_guardrail import (
    CustomGuardrail,
    log_guardrail_information,  # pyright: ignore[reportUnknownVariableType] # untyped decorator shared by every guardrail hook
)
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.types.guardrails import GuardrailEventHooks, Mode
from litellm.types.proxy.guardrails.guardrail_hooks.hide_secrets import HideSecretsGuardrailConfigModel
from litellm.types.utils import GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

NOT_SECRETS: Final = frozenset({"IPPublicDetector"})
_PATTERNS: Final = TypeAdapter(tuple[re.Pattern[str], ...])


class _DetectorRef(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    name: str


class DetectSecretsConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    plugins_used: tuple[_DetectorRef, ...]


@dataclass(frozen=True, slots=True)
class SecretPattern:
    secret_type: str
    regex: re.Pattern[str]


def load_secret_patterns(detect_secrets_config: Mapping[str, object] | None) -> tuple[SecretPattern, ...]:
    from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
    from detect_secrets.plugins.base import RegexBasedDetector

    detectors: Final = {
        detector.__name__: (secret_type, detector)
        for secret_type, detector in get_mapping_from_secret_type_to_class().items()
        if issubclass(detector, RegexBasedDetector)
    }
    names: Final = (
        tuple(sorted(detectors.keys() - NOT_SECRETS))
        if detect_secrets_config is None
        else tuple(ref.name for ref in DetectSecretsConfig.model_validate(detect_secrets_config).plugins_used)
    )
    unsupported: Final = sorted(name for name in names if name not in detectors)
    if unsupported:
        raise ValueError(
            f"hide-secrets only runs the regex-based detect-secrets plugins, so {unsupported} can't be used. "
            f"Pick from {sorted(detectors)}"
        )
    return tuple(
        SecretPattern(secret_type=detectors[name][0], regex=regex)
        for name in names
        for regex in _PATTERNS.validate_python(
            detectors[name][1]().denylist  # pyright: ignore[reportUnknownMemberType] # detect-secrets ships no type hints
        )
    )


def mask_secrets(text: str, patterns: tuple[SecretPattern, ...]) -> str:
    return reduce(
        lambda masked, pattern: pattern.regex.sub(f"[REDACTED {pattern.secret_type}]", masked),
        patterns,
        text,
    )


class HideSecretsGuardrail(CustomGuardrail):
    def __init__(
        self,
        guardrail_name: str,
        patterns: tuple[SecretPattern, ...],
        event_hook: GuardrailEventHooks | list[GuardrailEventHooks] | Mode | None = GuardrailEventHooks.pre_call,
        default_on: bool = False,
    ) -> None:
        self.patterns: Final = patterns
        super().__init__(
            guardrail_name=guardrail_name,
            supported_event_hooks=self.get_supported_event_hooks(),
            event_hook=event_hook,
            default_on=default_on,
            required_license_feature=LicenseFeature.GUARDRAILS,
        )

    @staticmethod
    def get_config_model() -> type[HideSecretsGuardrailConfigModel] | None:
        return HideSecretsGuardrailConfigModel

    @classmethod
    def get_supported_event_hooks(cls) -> list[GuardrailEventHooks]:
        return [GuardrailEventHooks.pre_call]

    @log_guardrail_information
    async def apply_guardrail(
        self,
        inputs: GenericGuardrailAPIInputs,
        request_data: dict[str, object],
        input_type: Literal["request", "response"],
        logging_obj: Optional["LiteLLMLoggingObj"] = None,
    ) -> GenericGuardrailAPIInputs:
        texts: Final = inputs.get("texts")
        if not texts:
            return inputs
        return {**inputs, "texts": [mask_secrets(text, self.patterns) for text in texts]}
