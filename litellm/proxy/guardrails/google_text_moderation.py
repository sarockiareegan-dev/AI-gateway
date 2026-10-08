from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Final, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.llms.custom_httpx.http_handler import get_async_httpx_client, httpxSpecialProvider
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.types.guardrails import GuardrailEventHooks
from litellm.types.utils import GenericGuardrailAPIInputs

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

MODERATE_TEXT_URL: Final = "https://language.googleapis.com/v2/documents:moderateText"
DEFAULT_CONFIDENCE_THRESHOLD: Final = 0.8

AccessTokenFn = Callable[[], Awaitable[tuple[str, str]]]


async def application_default_access_token() -> tuple[str, str]:
    from litellm.llms.vertex_ai.vertex_llm_base import VertexBase

    return await VertexBase().get_access_token_async(credentials=None, project_id=None)


class ModerationCategory(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str
    confidence: float


class ModerateTextResponse(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    categories: tuple[ModerationCategory, ...] = Field(default=(), alias="moderationCategories")


class GoogleTextModeration(CustomGuardrail):
    def __init__(
        self, confidence_threshold: float, access_token: AccessTokenFn = application_default_access_token
    ) -> None:
        self.confidence_threshold: Final = confidence_threshold
        self.access_token: Final = access_token
        self.http: Final = get_async_httpx_client(llm_provider=httpxSpecialProvider.GuardrailCallback)
        super().__init__(
            guardrail_name="google_text_moderation",
            event_hook=GuardrailEventHooks.during_call,
            default_on=True,
            required_license_feature=LicenseFeature.GUARDRAILS,
        )

    async def apply_guardrail(
        self,
        inputs: GenericGuardrailAPIInputs,
        request_data: dict[str, object],
        input_type: Literal["request", "response"],
        logging_obj: Optional["LiteLLMLoggingObj"] = None,
    ) -> GenericGuardrailAPIInputs:
        text: Final = "\n".join(inputs.get("texts") or [])
        if not text.strip():
            return inputs
        token, project = await self.access_token()
        response: Final = await self.http.post(
            url=MODERATE_TEXT_URL,
            headers={"Authorization": f"Bearer {token}", **({"x-goog-user-project": project} if project else {})},
            json={"document": {"type": "PLAIN_TEXT", "content": text}},
        )
        flagged: Final = {
            category.name: category.confidence
            for category in ModerateTextResponse.model_validate(response.json()).categories
            if category.confidence >= self.confidence_threshold
        }
        if not flagged:
            return inputs
        raise HTTPException(
            status_code=400,
            detail={"error": "Violated Google text moderation policy", "categories": flagged},
        )
