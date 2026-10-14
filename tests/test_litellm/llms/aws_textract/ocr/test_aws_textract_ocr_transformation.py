import json
from collections.abc import AsyncGenerator
from typing import Final
from unittest.mock import Mock

import httpx
import pytest

import litellm
from litellm.llms.aws_textract.ocr.blocks import TextractResponse, layout_by_page, lines_by_page, strip_bullet
from litellm.llms.aws_textract.ocr.transformation import (
    SYNC_DOCUMENT_MAX_BYTES,
    AWSTextractOCRConfig,
    TextractOperation,
)
from litellm.llms.custom_httpx import llm_http_handler
from litellm.llms.custom_httpx.http_handler import AsyncHTTPHandler, HTTPHandler

CONFIG: Final = AWSTextractOCRConfig()
UNSUPPORTED_HINT: Final = "other formats and multi-page documents are not supported"


def _blocks(blocks: list[dict[str, object]]) -> TextractResponse:
    return TextractResponse.model_validate({"Blocks": blocks})


def _child(ids: list[str]) -> list[dict[str, object]]:
    return [{"Type": "CHILD", "Ids": ids}]


def _line(id_: str, text: str, page: int | None = None) -> dict[str, object]:
    return {"Id": id_, "BlockType": "LINE", "Text": text, **({"Page": page} if page else {})}


def _word(id_: str, text: str, page: int | None = None) -> dict[str, object]:
    return {"Id": id_, "BlockType": "WORD", "Text": text, **({"Page": page} if page else {})}


def _layout(id_: str, block_type: str, children: list[str], page: int | None = None) -> dict[str, object]:
    return {"Id": id_, "BlockType": block_type, "Relationships": _child(children), **({"Page": page} if page else {})}


def _table(id_: str, cells: list[str], page: int | None = None) -> dict[str, object]:
    return {"Id": id_, "BlockType": "TABLE", "Relationships": _child(cells), **({"Page": page} if page else {})}


def _cell(id_: str, row: int, column: int, words: list[str], page: int | None = None) -> dict[str, object]:
    return {
        "Id": id_,
        "BlockType": "CELL",
        "RowIndex": row,
        "ColumnIndex": column,
        "Relationships": _child(words),
        **({"Page": page} if page else {}),
    }


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        pytest.param(
            [
                {"BlockType": "PAGE"},
                _line("a", "Invoice 12345"),
                _word("w1", "Invoice"),
                _word("w2", "12345"),
                _line("b", "total 67.89"),
            ],
            ((1, "Invoice 12345\ntotal 67.89"),),
            id="words_are_not_repeated",
        ),
        pytest.param(
            [_line("a", "second", 2), _line("b", "first", 1), _line("c", "also second", 2)],
            ((1, "first"), (2, "second\nalso second")),
            id="pages_are_sorted_and_keep_line_order",
        ),
        pytest.param(
            [{"BlockType": "PAGE", "Page": 1}, _line("a", "only", 2)],
            ((2, "only"),),
            id="a_page_without_lines_is_dropped",
        ),
        pytest.param([], (), id="no_blocks"),
    ],
)
def test_lines_are_grouped_by_page(blocks: list[dict[str, object]], expected: tuple[tuple[int, str], ...]) -> None:
    assert lines_by_page(_blocks(blocks).blocks) == expected


