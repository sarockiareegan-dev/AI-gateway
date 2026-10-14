import base64
import binascii
import json
from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Final, Literal, get_args
from urllib.parse import unquote_to_bytes

import httpx
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError

from litellm.litellm_core_utils.aws_partition import get_aws_dns_suffix
from litellm.litellm_core_utils.prompt_templates.image_handling import (
    async_convert_url_to_base64,
    convert_url_to_base64,
)
from litellm.llms.aws_textract.ocr.blocks import TextractResponse, layout_by_page, lines_by_page
from litellm.llms.base_llm.chat.transformation import BaseLLMException
from litellm.llms.base_llm.ocr.transformation import (
    BaseOCRConfig,
    DocumentType,
    OCRPage,
    OCRRequestData,
    OCRResponse,
    OCRUsageInfo,
)
from litellm.llms.bedrock.base_aws_llm import BaseAWSLLM, sign_aws_json_post
from litellm.secret_managers.main import get_secret_str
from litellm.types.llms.bedrock import AwsAuthParams

PROVIDER: Final = "aws_textract"
SYNC_DOCUMENT_MAX_BYTES: Final = 10 * 1024 * 1024
_SERVICE: Final = "textract"
_TARGET_HEADER: Final = "X-Amz-Target"
_CONTENT_TYPE_HEADER: Final = "Content-Type"
_AWS_JSON_CONTENT_TYPE: Final = "application/x-amz-json-1.1"
_UNSUPPORTED_DOCUMENT: Final = "UnsupportedDocumentException"
_HEALTH_CHECK_IMAGE: Final = (
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAX+Av4N70a4AAAAAElFTkSuQmCC"
)

FeatureType = Literal["TABLES", "FORMS", "QUERIES", "SIGNATURES", "LAYOUT"]
_FEATURE_TYPES: Final = TypeAdapter(list[FeatureType])
_DEFAULT_FEATURE_TYPES: Final[tuple[FeatureType, ...]] = ("LAYOUT", "TABLES")


class TextractOperation(Enum):
    DETECT_DOCUMENT_TEXT = "detect-document-text"
    ANALYZE_DOCUMENT = "analyze-document"

    @property
    def target(self) -> str:
        match self:
            case TextractOperation.DETECT_DOCUMENT_TEXT:
                return "Textract.DetectDocumentText"
            case TextractOperation.ANALYZE_DOCUMENT:
                return "Textract.AnalyzeDocument"

    @classmethod
    def from_model(cls, model: str) -> "TextractOperation":
        operation: Final = next((op for op in cls if op.value == model.lower()), None)
        if operation is None:
            supported: Final = ", ".join(op.value for op in cls)
            raise ValueError(f"{PROVIDER} has no model {model!r} - use one of: {supported}")
        return operation


