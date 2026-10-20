"""Public spreadsheet workflow; transport never decides financial facts."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StrictInt


class SheetModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceInput(SheetModel):
    nome: str = Field(min_length=1, max_length=100)


class SourceView(SheetModel):
    id: UUID
    origem_id: str
    nome: str
    version: int
    current_preview: UUID | None


class SourcePage(SheetModel):
    data: list[SourceView]
    total: int
    page: int
    page_size: int


class FieldView(SheetModel):
    field: str
    label: str
    type: Literal["text", "date-time", "decimal", "boolean", "account-id"]
    required: bool
    options: list[JsonValue]


class FieldCatalog(SheetModel):
    fields: list[FieldView]
    formats: list[Literal["xlsx"]]
    max_bytes: int
    max_sheets: int
    max_rows: int
    max_columns: int
    max_cells: int
    max_inflated_bytes: int
    max_expansion_ratio: int
    max_parse_seconds: int
    max_operation_seconds: int
    max_cell_characters: int
    daily_previews: int


class SheetHeader(SheetModel):
    name: str
    headers: list[str | None]
    total_rows: int


class FieldError(SheetModel):
    field: str | None
    code: str


class Counts(SheetModel):
    criadas: int
    atualizadas: int
    ignoradas: int
    recusadas: int
    revisoes: int


class PreviewView(SheetModel):
    id: UUID
    source_id: UUID
    origem_id: str
    revision: int
    source_version: int
    preview_hash: str
    file_hash: str
    state: Literal["ready", "invalid", "obsolete"]
    sheets: list[SheetHeader]
    sheet: str
    mapping: dict[str, int]
    mapping_errors: list[FieldError]
    total_rows: int
    counts: Counts
    expires_at: datetime


class PreviewRow(SheetModel):
    sheet: str
    number: int
    linha_id: str | None
    raw: list[JsonValue]
    normalized: dict[str, JsonValue]
    errors: list[FieldError]
    consequence: Literal["create", "update", "ignore", "refuse"]
    bet_key: str | None
    review_reason: str | None
    stake_centavos: int | None
    valor_aposta_centavos: int | None
    conta_casa_id: int | None


class PreviewPage(SheetModel):
    data: list[PreviewRow]
    total: int
    page: int
    page_size: int


class RemapInput(SheetModel):
    expected_revision: StrictInt = Field(ge=1)
    sheet: str = Field(min_length=1, max_length=31)
    mapping: dict[str, StrictInt] = Field(max_length=18)


class ConfirmInput(SheetModel):
    preview_id: UUID
    preview_hash: str = Field(pattern="^[0-9a-f]{64}$")


class AppliedRow(SheetModel):
    linha_id: str
    sheet: str
    number: int
    outcome: Literal["created", "updated", "ignored"]
    bet_key: str
    review_ids: list[int]


class ConfirmationView(SheetModel):
    id: UUID
    preview_id: UUID
    source_id: UUID
    source_version: int
    state: Literal["completed"]
    atomic: Literal[True]
    counts: Counts
    rows: list[AppliedRow]
    expires_at: datetime
    aggregate_freshness: Literal["materialized_views_eventual"]
