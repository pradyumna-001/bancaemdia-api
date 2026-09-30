"""Lossless sanitized source text -> canonical backend bets, independently of transport."""

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from ipaddress import ip_address
from typing import Literal, Protocol

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from bancaemdia.coleta.readers.errors import IncompletePayloadError, ReaderError, SchemaDriftError
from bancaemdia.coleta.readers.safety import check_safe
from bancaemdia.domain.financeiro import Estado


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, revalidate_instances="always")


def exact_host(value: str) -> str:
    if value != value.strip() or any(c in value for c in "*/:@?#\\%"):
        raise ValueError("exact hostname required")
    value = value.removesuffix(".").encode("idna").decode("ascii").lower()
    if (
        len(value) > 253
        or "." not in value
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", part) for part in value.split(".")
        )
    ):
        raise ValueError("invalid hostname")
    try:
        ip_address(value)
    except ValueError:
        pass
    else:
        raise ValueError("IP is not an integration hostname")
    if value.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private host")
    return value


def digest(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def canonical_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def decode_raw(text: str) -> object:
    def unique(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise SchemaDriftError("duplicate_json_key")
            result[key] = value
        return result

    try:
        result = json.loads(text, parse_float=Decimal, object_pairs_hook=unique)
    except ReaderError:
        raise
    except (ValueError, RecursionError):
        raise SchemaDriftError("invalid_source_json") from None
    check_safe(result)
    return result


class SourceLocation(StrictModel):
    channel: Literal["fetch", "xhr", "websocket"]
    direction: Literal["observed_response", "received_frame"]
    endpoint: str = Field(min_length=1, max_length=256, pattern=r"^/[^?#\\\s]*$")
    frame_signature: str | None = Field(default=None, pattern=r"^[a-zA-Z0-9._:-]{1,64}$")

    @model_validator(mode="after")
    def passive(self) -> "SourceLocation":
        if (self.channel == "websocket") != (self.direction == "received_frame"):
            raise ValueError("passive source channel/direction mismatch")
        if self.channel == "websocket" and self.frame_signature is None:
            raise ValueError("received frame requires a reviewed signature")
        if self.channel != "websocket" and self.frame_signature is not None:
            raise ValueError("HTTP response cannot assert a frame signature")
        return self


class ReaderEnvelope(StrictModel):
    envelope_schema: int = Field(strict=True, ge=1, le=1000)
    brand: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,39}$")
    hostname: str
    source: SourceLocation
    captured_at: AwareDatetime
    raw_schema_version: str = Field(pattern=r"^[a-zA-Z0-9._-]{1,40}$")
    content_type: str = Field(min_length=1, max_length=80)
    payload_text: str = Field(min_length=1, max_length=128 * 1024)
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("hostname")
    @classmethod
    def host(cls, value: str) -> str:
        return exact_host(value)

    @model_validator(mode="after")
    def verify_raw(self) -> "ReaderEnvelope":
        encoded = self.payload_text.encode("utf-8")
        if len(encoded) > 128 * 1024:
            raise ValueError("source byte limit")
        if digest(encoded) != self.content_hash:
            raise ValueError("source hash mismatch")
        return self


class CanonicalSelection(StrictModel):
    event_id: str = Field(min_length=1, max_length=100)
    event: str = Field(min_length=1, max_length=200)
    starts_at: AwareDatetime
    description: str = Field(min_length=1, max_length=200)
    market: str | None = Field(default=None, max_length=120)
    odds: Decimal | None = Field(default=None, ge=1, le=1000, decimal_places=6)
    result: Estado | None = None

    @field_validator("starts_at")
    @classmethod
    def utc_start(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_serializer("odds")
    def serialize_odds(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value.normalize(), "f")


class CanonicalBet(StrictModel):
    external_identity: str = Field(min_length=1, max_length=120)
    brand: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,39}$")
    hostname: str
    placed_at: AwareDatetime
    source_updated_at: AwareDatetime
    state: Estado
    stake_centavos: int = Field(strict=True, gt=0, le=10**12)
    odds: Decimal = Field(ge=1.01, le=1000, decimal_places=6)
    return_centavos: int | None = Field(default=None, strict=True, ge=0, le=10**15)
    selections: list[CanonicalSelection] = Field(min_length=1, max_length=100)
    note: str | None = Field(default=None, max_length=300)

    @field_validator("placed_at", "source_updated_at")
    @classmethod
    def utc_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)

    @field_validator("selections")
    @classmethod
    def stable_selections(cls, value: list[CanonicalSelection]) -> list[CanonicalSelection]:
        return sorted(value, key=lambda s: (s.event_id, s.description, s.market or ""))

    @field_serializer("odds")
    def serialize_odds(self, value: Decimal) -> str:
        return format(value.normalize(), "f")

    @field_validator("hostname")
    @classmethod
    def host(cls, value: str) -> str:
        return exact_host(value)

    @model_validator(mode="after")
    def financial_contract(self) -> "CanonicalBet":
        if self.source_updated_at < self.placed_at:
            raise ValueError("source revision predates placement")
        if self.state == Estado.PENDENTE and self.return_centavos is not None:
            raise ValueError("open bet cannot assert a final return")
        if self.state != Estado.PENDENTE and self.return_centavos is None:
            raise ValueError("settled bet requires authoritative return")
        if self.state == Estado.RED and self.return_centavos != 0:
            raise ValueError("lost bet cannot assert positive return")
        check_safe(self.model_dump(mode="json"))
        return self

    @property
    def game_at(self) -> datetime:
        return min(s.starts_at for s in self.selections).astimezone(UTC)

    @property
    def identity_hash(self) -> str:
        return digest(canonical_bytes([self.brand, self.hostname, self.external_identity]))

    @property
    def canonical_hash(self) -> str:
        return digest(canonical_bytes(self.model_dump(mode="json")))


class ParsedCapture(StrictModel):
    reader_id: str
    reader_version: str
    raw_schema_version: str
    content_hash: str
    captured_at: AwareDatetime
    bets: list[CanonicalBet] = Field(min_length=1, max_length=1000)


class BackendReader(Protocol):
    def parse(self, envelope: ReaderEnvelope) -> list[CanonicalBet]: ...


def checked_bets(reader: BackendReader, envelope: ReaderEnvelope) -> list[CanonicalBet]:
    try:
        bets = [
            CanonicalBet.model_validate(b.model_dump(mode="json")) for b in reader.parse(envelope)
        ]
    except ReaderError:
        raise
    except Exception:
        raise SchemaDriftError("reader_output_invalid") from None
    if not bets:
        raise IncompletePayloadError("empty_reader_result")
    if len({b.identity_hash for b in bets}) != len(bets):
        raise SchemaDriftError("duplicate_external_identity")
    if any(b.brand != envelope.brand or b.hostname != envelope.hostname for b in bets):
        raise SchemaDriftError("reader_changed_provenance")
    return sorted(bets, key=lambda b: b.identity_hash)
