"""Stable diagnostics deliberately contain no captured values or parser exception text."""

from enum import StrEnum

REASONS = frozenset({
    "unrecognized_schema",
    "market_not_supported",
    "required_fields_missing",
    "exact_integration_unavailable",
    "unsafe_capture",
    "depth_limit",
    "object_limit",
    "sensitive_field",
    "array_limit",
    "invalid_unicode",
    "control_character",
    "sensitive_value",
    "credential_url",
    "session_like_value",
    "non_finite_number",
    "non_json_value",
    "duplicate_json_key",
    "invalid_source_json",
    "reader_output_invalid",
    "empty_reader_result",
    "duplicate_external_identity",
    "reader_changed_provenance",
    "ambiguous_reader_registration",
    "invalid_envelope",
    "unknown_envelope_schema",
    "unknown_reader_schema_or_source",
    "ambiguous_reader",
    "financial_number_not_exact",
    "ambiguous_locale_number",
    "invalid_decimal",
    "fractional_centavo",
    "reference_source_shape_changed",
    "reader_failure",
    "game_time_missing",
    "invalid_game_time",
    "unknown_source_state",
    "game_fields_not_projected",
    "one_win_source_shape_changed",
    "invalid_placement_time",
    "selection_description_missing",
    "financial_evidence_pending",
    "duplicate_source_selection",
})


class ErrorCode(StrEnum):
    SCHEMA_DRIFT = "schema_drift"
    UNSUPPORTED_MARKET = "unsupported_market"
    INCOMPLETE_PAYLOAD = "incomplete_payload"
    WRONG_HOST = "wrong_host"
    UNSAFE_PAYLOAD = "unsafe_payload"


class ReaderError(ValueError):
    def __init__(self, code: ErrorCode, reason: str) -> None:
        self.code, self.reason = code, reason if reason in REASONS else "reader_failure"
        super().__init__(f"{code.value}:{self.reason}")


class SchemaDriftError(ReaderError):
    def __init__(self, reason: str = "unrecognized_schema") -> None:
        super().__init__(ErrorCode.SCHEMA_DRIFT, reason)


class UnsupportedMarketError(ReaderError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.UNSUPPORTED_MARKET, "market_not_supported")


class IncompletePayloadError(ReaderError):
    def __init__(self, reason: str = "required_fields_missing") -> None:
        super().__init__(ErrorCode.INCOMPLETE_PAYLOAD, reason)


class WrongHostError(ReaderError):
    def __init__(self) -> None:
        super().__init__(ErrorCode.WRONG_HOST, "exact_integration_unavailable")


class UnsafePayloadError(ReaderError):
    def __init__(self, reason: str = "unsafe_capture") -> None:
        super().__init__(ErrorCode.UNSAFE_PAYLOAD, reason)
