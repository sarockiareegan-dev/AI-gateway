import base64
from collections.abc import Mapping
from types import MappingProxyType
from typing import Final, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from litellm.litellm_core_utils.audio_utils.utils import process_audio_file
from litellm.llms.base_llm.audio_transcription.transformation import (
    AudioTranscriptionRequestData,
    BaseAudioTranscriptionConfig,
)
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.bedrock.base_aws_llm import BaseAWSLLM
from litellm.llms.bedrock.common_utils import BedrockError, get_all_bedrock_regions
from litellm.types.llms.openai import AllMessageValues, OpenAIAudioTranscriptionOptionalParams
from litellm.types.utils import FileTypes, TranscriptionResponse

_SUPPORTED_PARAMS: Final[tuple[OpenAIAudioTranscriptionOptionalParams, ...]] = (
    "language",
    "prompt",
    "temperature",
    "response_format",
)
_AUDIO_FORMATS: Final = MappingProxyType(
    {
        "audio/flac": "flac",
        "audio/mpeg": "mp3",
        "audio/mp3": "mp3",
        "audio/ogg": "ogg",
        "audio/wav": "wav",
        "audio/x-wav": "wav",
    }
)
_SUPPORTED_AUDIO_FORMATS: Final = frozenset(_AUDIO_FORMATS.values())
_MAX_TOKENS: Final = 4096
_TEMPERATURE: Final = TypeAdapter[float | None](float | None)


class _Request(BaseModel):
    model_config = ConfigDict(frozen=True)


class _AudioSource(_Request):
    data: str = Field(serialization_alias="bytes")


class _Audio(_Request):
    format: str
    source: _AudioSource


class _AudioContent(_Request):
    audio: _Audio


class _TextContent(_Request):
    text: str


class _UserMessage(_Request):
    role: Literal["user"] = "user"
    content: tuple[_AudioContent | _TextContent, ...]


class _InferenceConfig(_Request):
    max_tokens: int = Field(serialization_alias="maxTokens")
    temperature: float | None = None


class _ConverseRequest(_Request):
    messages: tuple[_UserMessage, ...]
    system: tuple[_TextContent, ...]
    inference_config: _InferenceConfig = Field(serialization_alias="inferenceConfig")


class _ContentBlock(BaseModel):
    model_config = ConfigDict(extra="ignore")

    text: str | None = None


