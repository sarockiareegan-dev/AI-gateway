from collections.abc import Mapping
from enum import Enum
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


class _TextractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="ignore", populate_by_name=True)


class Relationship(_TextractModel):
    type: str = Field(alias="Type")
    ids: tuple[str, ...] = Field(default=(), alias="Ids")


class Block(_TextractModel):
    id: str = Field(default="", alias="Id")
    block_type: str = Field(alias="BlockType")
    text: str | None = Field(default=None, alias="Text")
    page: int = Field(default=1, alias="Page")
    row_index: int | None = Field(default=None, alias="RowIndex")
    column_index: int | None = Field(default=None, alias="ColumnIndex")
    relationships: tuple[Relationship, ...] = Field(default=(), alias="Relationships")

    @property
    def children(self) -> tuple[str, ...]:
        return tuple(child for rel in self.relationships if rel.type == "CHILD" for child in rel.ids)


class DocumentMetadata(_TextractModel):
    pages: int | None = Field(default=None, alias="Pages")


class TextractResponse(_TextractModel):
    blocks: tuple[Block, ...] = Field(default=(), alias="Blocks")
    document_metadata: DocumentMetadata | None = Field(default=None, alias="DocumentMetadata")


class Layout(Enum):
    TEXT = "LAYOUT_TEXT"
    TITLE = "LAYOUT_TITLE"
    HEADER = "LAYOUT_HEADER"
    FOOTER = "LAYOUT_FOOTER"
    SECTION_HEADER = "LAYOUT_SECTION_HEADER"
    PAGE_NUMBER = "LAYOUT_PAGE_NUMBER"
    LIST = "LAYOUT_LIST"
    FIGURE = "LAYOUT_FIGURE"
    TABLE = "LAYOUT_TABLE"
    KEY_VALUE = "LAYOUT_KEY_VALUE"


_LAYOUTS: Final = MappingProxyType({layout.value: layout for layout in Layout})
_BULLETS: Final = "-*\u2022\u00b7"


def _pages(blocks: tuple[Block, ...]) -> tuple[int, ...]:
    return tuple(sorted(frozenset(block.page for block in blocks)))


def lines_by_page(blocks: tuple[Block, ...]) -> tuple[tuple[int, str], ...]:
    rendered: Final = (
        (page, "\n".join(b.text for b in blocks if b.block_type == "LINE" and b.page == page and b.text is not None))
        for page in _pages(blocks)
    )
    return tuple((page, markdown) for page, markdown in rendered if markdown)


def _text_of(block: Block, by_id: Mapping[str, Block], separator: str) -> str:
    if block.text is not None:
        return block.text
    parts: Final[list[str]] = []
    seen: Final[set[str]] = {block.id}
    pending: Final[list[Block]] = [block]
    while pending:
        current = pending.pop()
        if current.text is not None:
            if current.text:
                parts.append(current.text)
            continue
        children = tuple(by_id[child] for child in current.children if child in by_id and child not in seen)
        seen.update(child.id for child in children)
        pending.extend(reversed(children))
    return separator.join(parts)


def strip_bullet(item: str) -> str:
    return item.lstrip(_BULLETS).lstrip()


def _table_markdown(table: Block, by_id: Mapping[str, Block]) -> str:
    cells: Final = MappingProxyType(
        {
            (cell.row_index, cell.column_index): _text_of(cell, by_id, " ").replace("|", "\\|")
            for cell in (by_id[child] for child in table.children if child in by_id)
            if cell.block_type == "CELL" and cell.row_index is not None and cell.column_index is not None
        }
    )
    columns: Final = max((column for _, column in cells), default=0)
    rows: Final = tuple(sorted(frozenset(row for row, _ in cells)))
    rendered: Final = tuple(
        "| " + " | ".join(cells.get((row, column), "") for column in range(1, columns + 1)) + " |" for row in rows
    )
    divider: Final = "|" + " --- |" * columns
    return "\n".join(rendered[:1] + ((divider,) if rendered else ()) + rendered[1:])


def _layout_markdown(blocks: tuple[Block, ...], page: int, by_id: Mapping[str, Block]) -> str:
    on_page: Final = tuple(block for block in blocks if block.page == page)
    list_items: Final = frozenset(
        child for block in on_page if block.block_type == Layout.LIST.value for child in block.children
    )
    tables: Final = tuple(block for block in on_page if block.block_type == "TABLE")
    table_ordinal: Final = MappingProxyType(
        {block.id: ordinal for ordinal, block in enumerate(b for b in on_page if b.block_type == Layout.TABLE.value)}
    )

    def table_of(layout_table: Block) -> Block | None:
        linked: Final = next(
            (by_id[child] for child in layout_table.children if child in by_id and by_id[child].block_type == "TABLE"),
            None,
        )
        if linked is not None:
            return linked
        ordinal: Final = table_ordinal.get(layout_table.id)
        return tables[ordinal] if ordinal is not None and ordinal < len(tables) else None

    def section(block: Block, layout: Layout) -> str:
        match layout:
            case Layout.TITLE:
                return f"# {_text_of(block, by_id, ' ')}"
            case Layout.SECTION_HEADER:
                return f"## {_text_of(block, by_id, ' ')}"
            case Layout.LIST:
                return "\n".join(
                    f"- {strip_bullet(_text_of(by_id[child], by_id, ' '))}"
                    for child in block.children
                    if child in by_id
                )
            case Layout.TABLE:
                table: Final = table_of(block)
                return _table_markdown(table, by_id) if table is not None else _text_of(block, by_id, "\n")
            case Layout.KEY_VALUE:
                return _text_of(block, by_id, "\n")
            case Layout.FIGURE:
                return ""
            case Layout.TEXT | Layout.HEADER | Layout.FOOTER | Layout.PAGE_NUMBER:
                return _text_of(block, by_id, " ")

    sections: Final = (
        section(block, _LAYOUTS[block.block_type])
        for block in on_page
        if block.id not in list_items and block.block_type in _LAYOUTS
    )
    return "\n\n".join(text for text in sections if text.strip())


def layout_by_page(blocks: tuple[Block, ...]) -> tuple[tuple[int, str], ...]:
    if not any(block.block_type in _LAYOUTS for block in blocks):
        return lines_by_page(blocks)
    by_id: Final = MappingProxyType({block.id: block for block in blocks})
    rendered: Final = ((page, _layout_markdown(blocks, page, by_id)) for page in _pages(blocks))
    return tuple((page, markdown) for page, markdown in rendered if markdown)
