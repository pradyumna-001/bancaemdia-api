"""Versioned 1win source interpretation, pending reviewed financial/revision evidence.

The client projects one item per capture. Observation is deliberately not a
CanonicalBet: displayed amounts and public status labels cannot authorize money.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, localcontext
from typing import Annotated, Literal

from pydantic import BeforeValidator, Field, ValidationError, model_validator

from bancaemdia.coleta.readers.base import (
    CanonicalBet,
    CanonicalSelection,
    ReaderEnvelope,
    StrictModel,
    canonical_bytes,
    decode_raw,
    digest,
)
from bancaemdia.coleta.readers.errors import (
    IncompletePayloadError,
    SchemaDriftError,
    WrongHostError,
)
from bancaemdia.coleta.readers.native import NativeCanonicalBet, validated_native
from bancaemdia.coleta.readers.registry import ReaderRegistration, ReaderRegistry
from bancaemdia.domain.financeiro import Estado

READER_ID = "1win_history_candidate"
READER_VERSION = "0.2.0"
RESPONSE_HOST = "api-gateway.top-parser.com"
ENDPOINT = "/bets/history/get-many"
V1_SCHEMA = "1win-history-fields-v1"
V2_SCHEMA = "1win-history-game-v2"
PUBLIC_STATES = {
    0: Estado.PENDENTE,
    1: Estado.RED,
    2: Estado.GREEN,
    3: Estado.ANULADA,
    4: Estado.CASHOUT,
}


def source_number(value: object) -> Decimal:
    # decode_raw preserves JSON decimal lexemes; no float or string coercion.
    if isinstance(value, bool) or not isinstance(value, (int, Decimal)):
        raise SchemaDriftError("financial_number_not_exact")
    number = Decimal(value)
    if not number.is_finite() or number.copy_abs() > 10**15 or len(number.as_tuple().digits) > 128:
        raise SchemaDriftError("invalid_decimal")
    return number


Number = Annotated[Decimal, BeforeValidator(source_number)]
Identifier = (
    Annotated[str, Field(strict=True, min_length=1, max_length=98)]
    | Annotated[int, Field(strict=True, ge=0, le=2**53 - 1)]
)
Text = Annotated[str, Field(strict=True, min_length=1, max_length=200)]


class SourceProjection(StrictModel):
    @model_validator(mode="before")
    @classmethod
    def projected_types(cls, value: object) -> object:
        if isinstance(value, dict) and any(
            (item is None and key != "startAt") or (isinstance(item, str) and not item.strip())
            for key, item in value.items()
        ):
            raise ValueError("optional source fields may be absent, not null or blank")
        return value


class SourceBet(SourceProjection):
    id: Annotated[str, Field(strict=True, min_length=1, max_length=120)]
    createdAt: Text  # ruff: ignore[mixed-case-variable-in-class-scope] -- exact source names, never wire renames
    # USDT was observed in the reviewed source. This preserves its denomination;
    # accepting source data does not authorize posting it to the centavo ledger.
    currencyCode: Annotated[str, Field(strict=True, pattern=r"^(?:[A-Z]{3}|USDT)$")]  # ruff: ignore[mixed-case-variable-in-class-scope]
    wallet: Text
    status: Annotated[int, Field(strict=True)]
    amount: Number
    cf: Annotated[Number, Field(ge=1, le=1000, decimal_places=6)]
    profitAmount: Number  # ruff: ignore[mixed-case-variable-in-class-scope]
    freebetAmount: Number  # ruff: ignore[mixed-case-variable-in-class-scope]
    bonusAmount: Number  # ruff: ignore[mixed-case-variable-in-class-scope]
    bonusPercent: Number  # ruff: ignore[mixed-case-variable-in-class-scope]


class GameBet(SourceBet):
    betType: Literal["ordinary", "express"] | None = None  # ruff: ignore[mixed-case-variable-in-class-scope]


class Competitor(SourceProjection):
    name: Text


class Match(SourceProjection):
    id: Identifier
    startAt: object = None  # ruff: ignore[mixed-case-variable-in-class-scope]
    competitors: list[Competitor] | None = Field(default=None, max_length=10)


class Odd(SourceProjection):
    id: Identifier
    name: Text | None = None
    groupName: Annotated[str, Field(strict=True, min_length=1, max_length=120)] | None = None  # ruff: ignore[mixed-case-variable-in-class-scope]
    cf: Annotated[Number, Field(ge=1, le=1000, decimal_places=6)] | None = None


class Selection(SourceProjection):
    match: Match
    odd: Odd
    status: Annotated[int, Field(strict=True)] | None = None
    isHalfReturn: Annotated[bool, Field(strict=True)] | None = None  # ruff: ignore[mixed-case-variable-in-class-scope]


class GameItem(StrictModel):
    bet: GameBet
    selections: list[Selection] = Field(min_length=1, max_length=100)


def source_id(value: str | int) -> str:
    # Candidate projection preserves native ID types; do not collapse 1 and "1".
    return f"{'s' if isinstance(value, str) else 'n'}:{value}"


def game_time(value: object) -> datetime:
    if value is None:
        raise IncompletePayloadError("game_time_missing")
    if isinstance(value, str):
        # The candidate allows numeric strings, but never date/locale guessing.
        import re

        if not re.fullmatch(r"\d{1,12}(?:\.\d{1,6})?", value):
            raise SchemaDriftError("invalid_game_time")
        value = Decimal(value)
    seconds = source_number(value)
    with localcontext() as context:
        context.prec = max(28, len(seconds.as_tuple().digits) + 7)
        microseconds = seconds * 1_000_000
    if seconds <= 0 or microseconds != microseconds.to_integral_value():
        raise SchemaDriftError("invalid_game_time")
    try:
        return datetime(1970, 1, 1, tzinfo=UTC) + timedelta(microseconds=int(microseconds))
    except OverflowError:
        raise SchemaDriftError("invalid_game_time") from None


def public_state(value: int) -> Estado:
    try:
        return PUBLIC_STATES[value]
    except KeyError:
        raise SchemaDriftError("unknown_source_state") from None


@dataclass(frozen=True)
class Observation:
    external_identity: str
    identity_hash: str
    source_content_hash: str
    placed_at: datetime
    public_state: Estado
    currency: str
    displayed_amount: Decimal
    source_profit_amount: Decimal
    odds: Decimal
    freebet_amount: Decimal
    bonus_amount: Decimal
    bonus_percent: Decimal
    selections: tuple[CanonicalSelection, ...]
    half_return_flags: tuple[bool | None, ...]
    bet_type: str | None

    @property
    def game_at(self) -> datetime:
        return min(selection.starts_at for selection in self.selections)


class OneWinReader:
    def parse_native(self, envelope: ReaderEnvelope) -> NativeCanonicalBet:
        """Interpret only the observed ordinary/express cash GREEN/RED source variants.

        Review/production admission is a separate boundary. The native contract
        deliberately does not invent an authoritative source revision timestamp.
        """
        observation = self.inspect(envelope)
        if (
            observation.currency != "USDT"
            or observation.public_state not in {Estado.GREEN, Estado.RED}
            or any(
                value is None or value != 0
                for value in (
                    observation.freebet_amount,
                    observation.bonus_amount,
                    observation.bonus_percent,
                )
            )
            or any(flag is not False for flag in observation.half_return_flags)
            or observation.bet_type not in {"ordinary", "express"}
            or (observation.bet_type == "ordinary" and len(observation.selections) != 1)
            or (observation.bet_type == "express" and len(observation.selections) < 2)
            or any(
                selection.result not in {Estado.GREEN, Estado.RED}
                for selection in observation.selections
            )
            or (
                observation.public_state == Estado.GREEN
                and any(selection.result != Estado.GREEN for selection in observation.selections)
            )
            or (
                observation.public_state == Estado.RED
                and not any(selection.result == Estado.RED for selection in observation.selections)
            )
        ):
            raise IncompletePayloadError("native_source_variant_not_evidenced")
        return validated_native({
            "external_identity": observation.external_identity,
            "brand": envelope.brand,
            "hostname": envelope.hostname,
            "currency": observation.currency,
            "placed_at": observation.placed_at,
            "state": observation.public_state.value,
            "stake": observation.displayed_amount,
            "returned": observation.source_profit_amount,
            "odds": observation.odds,
            "selections": observation.selections,
        })

    def inspect(self, envelope: ReaderEnvelope) -> Observation:
        """Extract proved source concepts without asserting a financial settlement."""
        envelope = ReaderEnvelope.model_validate(envelope.model_dump(mode="json"))
        raw = decode_raw(envelope.payload_text)
        if envelope.brand != "1win" or envelope.hostname != RESPONSE_HOST:
            raise WrongHostError()
        if (
            envelope.envelope_schema != 1
            or envelope.content_type != "application/json"
            or envelope.source.channel not in {"fetch", "xhr"}
            or envelope.source.endpoint != ENDPOINT
        ):
            raise SchemaDriftError("unknown_reader_schema_or_source")
        try:
            if envelope.raw_schema_version == V1_SCHEMA:
                SourceBet.model_validate(raw)
                raise IncompletePayloadError("game_fields_not_projected")
            if envelope.raw_schema_version != V2_SCHEMA:
                raise SchemaDriftError("unknown_reader_schema_or_source")
            item = GameItem.model_validate(raw)
        except ValidationError as error:
            if any(e["type"] == "missing" for e in error.errors()):
                raise IncompletePayloadError() from None
            raise SchemaDriftError("one_win_source_shape_changed") from None
        bet = item.bet
        try:
            placed_at = datetime.fromisoformat(bet.createdAt.replace("Z", "+00:00"))
        except ValueError:
            raise SchemaDriftError("invalid_placement_time") from None
        if placed_at.tzinfo is None:
            raise SchemaDriftError("invalid_placement_time")
        selections = []
        flags = []
        seen = set()
        for selection in item.selections:
            key = (source_id(selection.match.id), source_id(selection.odd.id))
            if key in seen:
                raise SchemaDriftError("duplicate_source_selection")
            seen.add(key)
            starts_at = game_time(selection.match.startAt)
            if not selection.match.competitors or not selection.odd.name:
                raise IncompletePayloadError("selection_description_missing")
            event = " x ".join(c.name for c in selection.match.competitors)
            if len(event) > 200:
                raise SchemaDriftError("one_win_source_shape_changed")
            selections.append(
                CanonicalSelection(
                    event_id=source_id(selection.match.id),
                    event=event,
                    starts_at=starts_at,
                    description=selection.odd.name,
                    market=selection.odd.groupName,
                    odds=selection.odd.cf,
                    result=None if selection.status is None else public_state(selection.status),
                )
            )
            flags.append(selection.isHalfReturn)
        return Observation(
            external_identity=bet.id,
            identity_hash=digest(canonical_bytes([envelope.brand, envelope.hostname, bet.id])),
            source_content_hash=envelope.content_hash,
            placed_at=placed_at.astimezone(UTC),
            public_state=public_state(bet.status),
            currency=bet.currencyCode,
            displayed_amount=bet.amount,
            source_profit_amount=bet.profitAmount,
            odds=bet.cf,
            freebet_amount=bet.freebetAmount,
            bonus_amount=bet.bonusAmount,
            bonus_percent=bet.bonusPercent,
            selections=tuple(selections),
            half_return_flags=tuple(flags),
            bet_type=bet.betType,
        )

    def parse(self, envelope: ReaderEnvelope) -> list[CanonicalBet]:
        self.inspect(envelope)
        # Neither profitAmount nor capture time proves authoritative return/revision.
        # Do not materialize even OPEN until stake/revision/corpus are established.
        raise IncompletePayloadError("financial_evidence_pending")


def candidate_registry() -> ReaderRegistry:
    """Explicit authoring/quarantine boundary, never DEFAULT_REGISTRY or support proof."""
    return ReaderRegistry(
        tuple(
            ReaderRegistration(
                READER_ID,
                READER_VERSION,
                "1win",
                RESPONSE_HOST,
                1,
                schema,
                "application/json",
                channel,
                ENDPOINT,
                None,
                OneWinReader(),
            )
            for schema in (V1_SCHEMA, V2_SCHEMA)
            for channel in ("fetch", "xhr")
        )
    )