@pytest.mark.parametrize(
    ("blocks", "expected"),
    [
        pytest.param(
            [
                _line("l1", "Quarterly Report"),
                _line("l2", "This report lists"),
                _line("l3", "the invoices."),
                _line("l4", "Line items"),
                _line("l5", "- Pay within 30 days"),
                _line("l6", "\u2022 Quote the number"),
                _layout("t", "LAYOUT_TITLE", ["l1"]),
                _layout("p", "LAYOUT_TEXT", ["l2", "l3"]),
                _layout("h", "LAYOUT_SECTION_HEADER", ["l4"]),
                _layout("ul", "LAYOUT_LIST", ["i1", "i2"]),
                _layout("i1", "LAYOUT_TEXT", ["l5"]),
                _layout("i2", "LAYOUT_TEXT", ["l6"]),
            ],
            "# Quarterly Report\n\nThis report lists the invoices.\n\n## Line items\n\n"
            "- Pay within 30 days\n- Quote the number",
            id="headings_paragraphs_and_a_list_without_repeating_its_items",
        ),
        pytest.param(
            [
                _line("l1", "ACME Corp"),
                _line("l2", "Body"),
                _line("l3", "Confidential"),
                _line("l4", "3"),
                _layout("hd", "LAYOUT_HEADER", ["l1"]),
                _layout("p", "LAYOUT_TEXT", ["l2"]),
                _layout("ft", "LAYOUT_FOOTER", ["l3"]),
                _layout("pn", "LAYOUT_PAGE_NUMBER", ["l4"]),
            ],
            "ACME Corp\n\nBody\n\nConfidential\n\n3",
            id="header_footer_and_page_number_stay_in_reading_order",
        ),
        pytest.param(
            [
                _line("l1", "Invoice"),
                _line("l2", "Total"),
                _word("w1", "Invoice"),
                _word("w2", "Total"),
                _word("w3", "12345"),
                _word("w4", "a|b"),
                {
                    "Id": "tb",
                    "BlockType": "TABLE",
                    "Relationships": [
                        {"Type": "CHILD", "Ids": ["c4", "c1", "c3", "c2"]},
                        {"Type": "TABLE_TITLE", "Ids": ["title"]},
                    ],
                },
                _cell("c1", 1, 1, ["w1"]),
                _cell("c2", 1, 2, ["w2"]),
                _cell("c3", 2, 1, ["w3"]),
                _cell("c4", 2, 2, ["w4"]),
                _layout("lt", "LAYOUT_TABLE", ["l1", "l2"]),
            ],
            "| Invoice | Total |\n| --- | --- |\n| 12345 | a\\|b |",
            id="a_table_is_rendered_from_its_cells_in_row_and_column_order",
        ),
        pytest.param(
            [
                _word("w1", "first"),
                _word("w2", "second"),
                _table("tb1", ["c1"]),
                _cell("c1", 1, 1, ["w1"]),
                _table("tb2", ["c2"]),
                _cell("c2", 1, 1, ["w2"]),
                _layout("lt", "LAYOUT_TABLE", ["tb2"]),
            ],
            "| second |\n| --- |",
            id="a_layout_table_that_links_its_table_renders_that_one",
        ),
        pytest.param(
            [
                _word("w1", "a"),
                _word("w2", "b"),
                _word("w3", "c"),
                _table("tb", ["c1", "c2", "c3"]),
                _cell("c1", 1, 1, ["w1"]),
                _cell("c2", 1, 2, ["w2"]),
                _cell("c3", 2, 2, ["w3"]),
                _layout("lt", "LAYOUT_TABLE", []),
            ],
            "| a | b |\n| --- | --- |\n|  | c |",
            id="a_missing_cell_leaves_an_empty_column",
        ),
        pytest.param(
            [_line("l1", "Invoice Total"), _line("l2", "12345 67.89"), _layout("lt", "LAYOUT_TABLE", ["l1", "l2"])],
            "Invoice Total\n12345 67.89",
            id="a_layout_table_without_table_blocks_keeps_its_lines",
        ),
        pytest.param(
            [_line("l1", "Name: Ana"), _line("l2", "Date: 2024-01-01"), _layout("kv", "LAYOUT_KEY_VALUE", ["l1", "l2"])],
            "Name: Ana\nDate: 2024-01-01",
            id="key_values_keep_one_line_each",
        ),
        pytest.param(
            [_line("l1", "Caption"), _layout("f", "LAYOUT_FIGURE", []), _layout("p", "LAYOUT_TEXT", ["l1"])],
            "Caption",
            id="a_figure_has_no_markdown",
        ),
        pytest.param(
            [_line("l1", "Body"), _layout("new", "LAYOUT_SIDEBAR", ["l1"]), _layout("p", "LAYOUT_TEXT", ["l1"])],
            "Body",
            id="a_block_type_added_later_is_ignored",
        ),
        pytest.param(
            [_line("l1", "first"), _word("w1", "first"), _line("l2", "second")],
            "first\nsecond",
            id="without_layout_blocks_lines_are_used",
        ),
    ],
)
def test_layout_blocks_become_markdown(blocks: list[dict[str, object]], expected: str) -> None:
    assert layout_by_page(_blocks(blocks).blocks) == ((1, expected),)


def test_each_page_gets_its_own_markdown_and_its_own_tables() -> None:
    blocks: Final = [
        _line("a", "one", 1),
        _line("b", "two", 2),
        _word("w", "cell", 2),
        _layout("t1", "LAYOUT_TEXT", ["a"], 1),
        _table("tb", ["c"], 2),
        _cell("c", 1, 1, ["w"], 2),
        _layout("lt", "LAYOUT_TABLE", ["b"], 2),
    ]

    assert layout_by_page(_blocks(blocks).blocks) == ((1, "one"), (2, "| cell |\n| --- |"))


@pytest.mark.parametrize(
    ("item", "expected"),
    [("- item", "item"), ("* item", "item"), ("\u2022 item", "item"), ("\u00b7item", "item"), ("item - x", "item - x")],
)
def test_list_items_lose_their_own_bullet(item: str, expected: str) -> None:
    assert strip_bullet(item) == expected


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("detect-document-text", TextractOperation.DETECT_DOCUMENT_TEXT),
        ("analyze-document", TextractOperation.ANALYZE_DOCUMENT),
        ("Analyze-Document", TextractOperation.ANALYZE_DOCUMENT),
    ],
)
def test_a_model_names_its_operation(model: str, expected: TextractOperation) -> None:
    assert TextractOperation.from_model(model) is expected