class _Message(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: tuple[_ContentBlock, ...]


class _Output(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: _Message


class _ConverseResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    output: _Output


def split_region_prefix(model: str) -> tuple[str | None, str]:
    prefix, separator, rest = model.partition("/")
    if separator and prefix in get_all_bedrock_regions():
        return prefix, rest
    return None, model


def _audio_format(audio_file: FileTypes) -> tuple[bytes, str]:
    processed: Final = process_audio_file(audio_file)
    extension: Final = processed.filename.rsplit(".", 1)[-1].lower() if "." in processed.filename else ""
    audio_format: Final = _AUDIO_FORMATS.get(processed.content_type) or extension
    if audio_format not in _SUPPORTED_AUDIO_FORMATS:
        raise ValueError(f"Unsupported Bedrock audio format for file {processed.filename!r}")
    return processed.file_content, audio_format


def _instruction(optional_params: Mapping[str, object]) -> str:
    language: Final = optional_params.get("language")
    prompt: Final = optional_params.get("prompt")
    return "".join(
        (
            "Transcribe the audio. Respond with only the transcript.",
            f" The audio language is {language}." if isinstance(language, str) and language else "",
            f" Additional context: {prompt}" if isinstance(prompt, str) and prompt else "",
        )
    )


class BedrockAudioTranscriptionConfig(BaseAudioTranscriptionConfig, BaseAWSLLM):
    def __init__(self) -> None:
        BaseAudioTranscriptionConfig.__init__(self)
        BaseAWSLLM.__init__(self)

    def get_supported_openai_params(self, model: str) -> list[OpenAIAudioTranscriptionOptionalParams]:
        return list(_SUPPORTED_PARAMS)  # mutable-ok: BaseAudioTranscriptionConfig returns a list

    def map_openai_params(
        self,
        non_default_params: dict[str, object],
        optional_params: dict[str, object],
        model: str,
        drop_params: bool,
    ) -> dict[str, object]:
        supplied: Final = MappingProxyType(
            {key: value for key, value in non_default_params.items() if key in _SUPPORTED_PARAMS}
        )
        return {**optional_params, **supplied}  # mutable-ok: BaseAudioTranscriptionConfig returns a dict

    def validate_environment(
        self,
        headers: dict[str, str],
        model: str,
        messages: list[AllMessageValues],
        optional_params: dict[str, object],
        litellm_params: dict[str, object],
        api_key: str | None = None,
        api_base: str | None = None,
    ) -> dict[str, str]:
        return {
            **headers,
            "Content-Type": "application/json",
        }  # mutable-ok: BaseAudioTranscriptionConfig returns a dict

    def _signing_params(self, model: str, optional_params: Mapping[str, object]) -> dict[str, object]:
        model_region, _ = split_region_prefix(model)
        if optional_params.get("aws_region_name") is not None or model_region is None:
            return dict(optional_params)  # mutable-ok: BaseAWSLLM signing helpers take a dict
        return {
            **optional_params,
            "aws_region_name": model_region,
        }  # mutable-ok: BaseAWSLLM signing helpers take a dict

    def get_complete_url(
        self,
        api_base: str | None,
        api_key: str | None,
        model: str,
        optional_params: dict[str, object],
        litellm_params: dict[str, object],
        stream: bool | None = None,
    ) -> str:
        _, model_id = split_region_prefix(model)
        signing_params: Final = self._signing_params(model, optional_params)
        runtime_endpoint: Final = signing_params.get("aws_bedrock_runtime_endpoint")
        endpoint, _ = self.get_runtime_endpoint(
            api_base=api_base,
            aws_bedrock_runtime_endpoint=runtime_endpoint if isinstance(runtime_endpoint, str) else None,
            aws_region_name=self._get_aws_region_name(optional_params=signing_params, model=model_id),
        )
        return f"{endpoint.rstrip('/')}/model/{self.encode_model_id(model_id)}/converse"

    def transform_audio_transcription_request(
        self,
        model: str,
        audio_file: FileTypes,
        optional_params: dict[str, object],
        litellm_params: dict[str, object],
    ) -> AudioTranscriptionRequestData:
        audio, audio_format = _audio_format(audio_file)
        request: Final = _ConverseRequest(
            messages=(
                _UserMessage(
                    content=(
                        _AudioContent(
                            audio=_Audio(
                                format=audio_format, source=_AudioSource(data=base64.b64encode(audio).decode())
                            )
                        ),
                        _TextContent(text=_instruction(optional_params)),
                    )
                ),
            ),
            system=(_TextContent(text="You are a transcription assistant."),),
            inference_config=_InferenceConfig(
                max_tokens=_MAX_TOKENS, temperature=_TEMPERATURE.validate_python(optional_params.get("temperature"))
            ),
        )
        return AudioTranscriptionRequestData(data=request.model_dump(mode="json", by_alias=True, exclude_none=True))

    def sign_request(
        self,
        headers: dict[str, object],
        optional_params: dict[str, object],
        request_data: dict[str, object],
        api_base: str,
        api_key: str | None = None,
        model: str | None = None,
        stream: bool | None = None,
        fake_stream: bool | None = None,
    ) -> tuple[dict[str, object], bytes | None]:
        _, model_id = split_region_prefix(model or "")
        return self._sign_request(
            service_name="bedrock",
            headers=headers,
            optional_params=self._signing_params(model or "", optional_params),
            request_data=request_data,
            api_base=api_base,
            model=model_id,
            api_key=api_key,
        )

    def transform_audio_transcription_response(self, raw_response: httpx.Response) -> TranscriptionResponse:
        parsed: Final = _ConverseResponse.model_validate_json(raw_response.content)
        text: Final = "".join(block.text for block in parsed.output.message.content if block.text is not None)
        return TranscriptionResponse(text=text)

    def get_error_class(
        self, error_message: str, status_code: int, headers: dict[str, object] | httpx.Headers
    ) -> BaseLLMException:
        return BedrockError(status_code=status_code, message=error_message, headers=headers)