class _AwsError(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    kind: str = Field(default="", alias="__type")
    message: str = Field(default="", validation_alias="Message")
    lowercase_message: str = Field(default="", validation_alias="message")


class _Document(BaseModel):
    model_config = ConfigDict(frozen=True)

    data: str = Field(serialization_alias="Bytes")


class _TextractRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    document: _Document = Field(serialization_alias="Document")
    feature_types: tuple[FeatureType, ...] | None = Field(default=None, serialization_alias="FeatureTypes")


def _region(litellm_params: Mapping[str, object]) -> str:
    configured: Final = litellm_params.get("aws_region_name")
    region: Final = (
        configured
        if isinstance(configured, str) and configured
        else get_secret_str("AWS_REGION_NAME") or get_secret_str("AWS_REGION")
    )
    if not region:
        raise ValueError("Missing AWS region - pass aws_region_name or set AWS_REGION_NAME or AWS_REGION")
    return region


def _decode_data_uri(uri: str) -> bytes:
    header, separator, payload = uri.removeprefix("data:").partition(",")
    if not uri.startswith("data:") or not separator:
        raise ValueError(f"{PROVIDER} needs the document as a data URI or a downloadable URL")
    try:
        raw: Final = (
            base64.b64decode(payload, validate=True) if header.endswith(";base64") else unquote_to_bytes(payload)
        )
    except binascii.Error as error:
        raise ValueError(f"{PROVIDER} could not decode the document's base64 data") from error
    if len(raw) > SYNC_DOCUMENT_MAX_BYTES:
        raise ValueError(f"{PROVIDER} accepts documents up to {SYNC_DOCUMENT_MAX_BYTES} bytes")
    return raw


def _document_source(document: DocumentType) -> str:
    return document.get("document_url") or document.get("image_url") or ""


def _operation_headers(headers: Mapping[str, object], operation: TextractOperation) -> dict[str, str]:
    ours: Final = MappingProxyType({_TARGET_HEADER: operation.target, _CONTENT_TYPE_HEADER: _AWS_JSON_CONTENT_TYPE})
    lowered: Final = frozenset(name.lower() for name in ours)
    return {  # mutable-ok: BaseOCRConfig.validate_environment returns a dict
        **{str(name): str(value) for name, value in headers.items() if str(name).lower() not in lowered},
        **ours,
    }


class AWSTextractOCRConfig(BaseOCRConfig, BaseAWSLLM):
    def __init__(self) -> None:
        BaseOCRConfig.__init__(self)
        BaseAWSLLM.__init__(self)

    def get_supported_ocr_params(self, model: str) -> list[str]:
        supported: Final = ("feature_types",) if model.lower() == TextractOperation.ANALYZE_DOCUMENT.value else ()
        return list(supported)  # mutable-ok: BaseOCRConfig returns a list

    def get_health_check_document(self) -> DocumentType:
        return {"type": "image_url", "image_url": _HEALTH_CHECK_IMAGE}  # mutable-ok: litellm.aocr requires a dict

    def map_ocr_params(
        self, non_default_params: dict[str, object], optional_params: dict[str, object], model: str
    ) -> dict[str, object]:
        operation: Final = TextractOperation.from_model(model)
        if operation is TextractOperation.DETECT_DOCUMENT_TEXT:
            return {}  # mutable-ok: the OCR handler takes the mapped params as a dict
        requested: Final = non_default_params.get("feature_types")
        if requested is None:
            return {"feature_types": list(_DEFAULT_FEATURE_TYPES)}  # mutable-ok: the OCR handler takes a dict
        try:
            feature_types: Final = _FEATURE_TYPES.validate_python(requested, strict=True)
        except ValidationError as error:
            raise ValueError(
                f"feature_types must be a list of {', '.join(get_args(FeatureType))}, got {requested!r}"
            ) from error
        return {"feature_types": feature_types}  # mutable-ok: the OCR handler takes the mapped params as a dict

    def validate_environment(
        self,
        headers: dict[str, object],
        model: str,
        api_key: str | None = None,
        api_base: str | None = None,
        litellm_params: dict[str, object] | None = None,
        **kwargs: object,  # kwargs-ok: BaseOCRConfig contract
    ) -> dict[str, str]:
        return _operation_headers(headers, TextractOperation.from_model(model))

    def get_complete_url(
        self,
        api_base: str | None,
        model: str,
        optional_params: dict[str, object],
        litellm_params: dict[str, object] | None = None,
        **kwargs: object,  # kwargs-ok: BaseOCRConfig contract
    ) -> str:
        if api_base:
            return api_base
        region: Final = _region(litellm_params or MappingProxyType({}))
        return f"https://textract.{region}.{get_aws_dns_suffix(region)}/"

    def _request(self, document_bytes: bytes, optional_params: Mapping[str, object]) -> OCRRequestData:
        encoded: Final = base64.b64encode(document_bytes).decode("ascii")
        feature_types: Final = optional_params.get("feature_types")
        request: Final = _TextractRequest(
            document=_Document(data=encoded),
            feature_types=tuple(_FEATURE_TYPES.validate_python(feature_types)) if feature_types else None,
        )
        return OCRRequestData(data=request.model_dump(mode="json", by_alias=True, exclude_none=True))

    def transform_ocr_request(
        self,
        model: str,
        document: DocumentType,
        optional_params: dict[str, object],
        headers: dict[str, str],
        **kwargs: object,  # kwargs-ok: BaseOCRConfig contract
    ) -> OCRRequestData:
        source: Final = _document_source(document)
        inline: Final = source if source.startswith("data:") else convert_url_to_base64(source)
        return self._request(_decode_data_uri(inline), optional_params)

    async def async_transform_ocr_request(
        self,
        model: str,
        document: DocumentType,
        optional_params: dict[str, object],
        headers: dict[str, str],
        **kwargs: object,  # kwargs-ok: BaseOCRConfig contract
    ) -> OCRRequestData:
        source: Final = _document_source(document)
        inline: Final = source if source.startswith("data:") else await async_convert_url_to_base64(source)
        return self._request(_decode_data_uri(inline), optional_params)

    def sign_request(
        self,
        headers: dict[str, object],
        request_data: dict[str, object],
        api_base: str,
        litellm_params: Mapping[str, object],
    ) -> tuple[dict[str, object], bytes | None]:
        region: Final = _region(litellm_params)
        credentials: Final = self.resolve_credentials(AwsAuthParams.model_validate(litellm_params), region)
        prepared: Final = sign_aws_json_post(
            lambda: credentials,
            _SERVICE,
            region,
            api_base,
            json.dumps(request_data),
            MappingProxyType({str(name): str(value) for name, value in headers.items()}),
        )
        body: Final = prepared.body
        signed_headers: Final[dict[str, object]] = {  # mutable-ok: BaseOCRConfig.sign_request returns a dict
            str(name): str(value) for name, value in prepared.headers.items()
        }
        signed_body: Final = (
            body.encode() if isinstance(body, str) else bytes(body) if isinstance(body, (bytes, bytearray)) else None
        )
        return signed_headers, signed_body

    def transform_ocr_response(
        self,
        model: str,
        raw_response: httpx.Response,
        logging_obj: object,
        **kwargs: object,  # kwargs-ok: BaseOCRConfig contract
    ) -> OCRResponse:
        response: Final = TextractResponse.model_validate_json(raw_response.content)
        operation: Final = TextractOperation.from_model(model)
        page_markdown: Final = (
            layout_by_page(response.blocks)
            if operation is TextractOperation.ANALYZE_DOCUMENT
            else lines_by_page(response.blocks)
        )
        pages: Final = tuple(OCRPage(index=page - 1, markdown=markdown) for page, markdown in page_markdown)
        metadata_pages: Final = response.document_metadata.pages if response.document_metadata else None
        return OCRResponse(
            pages=list(pages),  # mutable-ok: OCRResponse.pages is a list
            model=model,
            usage_info=OCRUsageInfo(pages_processed=metadata_pages if metadata_pages is not None else len(pages)),
        )

    def get_error_class(self, error_message: str, status_code: int, headers: dict[str, str]) -> Exception:
        try:
            error: Final = _AwsError.model_validate_json(error_message)
        except ValidationError:
            return BaseLLMException(status_code=status_code, message=error_message, headers=headers)
        if not error.kind.endswith(_UNSUPPORTED_DOCUMENT):
            return BaseLLMException(status_code=status_code, message=error_message, headers=headers)
        return BaseLLMException(
            status_code=status_code,
            message=(
                f"{_UNSUPPORTED_DOCUMENT}: {error.message or error.lowercase_message}. {PROVIDER} uses Textract's "
                "synchronous API, which reads a JPEG, PNG, or a single-page PDF or TIFF; other formats and "
                "multi-page documents are not supported"
            ),
            headers=headers,
        )
