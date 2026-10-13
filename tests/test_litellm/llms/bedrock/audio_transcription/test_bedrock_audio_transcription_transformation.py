import base64
import json
from collections.abc import AsyncGenerator
from typing import Final
from unittest.mock import Mock

import httpx
import pytest

import litellm
from litellm.llms.bedrock.audio_transcription.transformation import BedrockAudioTranscriptionConfig
from litellm.llms.custom_httpx import llm_http_handler
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

CONFIG: Final = BedrockAudioTranscriptionConfig()
MODEL: Final = "mistral.voxtral-mini-3b-2507"
AUDIO_FILE: Final = ("sample.wav", b"\x01\x02", "audio/wav")


def test_request_carries_the_audio_the_instruction_and_the_inference_config() -> None:
    params: Final = CONFIG.map_openai_params(
        non_default_params={"language": "en", "prompt": "Speaker names", "temperature": 0, "user": "ignored"},
        optional_params={},
        model=MODEL,
        drop_params=False,
    )

    request: Final = CONFIG.transform_audio_transcription_request(
        model=MODEL, audio_file=AUDIO_FILE, optional_params=params, litellm_params={}
    )

    assert request.data == {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"audio": {"format": "wav", "source": {"bytes": "AQI="}}},
                    {
                        "text": "Transcribe the audio. Respond with only the transcript. "
                        "The audio language is en. Additional context: Speaker names"
                    },
                ],
            }
        ],
        "system": [{"text": "You are a transcription assistant."}],
        "inferenceConfig": {"maxTokens": 4096, "temperature": 0},
    }


def test_without_options_the_request_has_only_the_base_instruction_and_token_limit() -> None:
    request: Final = CONFIG.transform_audio_transcription_request(
        model=MODEL, audio_file=("clip.mp3", b"abc", "audio/mpeg"), optional_params={}, litellm_params={}
    )

    assert isinstance(request.data, dict)
    assert request.data["messages"][0]["content"] == [
        {"audio": {"format": "mp3", "source": {"bytes": base64.b64encode(b"abc").decode()}}},
        {"text": "Transcribe the audio. Respond with only the transcript."},
    ]
    assert request.data["inferenceConfig"] == {"maxTokens": 4096}


@pytest.mark.parametrize(
    "audio_file",
    [("notes.txt", b"abc", "text/plain"), ("voice.m4a", b"abc", "audio/mp4")],
)
def test_audio_bedrock_cannot_read_is_rejected(audio_file: tuple[str, bytes, str]) -> None:
    with pytest.raises(ValueError, match="Unsupported Bedrock audio format"):
        CONFIG.transform_audio_transcription_request(
            model=MODEL, audio_file=audio_file, optional_params={}, litellm_params={}
        )


def test_response_concatenates_the_text_blocks() -> None:
    response: Final = CONFIG.transform_audio_transcription_response(
        httpx.Response(
            200,
            json={"output": {"message": {"content": [{"text": "hello "}, {"reasoningContent": {}}, {"text": "world"}]}}},
        )
    )

    assert response.text == "hello world"


@pytest.mark.parametrize(
    ("model", "optional_params", "expected"),
    [
        pytest.param(
            f"us-east-1/{MODEL}",
            {"aws_region_name": "eu-west-1"},
            f"https://bedrock-runtime.eu-west-1.amazonaws.com/model/{MODEL}/converse",
            id="explicit_region_beats_the_model_path",
        ),
        pytest.param(
            f"us-east-1/{MODEL}",
            {},
            f"https://bedrock-runtime.us-east-1.amazonaws.com/model/{MODEL}/converse",
            id="the_model_path_region_is_used_without_an_explicit_one",
        ),
        pytest.param(
            "arn:aws:bedrock:us-west-2:123456789012:imported-model/abc",
            {"aws_region_name": "us-west-2"},
            "https://bedrock-runtime.us-west-2.amazonaws.com/model/"
            "arn%3Aaws%3Abedrock%3Aus-west-2%3A123456789012%3Aimported-model%2Fabc/converse",
            id="an_arn_model_id_is_url_encoded",
        ),
    ],
)
def test_the_url_follows_region_precedence(model: str, optional_params: dict[str, object], expected: str) -> None:
    url: Final = CONFIG.get_complete_url(
        api_base=None, api_key=None, model=model, optional_params=optional_params, litellm_params={}
    )

    assert url == expected


@pytest.fixture
async def bedrock() -> AsyncGenerator[Mock]:
    handler: Final = Mock(
        return_value=httpx.Response(200, json={"output": {"message": {"content": [{"text": "transcribed text"}]}}})
    )
    transport: Final = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            sync_handler: Final = HTTPHandler(client=sync_client)
            async_handler: Final = AsyncHTTPHandler()
            await async_handler.client.aclose()
            async_handler.client = async_client
            with pytest.MonkeyPatch.context() as patch:
                patch.delenv("AWS_BEARER_TOKEN_BEDROCK", raising=False)
                patch.setattr(llm_http_handler, "_get_httpx_client", lambda *_, **__: sync_handler)
                patch.setattr(llm_http_handler, "get_async_httpx_client", lambda *_, **__: async_handler)
                yield handler


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_public_transcription_signs_a_converse_request_and_returns_the_text(
    bedrock: Mock, asynchronous: bool
) -> None:
    arguments: Final = {
        "model": f"bedrock/us-east-1/{MODEL}",
        "file": AUDIO_FILE,
        "language": "en",
        "prompt": "Speaker names",
        "aws_access_key_id": "AKIDEXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
    }

    response: Final = (
        await litellm.atranscription(**arguments) if asynchronous else litellm.transcription(**arguments)
    )

    assert response.text == "transcribed text"
    request: Final[httpx.Request] = bedrock.call_args.args[0]
    assert str(request.url) == f"https://bedrock-runtime.us-east-1.amazonaws.com/model/{MODEL}/converse"
    authorization: Final = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/us-east-1/bedrock/aws4_request" in authorization
    body: Final = json.loads(request.content)
    assert body["messages"][0]["content"][0] == {"audio": {"format": "wav", "source": {"bytes": "AQI="}}}
    assert "The audio language is en. Additional context: Speaker names" in body["messages"][0]["content"][1]["text"]
    assert "aws_secret_access_key" not in request.content.decode()


@pytest.mark.asyncio
async def test_a_bedrock_api_key_is_sent_as_a_bearer_token_instead_of_sigv4(bedrock: Mock) -> None:
    await litellm.atranscription(model=f"bedrock/{MODEL}", file=AUDIO_FILE, api_key="bedrock-key", aws_region_name="us-east-1")

    request: Final[httpx.Request] = bedrock.call_args.args[0]
    assert request.headers["authorization"] == "Bearer bedrock-key"


@pytest.mark.asyncio
async def test_a_provider_error_keeps_its_status(bedrock: Mock) -> None:
    bedrock.return_value = httpx.Response(429, json={"message": "Too many requests"})

    with pytest.raises(litellm.RateLimitError):
        await litellm.atranscription(
            model=f"bedrock/{MODEL}", file=AUDIO_FILE, api_key="bedrock-key", aws_region_name="us-east-1"
        )
