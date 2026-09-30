"""Executable authoring example, exclusively synthetic; never a production bookmaker adapter."""

import re
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from pydantic import AwareDatetime, Field, ValidationError

from bancaemdia.coleta.readers.base import (
    CanonicalBet,
    CanonicalSelection,
    ReaderEnvelope,
    StrictModel,
    decode_raw,
)
from bancaemdia.coleta.readers.errors import (
    IncompletePayloadError,
    SchemaDriftError,
    UnsupportedMarketError,
)
from bancaemdia.coleta.readers.registry import ReaderRegistration, ReaderRegistry
from bancaemdia.domain.financeiro import Estado


def decimal_number(value: object, *, currency: bool = False) -> Decimal:
    if (
        isinstance(value, bool)
        or isinstance(value, float)
        or not isinstance(value, (str, int, Decimal))
    ):
        raise SchemaDriftError("financial_number_not_exact")
    if isinstance(value, str):
        text = value.strip()
        if currency and text.startswith("R$ "):
            text = text[3:]
        if "," in text:
            if not re.fullmatch(r"(?:\d{1,3}(?:\.\d{3})*|\d+),\d+", text):
                raise SchemaDriftError("ambiguous_locale_number")
            text = text.replace(".", "").replace(",", ".")
        elif not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise SchemaDriftError("ambiguous_locale_number")
        value = text
    try:
        result = Decimal(value)
    except InvalidOperation:
        raise SchemaDriftError("invalid_decimal") from None
    if not result.is_finite():
        raise SchemaDriftError("invalid_decimal")
    return result


def money(value: object) -> int:
    number = decimal_number(value, currency=True)
    with localcontext() as context:
        context.prec = max(28, len(number.as_tuple().digits) + 3)
        result = number * 100
    if result != result.to_integral_value():
        raise SchemaDriftError("fractional_centavo")
    return int(result)


class RawSelection(StrictModel):
    event_id: str
    event: str
    starts_at: AwareDatetime
    selection: str
    market: str | None = None


class RawBet(StrictModel):
    ticket_id: str
    placed_at: AwareDatetime
    revised_at: AwareDatetime
    status: str
    stake: object
    odds: object
    returned: object = None
    currency: Literal["BRL"]
    selections: list[RawSelection] = Field(min_length=1, max_length=100)
    note: str | None = None


class RawSnapshot(StrictModel):
    source_schema: Literal["example-1"]
    bets: list[RawBet] = Field(min_length=1, max_length=1000)


STATES = {
    "OPEN": Estado.PENDENTE,
    "WIN": Estado.GREEN,
    "LOSE": Estado.RED,
    "VOID": Estado.ANULADA,
    "CASHOUT": Estado.CASHOUT,
    "HALF_WIN": Estado.MEIO_GREEN,
    "HALF_LOSE": Estado.MEIO_RED,
}


class ReferenceReader:
    def parse(self, envelope: ReaderEnvelope) -> list[CanonicalBet]:
        try:
            snapshot = RawSnapshot.model_validate(decode_raw(envelope.payload_text))
        except ValidationError as error:
            if any(e["type"] == "missing" for e in error.errors()):
                raise IncompletePayloadError() from None
            raise SchemaDriftError("reference_source_shape_changed") from None
        bets = []
        for raw in snapshot.bets:
            if raw.status not in STATES:
                raise UnsupportedMarketError()
            bets.append(
                CanonicalBet(
                    external_identity=raw.ticket_id,
                    brand=envelope.brand,
                    hostname=envelope.hostname,
                    placed_at=raw.placed_at,
                    source_updated_at=raw.revised_at,
                    state=STATES[raw.status],
                    stake_centavos=money(raw.stake),
                    odds=decimal_number(raw.odds),
                    return_centavos=None if raw.returned is None else money(raw.returned),
                    selections=[
                        CanonicalSelection(
                            event_id=s.event_id,
                            event=s.event,
                            starts_at=s.starts_at,
                            description=s.selection,
                            market=s.market,
                        )
                        for s in raw.selections
                    ],
                    note=raw.note,
                )
            )
        return bets


def reference_registry() -> ReaderRegistry:
    return ReaderRegistry((
        ReaderRegistration(
            "synthetic_reference",
            "1.0.0",
            "example",
            "reader.example.invalid",
            1,
            "example-1",
            "application/json",
            "fetch",
            "/history",
            None,
            ReferenceReader(),
        ),
    ))
