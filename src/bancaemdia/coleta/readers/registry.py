"""Exact provenance/version routing; no brand/platform fallback or first match."""

import re
from dataclasses import dataclass

from pydantic import ValidationError

from bancaemdia.coleta.readers.base import (
    BackendReader,
    ParsedCapture,
    ReaderEnvelope,
    SourceLocation,
    checked_bets,
    exact_host,
)
from bancaemdia.coleta.readers.errors import (
    IncompletePayloadError,
    SchemaDriftError,
    WrongHostError,
)
from bancaemdia.coleta.readers.safety import check_safe


@dataclass(frozen=True)
class ReaderRegistration:
    reader_id: str
    reader_version: str
    brand: str
    hostname: str
    envelope_schema: int
    raw_schema_version: str
    content_type: str
    channel: str
    endpoint: str
    frame_signature: str | None
    reader: BackendReader

    def __post_init__(self) -> None:
        if self.hostname != exact_host(self.hostname):
            raise ValueError("registration requires a normalized exact hostname")
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,63}", self.reader_id) or not re.fullmatch(
            r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", self.reader_version
        ):
            raise ValueError("stable reader ID and version required")
        if (
            self.envelope_schema != 1
            or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{1,39}", self.brand)
            or not re.fullmatch(r"[a-zA-Z0-9._-]{1,40}", self.raw_schema_version)
            or self.content_type != "application/json"
        ):
            raise ValueError("reviewed envelope, brand, JSON type and raw schema required")
        SourceLocation.model_validate({
            "channel": self.channel,
            "direction": "received_frame" if self.channel == "websocket" else "observed_response",
            "endpoint": self.endpoint,
            "frame_signature": self.frame_signature,
        })
        check_safe({
            "brand": self.brand,
            "hostname": self.hostname,
            "endpoint": self.endpoint,
            "frame_signature": self.frame_signature,
        })

    @property
    def routing_key(self) -> tuple[object, ...]:
        return (
            self.brand,
            self.hostname,
            self.envelope_schema,
            self.raw_schema_version,
            self.content_type,
            self.channel,
            self.endpoint,
            self.frame_signature,
        )


class ReaderRegistry:
    def __init__(self, registrations: tuple[ReaderRegistration, ...] = ()) -> None:
        self.registrations = registrations
        if len({r.routing_key for r in registrations}) != len(registrations):
            raise SchemaDriftError("ambiguous_reader_registration")

    def read(self, value: object) -> ParsedCapture:
        if isinstance(value, ReaderEnvelope):
            value = value.model_dump(mode="json")
        if not isinstance(value, dict):
            raise IncompletePayloadError()
        metadata = {k: v for k, v in value.items() if k != "payload_text"}
        check_safe(metadata)
        try:
            envelope = ReaderEnvelope.model_validate(value)
        except ValidationError as error:
            if any(e["type"] == "missing" for e in error.errors()):
                raise IncompletePayloadError() from None
            if any(tuple(e["loc"]) == ("hostname",) for e in error.errors()):
                raise WrongHostError() from None
            raise SchemaDriftError("invalid_envelope") from None
        if envelope.envelope_schema != 1:
            raise SchemaDriftError("unknown_envelope_schema")
        if envelope.content_type != "application/json":
            raise SchemaDriftError("unknown_reader_schema_or_source")
        # Screening does not depend on finding a reader: unknown versions must not retain secrets.
        from bancaemdia.coleta.readers.base import decode_raw

        decode_raw(envelope.payload_text)
        host = [
            r
            for r in self.registrations
            if (r.brand, r.hostname) == (envelope.brand, envelope.hostname)
        ]
        if not host:
            raise WrongHostError()
        key = (
            envelope.brand,
            envelope.hostname,
            envelope.envelope_schema,
            envelope.raw_schema_version,
            envelope.content_type,
            envelope.source.channel,
            envelope.source.endpoint,
            envelope.source.frame_signature,
        )
        matches = [r for r in host if r.routing_key == key]
        if len(matches) != 1:
            raise SchemaDriftError(
                "unknown_reader_schema_or_source" if not matches else "ambiguous_reader"
            )
        registration = matches[0]
        return ParsedCapture(
            reader_id=registration.reader_id,
            reader_version=registration.reader_version,
            raw_schema_version=envelope.raw_schema_version,
            content_hash=envelope.content_hash,
            captured_at=envelope.captured_at,
            bets=checked_bets(registration.reader, envelope),
        )


# The six inherited brand-based readers are migrated with fresh captures in #115.
# An empty production registry is intentional: a vendor name is not exact-host evidence.
DEFAULT_REGISTRY = ReaderRegistry()
