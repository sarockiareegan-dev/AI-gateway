from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine, Iterator
from typing import TypeAlias, cast  # noqa: TID251  # @client erases signatures; the aliases restore return types

from litellm import main
from litellm.llms.anthropic.experimental_pass_through.messages import handler as messages_handler
from litellm.llms.base_llm.ocr.transformation import OCRResponse
from litellm.ocr import main as ocr_main
from litellm.responses import main as responses_main
from litellm.responses.streaming_iterator import BaseResponsesAPIStreamingIterator
from litellm.types.llms.anthropic_messages.anthropic_response import AnthropicMessagesResponse
from litellm.types.llms.openai import ResponsesAPIResponse
from litellm.types.utils import ModelResponse
from litellm.utils import CustomStreamWrapper

__all__ = (
    "acompletion",
    "anthropic_messages",
    "anthropic_messages_handler",
    "aocr",
    "aresponses",
    "completion",
    "ocr",
    "responses",
)

ChatResult: TypeAlias = ModelResponse | CustomStreamWrapper
ResponsesResult: TypeAlias = ResponsesAPIResponse | BaseResponsesAPIStreamingIterator
MessagesResult: TypeAlias = AnthropicMessagesResponse | Iterator[bytes] | AsyncIterator[object]
Completion: TypeAlias = Callable[..., ChatResult | Coroutine[object, object, ChatResult]]
Acompletion: TypeAlias = Callable[..., Awaitable[ChatResult]]
Responses: TypeAlias = Callable[..., ResponsesResult | Coroutine[object, object, ResponsesResult]]
Aresponses: TypeAlias = Callable[..., Awaitable[ResponsesResult]]
MessagesHandler: TypeAlias = Callable[..., MessagesResult | Coroutine[object, object, MessagesResult]]
Amessages: TypeAlias = Callable[..., Awaitable[MessagesResult]]
Ocr: TypeAlias = Callable[..., OCRResponse | Coroutine[object, object, OCRResponse]]
Aocr: TypeAlias = Callable[..., Awaitable[OCRResponse]]

# Not Final: litellm/__init__.py star-imports the untyped originals first and these typed aliases override them.
completion: Completion = cast(Completion, main.completion)  # cast-ok: @client erases it  # rebind-ok: see above
acompletion: Acompletion = cast(Acompletion, main.acompletion)  # cast-ok: @client erases it  # rebind-ok: see above
responses: Responses = cast(Responses, responses_main.responses)  # cast-ok: @client erases it  # rebind-ok: see above
aresponses: Aresponses = cast(  # cast-ok: @client erases it  # rebind-ok: see above
    Aresponses, responses_main.aresponses
)
anthropic_messages_handler: MessagesHandler = cast(  # cast-ok: @client erases it  # rebind-ok: see above
    MessagesHandler, messages_handler.anthropic_messages_handler
)
anthropic_messages: Amessages = cast(  # cast-ok: @client erases it  # rebind-ok: see above
    Amessages, messages_handler.anthropic_messages
)
ocr: Ocr = cast(Ocr, ocr_main.ocr)  # cast-ok: @client erases it  # rebind-ok: see above
aocr: Aocr = cast(Aocr, ocr_main.aocr)  # cast-ok: @client erases it  # rebind-ok: see above
