"""Public states and limits; processing money never enters the web contract."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

MAX_ITEMS = 20
MAX_INTENT_BYTES = 100 * 1024 * 1024
STATE_DAYS = 90
MEDIA_DAYS = 7
MAX_ATTEMPTS = 3


class PrintResult(BaseModel):
    type: Literal["materialized", "review"]
    bet_key: str | None
    review_ids: list[int]


class PrintItemResponse(BaseModel):
    intent_id: UUID
    item_id: UUID
    status: Literal["accepted", "processing", "completed", "failed"]
    outcome: Literal["materialized", "review", "no_bet", "duplicate", "failed"] | None
    duplicate: bool
    attempt: int
    last_retry_id: UUID | None
    retryable: bool
    error_code: (
        Literal["unavailable", "account_read_only", "media_expired", "invalid_reading"] | None
    )
    progress_percent: int | None
    results: list[PrintResult] | None
    media_expires_at: datetime
    reconciliation_expires_at: datetime


class PrintIntentResponse(BaseModel):
    intent_id: UUID
    state: Literal["processing", "complete", "partial", "failed"]
    items: list[PrintItemResponse]
    received_items: int
    terminal_items: int
    reconciliation_expires_at: datetime


class RetryPrint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    retry_id: UUID
    expected_attempt: int = Field(ge=1, le=MAX_ATTEMPTS)
