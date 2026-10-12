from typing import TYPE_CHECKING, Final, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, ValidationError

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.llms.custom_httpx.http_handler import get_async_httpx_client, httpxSpecialProvider
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.types.guardrails import GuardrailEventHooks
from litellm.types.utils import GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

LLMGuardMode = Literal["all", "key-specific", "request-specific"]


class _Permissions(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enable_llm_guard_check: bool = False


class _Metadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    permissions: _Permissions = _Permissions()


class LLMGuardVerdict(BaseModel):
    model_config = ConfigDict(extra="ignore")

    is_valid: bool
    scanners: dict[str, float] = {}


def _opted_in(metadata: object) -> bool:
    try:
        return _Metadata.model_validate(metadata).permissions.enable_llm_guard_check
    except ValidationError:
        return False


class LLMGuardModeration(CustomGuardrail):
    def __init__(self, api_base: str, api_key: str | None, mode: LLMGuardMode) -> None:
        self.api_base: Final = api_base.rstrip("/")
        self.api_key: Final = api_key
        self.mode: Final = mode
        self.http: Final = get_async_httpx_client(llm_provider=httpxSpecialProvider.GuardrailCallback)
        super().__init__(
            guardrail_name="llmguard_moderations",
            event_hook=GuardrailEventHooks.during_call,
            default_on=True,
            required_license_feature=LicenseFeature.GUARDRAILS,
        )

    def _applies_to(self, request_data: dict[str, object]) -> bool:
        match self.mode:
            case "all":
                return True
            case "key-specific":
                return _opted_in(self._get_admin_metadata(request_data))
            case "request-specific":
                return _opted_in(request_data.get("metadata"))

    async def apply_guardrail(
        self,
        inputs: GenericGuardrailAPIInputs,
        request_data: dict[str, object],
        input_type: Literal["request", "response"],
        logging_obj: Optional["LiteLLMLoggingObj"] = None,
    ) -> GenericGuardrailAPIInputs:
        prompt: Final = "\n".join(inputs.get("texts") or ())
        if not prompt.strip() or not self._applies_to(request_data):
            return inputs
        response: Final = await self.http.post(
            url=f"{self.api_base}/analyze/prompt",
            headers={"Authorization": f"Bearer {self.api_key}"} if self.api_key else {},
            json={"prompt": prompt},
        )
        verdict: Final = LLMGuardVerdict.model_validate(response.json())
        if verdict.is_valid:
            return inputs
        raise HTTPException(
            status_code=400,
            detail={"error": "Violated LLM Guard policy", "scanners": verdict.scanners},
        )
