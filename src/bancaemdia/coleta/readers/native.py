"""Version 2 money contract: source units with an explicit denomination, never floats."""

import re
from datetime import datetime
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import (
    AwareDatetime,
    BeforeValidator,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from bancaemdia.coleta.readers.base import CanonicalSelection, StrictModel, canonical_bytes, digest
from bancaemdia.coleta.readers.errors import SchemaDriftError

Currency = Literal["BRL", "USDT"]


def exact_amount(value: object) -> Decimal:
    if isinstance(value, str):
        if not re.fullmatch(r"-?\d{1,20}(?:\.\d{1,30})?", value):
            raise ValueError("exact decimal text required")
        value = Decimal(value)
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise ValueError("floating point money is not accepted")
    number = Decimal(value)
    if not number.is_finite() or number.copy_abs() >= 10**20:
        raise ValueError("native money out of range")
    # NUMERIC(50,30) must never silently round a source amount.
    with localcontext() as context:
        context.prec = 80
        if number != number.quantize(Decimal("1e-30")):
            raise ValueError("native money precision exceeds storage")
    return number


Amount = Annotated[Decimal, BeforeValidator(exact_amount)]


def money_text(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


class NativeCanonicalBet(StrictModel):
    money_contract: Literal[2] = 2
    reader_id: Literal["1win_history_candidate"] = "1win_history_candidate"
    reader_version: Literal["0.2.0"] = "0.2.0"
    external_identity: Annotated[str, Field(strict=True, min_length=1, max_length=120)]
    brand: Literal["1win"]
    hostname: Literal["api-gateway.top-parser.com"]
    currency: Currency
    placed_at: AwareDatetime
    source_updated_at: None = None
    revision_policy: Literal["unversioned_conflict_review"] = "unversioned_conflict_review"
    state: Literal["GREEN", "RED"]
    stake: Annotated[Amount, Field(gt=0)]
    returned: Annotated[Amount, Field(ge=0)]
    odds: Annotated[Decimal, Field(ge=1, le=1000, decimal_places=6)]
    selections: list[CanonicalSelection] = Field(min_length=1, max_length=100)

    @field_validator("selections")
    @classmethod
    def stable_selections(cls, value: list[CanonicalSelection]) -> list[CanonicalSelection]:
        return sorted(
            value,
            key=lambda selection: (
                selection.event_id,
                selection.description,
                selection.market or "",
            ),
        )

    @model_validator(mode="after")
    def contract(self) -> "NativeCanonicalBet":
        if self.state == "RED" and self.returned != 0:
            raise ValueError("lost source bet cannot assert a positive return")
        if self.state == "GREEN" and self.returned == 0:
            raise ValueError("winning cash source bet requires a positive gross return")
        return self

    @field_serializer("stake", "returned", "odds")
    def serialize_money(self, value: Decimal) -> str:
        return money_text(value)

    @property
    def game_at(self) -> datetime:
        return min(selection.starts_at for selection in self.selections)

    @property
    def identity_hash(self) -> str:
        return digest(canonical_bytes([self.brand, self.hostname, self.external_identity]))

    @property
    def canonical_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))

    @property
    def profit(self) -> Decimal:
        with localcontext() as context:
            context.prec = 80
            return self.returned - self.stake


def validated_native(value: object) -> NativeCanonicalBet:
    try:
        return NativeCanonicalBet.model_validate(value)
    except ValueError:
        raise SchemaDriftError("native_money_contract_invalid") from None
