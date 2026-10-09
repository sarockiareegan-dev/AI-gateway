from collections.abc import Awaitable, Callable
from itertools import groupby
from typing import TYPE_CHECKING, Final, Literal, Optional

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict

from litellm._logging import verbose_proxy_logger
from litellm.integrations.custom_guardrail import CustomGuardrail
from litellm.litellm_core_utils.prompt_templates.common_utils import convert_content_list_to_str
from litellm.proxy._types import ProxyErrorDetail
from litellm.proxy.auth.entitlements import LicenseFeature
from litellm.types.guardrails import GuardrailEventHooks
from litellm.types.llms.openai import AllMessageValues, ChatCompletionAssistantMessage, ChatCompletionUserMessage
from litellm.types.utils import Choices, GenericGuardrailAPIInputs, ModelResponse

if TYPE_CHECKING:
    from litellm.litellm_core_utils.litellm_logging import Logging as LiteLLMLoggingObj

Turn = tuple[Literal["user", "assistant"], str]


class _CallerMetadata(BaseModel):
    model_config = ConfigDict(extra="ignore")

    user_api_key_team_id: str | None = None


CompleteFn = Callable[[str, list[AllMessageValues], str | None], Awaitable[ModelResponse]]


async def complete_via_proxy(model: str, messages: list[AllMessageValues], team_id: str | None) -> ModelResponse:
    from litellm.litellm_core_utils.llm_judge import judge_acompletion
    from litellm.proxy.proxy_server import llm_router

    return await judge_acompletion(llm_router, model, messages, team_id=team_id, max_tokens=20)


def _turn(role: Literal["user", "assistant"], text: str) -> Turn:
    return (role, text)


def _message(turn: Turn) -> AllMessageValues:
    role, text = turn
    if role == "user":
        return ChatCompletionUserMessage(role="user", content=text)
    return ChatCompletionAssistantMessage(role="assistant", content=text)


def conversation_turns(inputs: GenericGuardrailAPIInputs) -> tuple[Turn, ...]:
    structured: Final = inputs.get("structured_messages") or ()
    raw: Final = tuple(
        _turn("user" if message["role"] == "user" else "assistant", convert_content_list_to_str(message))
        for message in structured
        if message["role"] in ("user", "assistant")
    ) or tuple(_turn("user", text) for text in inputs.get("texts") or ())
    return tuple(
        _turn(role, "\n".join(text for _, text in turns))
        for role, turns in groupby((turn for turn in raw if turn[1].strip()), key=_role)
    )


def _role(turn: Turn) -> Literal["user", "assistant"]:
    return turn[0]


def custom_categories_prompt(categories: str, turns: tuple[Turn, ...]) -> str:
    judged: Final = "Agent" if turns[-1][0] == "assistant" else "User"
    conversation: Final = "\n\n".join(f"{'User' if role == 'user' else 'Agent'}: {text}" for role, text in turns)
    return (
        f"Task: Check if there is unsafe content in '{judged}' messages in conversations according our safety "
        "policy with the below categories.\n\n"
        f"<BEGIN UNSAFE CONTENT CATEGORIES>\n{categories.strip()}\n<END UNSAFE CONTENT CATEGORIES>\n\n"
        f"<BEGIN CONVERSATION>\n\n{conversation}\n\n<END CONVERSATION>\n\n"
        f"Provide your safety assessment for ONLY THE LAST {judged} message in the above conversation:\n"
        "- First line must read 'safe' or 'unsafe'.\n"
        "- If unsafe, a second line must include a comma-separated list of violated categories."
    )


def raise_on_unsafe_verdict(reply: ModelResponse) -> None:
    choice: Final = reply.choices[0] if reply.choices else None
    content: Final = choice.message.content if isinstance(choice, Choices) else None
    lines: Final = tuple(line.strip() for line in (content or "").splitlines() if line.strip())
    verdict: Final = lines[0].lower() if lines else ""
    if verdict == "safe":
        return
    if verdict == "unsafe":
        raise HTTPException(
            status_code=400,
            detail={"error": "Violated content safety policy", "categories": lines[1] if len(lines) > 1 else ""},
        )
    raise HTTPException(
        status_code=502, detail=ProxyErrorDetail(error="Llama Guard returned no safe or unsafe verdict")
    )


class LlamaGuardModeration(CustomGuardrail):
    def __init__(self, model: str, categories: str | None, complete: CompleteFn = complete_via_proxy) -> None:
        self.model: Final = model
        self.categories: Final = categories
        self.complete: Final = complete
        super().__init__(
            guardrail_name="llamaguard_moderations",
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
        turns: Final = conversation_turns(inputs)
        if not turns:
            return inputs
        messages: Final = (
            [_message(turn) for turn in turns]
            if self.categories is None
            else [_message(_turn("user", custom_categories_prompt(self.categories, turns)))]
        )
        metadata: Final = request_data.get("metadata")
        team_id: Final = (
            _CallerMetadata.model_validate(metadata).user_api_key_team_id if isinstance(metadata, dict) else None
        )
        try:
            reply: Final = await self.complete(self.model, messages, team_id)
        except Exception as e:
            verbose_proxy_logger.exception("Llama Guard call to %s failed", self.model)
            raise HTTPException(status_code=502, detail=ProxyErrorDetail(error="Llama Guard check failed")) from e
        raise_on_unsafe_verdict(reply)
        return inputs