@pytest.mark.parametrize("model", ["analyse-document", "AnalyzeDocument", "analyze-expense", ""])
def test_a_model_outside_the_operations_is_refused_with_the_supported_names(model: str) -> None:
    with pytest.raises(ValueError, match="use one of: detect-document-text, analyze-document"):
        CONFIG.map_ocr_params(non_default_params={}, optional_params={}, model=model)


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({}, ["LAYOUT", "TABLES"]),
        ({"feature_types": ["FORMS", "SIGNATURES"]}, ["FORMS", "SIGNATURES"]),
        ({"feature_types": None}, ["LAYOUT", "TABLES"]),
    ],
)
def test_feature_types_reach_the_analyze_request(arguments: dict[str, object], expected: list[str]) -> None:
    params: Final = CONFIG.map_ocr_params(non_default_params=arguments, optional_params={}, model="analyze-document")

    request: Final = CONFIG.transform_ocr_request(
        model="analyze-document",
        document={"type": "image_url", "image_url": "data:image/png;base64,aGk="},
        optional_params=params,
        headers={},
    )

    assert request.data == {"Document": {"Bytes": "aGk="}, "FeatureTypes": expected}


@pytest.mark.parametrize("feature_types", [["HANDWRITING"], ["layout"], "LAYOUT"])
def test_feature_types_outside_the_documented_values_are_refused(feature_types: object) -> None:
    with pytest.raises(ValueError, match="feature_types must be a list of"):
        CONFIG.map_ocr_params(
            non_default_params={"feature_types": feature_types}, optional_params={}, model="analyze-document"
        )


def test_detect_document_text_takes_no_options_and_sends_only_the_document() -> None:
    assert CONFIG.get_supported_ocr_params("detect-document-text") == []
    params: Final = CONFIG.map_ocr_params(non_default_params={}, optional_params={}, model="detect-document-text")

    request: Final = CONFIG.transform_ocr_request(
        model="detect-document-text",
        document={"type": "document_url", "document_url": "data:application/pdf;base64,YWJj"},
        optional_params=params,
        headers={},
    )

    assert request.data == {"Document": {"Bytes": "YWJj"}}


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("data:image/png;base64,aGVsbG8=", "aGVsbG8="),
        ("data:,abc", "YWJj"),
        ("data:text/plain,a%20b", "YSBi"),
    ],
)
def test_document_bytes_are_the_base64_payload_without_the_data_uri_envelope(source: str, expected: str) -> None:
    request: Final = CONFIG.transform_ocr_request(
        model="detect-document-text",
        document={"type": "document_url", "document_url": source},
        optional_params={},
        headers={},
    )

    assert request.data == {"Document": {"Bytes": expected}}


@pytest.mark.parametrize(
    ("source", "message"),
    [
        pytest.param("data:image/png;base64,@@@", "could not decode", id="invalid_base64"),
        pytest.param(
            f"data:,{'a' * (SYNC_DOCUMENT_MAX_BYTES + 1)}", "accepts documents up to", id="over_the_sync_limit"
        ),
    ],
)
def test_document_bytes_refuse_what_the_sync_api_cannot_take(source: str, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        CONFIG.transform_ocr_request(
            model="detect-document-text",
            document={"type": "document_url", "document_url": source},
            optional_params={},
            headers={},
        )


def test_the_health_check_document_is_an_inline_image_the_request_accepts() -> None:
    request: Final = CONFIG.transform_ocr_request(
        model="detect-document-text",
        document=CONFIG.get_health_check_document(),
        optional_params={},
        headers={},
    )

    assert isinstance(request.data, dict)
    assert request.data["Document"]["Bytes"]


@pytest.mark.parametrize(
    ("caller_headers", "kept"),
    [
        ({}, {}),
        ({"x-trace": "1"}, {"x-trace": "1"}),
        ({"content-type": "application/json", "x-trace": "1"}, {"x-trace": "1"}),
        ({"X-AMZ-TARGET": "Textract.AnalyzeDocument"}, {}),
    ],
)
def test_operation_headers_are_sent_once(caller_headers: dict[str, str], kept: dict[str, str]) -> None:
    headers: Final = CONFIG.validate_environment(headers=caller_headers, model="detect-document-text")

    assert headers == {
        **kept,
        "X-Amz-Target": "Textract.DetectDocumentText",
        "Content-Type": "application/x-amz-json-1.1",
    }


@pytest.mark.parametrize(
    ("body", "hinted"),
    [
        ('{"__type":"UnsupportedDocumentException","Message":"Request has unsupported document format"}', True),
        ('{"__type":"com.amazonaws.textract#UnsupportedDocumentException","Message":"bad"}', True),
        ('{"__type":"UnsupportedDocumentException","message":"bad"}', True),
        ('{"__type":"AccessDeniedException","Message":"no"}', False),
        ('{"Message":"no"}', False),
        ("<html>bad gateway</html>", False),
    ],
)
def test_only_an_unsupported_document_gains_the_sync_api_hint(body: str, hinted: bool) -> None:
    error: Final = CONFIG.get_error_class(error_message=body, status_code=400, headers={"x-amzn-requestid": "abc"})

    assert getattr(error, "status_code") == 400
    message: Final = str(getattr(error, "message"))
    assert (UNSUPPORTED_HINT in message) is hinted
    assert message == body or hinted


def test_a_missing_region_is_reported_before_any_request(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AWS_REGION_NAME", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)

    with pytest.raises(ValueError, match="Missing AWS region"):
        CONFIG.get_complete_url(api_base=None, model="detect-document-text", optional_params={}, litellm_params={})


@pytest.mark.parametrize(
    ("region", "expected"),
    [("eu-west-1", "https://textract.eu-west-1.amazonaws.com/"), ("cn-north-1", "https://textract.cn-north-1.amazonaws.com.cn/")],
)
def test_the_endpoint_follows_the_region_and_its_partition(region: str, expected: str) -> None:
    url: Final = CONFIG.get_complete_url(
        api_base=None, model="detect-document-text", optional_params={}, litellm_params={"aws_region_name": region}
    )

    assert url == expected


@pytest.fixture
async def textract() -> AsyncGenerator[Mock]:
    handler: Final = Mock(
        return_value=httpx.Response(
            200,
            json={
                "DocumentMetadata": {"Pages": 1},
                "Blocks": [
                    {"BlockType": "PAGE"},
                    {"BlockType": "LINE", "Text": "Invoice 12345"},
                    {"BlockType": "LINE", "Text": "total 67.89"},
                ],
            },
        )
    )
    transport: Final = httpx.MockTransport(handler)
    with httpx.Client(transport=transport) as sync_client:
        async with httpx.AsyncClient(transport=transport) as async_client:
            sync_handler: Final = HTTPHandler(client=sync_client)
            async_handler: Final = AsyncHTTPHandler()
            await async_handler.client.aclose()
            async_handler.client = async_client
            with pytest.MonkeyPatch.context() as patch:
                patch.setattr(llm_http_handler, "_get_httpx_client", lambda: sync_handler)
                patch.setattr(llm_http_handler, "get_async_httpx_client", lambda llm_provider: async_handler)
                yield handler


@pytest.mark.asyncio
@pytest.mark.parametrize("asynchronous", [False, True])
async def test_public_ocr_signs_the_textract_request_and_normalizes_the_lines(
    textract: Mock, asynchronous: bool
) -> None:
    arguments: Final = {
        "model": "aws_textract/detect-document-text",
        "document": {"type": "image_url", "image_url": "data:image/png;base64,aGVsbG8="},
        "aws_access_key_id": "AKIDEXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG+bPxRfiCYEXAMPLEKEY",
        "aws_region_name": "eu-west-1",
        "extra_headers": {"x-trace": "kept"},
    }

    response: Final = await litellm.aocr(**arguments) if asynchronous else litellm.ocr(**arguments)

    assert [(page.index, page.markdown) for page in response.pages] == [(0, "Invoice 12345\ntotal 67.89")]
    assert response.usage_info is not None and response.usage_info.pages_processed == 1
    request: Final[httpx.Request] = textract.call_args.args[0]
    assert str(request.url) == "https://textract.eu-west-1.amazonaws.com/"
    assert request.headers["x-amz-target"] == "Textract.DetectDocumentText"
    assert request.headers["content-type"] == "application/x-amz-json-1.1"
    assert request.headers["x-trace"] == "kept"
    authorization: Final = request.headers["authorization"]
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=AKIDEXAMPLE/")
    assert "/eu-west-1/textract/aws4_request" in authorization
    assert "x-amz-target" in authorization.split("SignedHeaders=")[1]
    assert request.content == json.dumps({"Document": {"Bytes": "aGVsbG8="}}).encode()
    assert "wJalrXUtnFEMI" not in request.content.decode() + str(dict(request.headers))


@pytest.mark.asyncio
async def test_public_ocr_rejects_an_unknown_textract_model_as_a_bad_request(textract: Mock) -> None:
    with pytest.raises(litellm.BadRequestError, match="use one of"):
        await litellm.aocr(
            model="aws_textract/analyze-expense",
            document={"type": "image_url", "image_url": "data:image/png;base64,aGVsbG8="},
            aws_region_name="eu-west-1",
        )

    assert textract.call_count == 0
