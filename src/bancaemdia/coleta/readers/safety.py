"""Conservative fixture/capture screening; human review is an additional required gate."""

import math
import re
from collections import Counter
from decimal import Decimal
from urllib.parse import urlsplit

from bancaemdia.coleta.readers.errors import UnsafePayloadError

KEY = re.compile(
    r"token|cookie|password|passwd|authorization|secret|session|credential|csrf|apikey|"
    r"email|cpf|cnpj|phone|telefone|celular|account|conta|customer|userid|username|"
    r"fullname|firstname|lastname|address|endereco|passport|document|birth|ipaddress",
    re.I,
)
VALUE = re.compile(
    r"\b(?:Bearer|Basic)\s+\S+|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"\b(?:cti_|cpc_)[A-Za-z0-9_-]+|-----BEGIN .*PRIVATE KEY-----|"
    r"\bAKIA[A-Z0-9]{16}\b|[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}|"
    r"\b\d{3}\.\d{3}\.\d{3}-\d{2}\b|\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b|"
    r"\b(?:account|conta)\s*[:=]\s*\d+",
    re.I,
)
DDD = {
    str(n)
    for n in (
        11,
        12,
        13,
        14,
        15,
        16,
        17,
        18,
        19,
        21,
        22,
        24,
        27,
        28,
        31,
        32,
        33,
        34,
        35,
        37,
        38,
        41,
        42,
        43,
        44,
        45,
        46,
        47,
        48,
        49,
        51,
        53,
        54,
        55,
        61,
        62,
        63,
        64,
        65,
        66,
        67,
        68,
        69,
        71,
        73,
        74,
        75,
        77,
        79,
        81,
        82,
        83,
        84,
        85,
        86,
        87,
        88,
        89,
        91,
        92,
        93,
        94,
        95,
        96,
        97,
        98,
        99,
    )
}
HASH_FIELDS = {"content_hash", "identity_hash", "canonical_hash", "sha256"}
# Public GraphQL type enums in the inherited fixtures, not arbitrary identifier exceptions.
PUBLIC_TYPE_NAMES = frozenset({
    "BetCardGroup",
    "SportsbookBetCard",
    "SportsbookBet",
    "BetLeg",
    "SportsbookExpandableLegCardGroup",
    "SportsbookBetLegCardGroup",
    "BetLegCard",
    "SportsbookOdds",
    "LegPart",
    "FixtureCard",
    "FootballFixture",
    "SportsEvent",
})


def _cpf(value: str) -> bool:
    if not re.fullmatch(r"\d{11}", value) or len(set(value)) == 1:
        return False
    for length in (9, 10):
        digit = (sum(int(value[i]) * (length + 1 - i) for i in range(length)) * 10) % 11
        if digit % 10 != int(value[length]):
            return False
    return True


def _phone(value: str) -> bool:
    for match in re.finditer(
        r"(?<!\d)(?:\+?55[ .-]?)?\(?\d{2}\)?[ .-]?\d{4,5}[ .-]?\d{4}(?!\d)", value
    ):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) in (12, 13) and digits.startswith("55"):
            digits = digits[2:]
        if digits[:2] in DDD and (
            (len(digits) == 11 and digits[2] == "9") or (len(digits) == 10 and digits[2] in "2345")
        ):
            return True
    return False


def _entropy(value: str) -> bool:
    if len(value) < 24 or not re.fullmatch(r"[A-Za-z0-9_+/=-]+", value):
        return False
    classes = sum(bool(re.search(pattern, value)) for pattern in (r"[a-z]", r"[A-Z]", r"\d"))
    entropy = -sum((n / len(value)) * math.log2(n / len(value)) for n in Counter(value).values())
    return classes >= 2 and entropy > 4.0


def check_safe(value: object, *, field: str = "", depth: int = 0) -> None:
    if depth > 20:
        raise UnsafePayloadError("depth_limit")
    if isinstance(value, dict):
        if len(value) > 2000:
            raise UnsafePayloadError("object_limit")
        for key, child in value.items():
            if not isinstance(key, str) or KEY.search(re.sub(r"[^a-z0-9]", "", key.lower())):
                raise UnsafePayloadError("sensitive_field")
            check_safe(key, depth=depth + 1)
            check_safe(child, field=key, depth=depth + 1)
    elif isinstance(value, (list, tuple)):
        if len(value) > 2000:
            raise UnsafePayloadError("array_limit")
        for child in value:
            check_safe(child, depth=depth + 1)
    elif isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise UnsafePayloadError("invalid_unicode") from None
        if "\x00" in value or any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            raise UnsafePayloadError("control_character")
        if VALUE.search(value) or _phone(value) or _cpf(value):
            raise UnsafePayloadError("sensitive_value")
        if "://" in value:
            try:
                parsed = urlsplit(value)
            except ValueError:
                raise UnsafePayloadError("credential_url") from None
            if parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise UnsafePayloadError("credential_url")
        if (
            _entropy(value)
            and not (field in HASH_FIELDS and re.fullmatch(r"[0-9a-f]{64}", value))
            and not (field == "__typename" and value in PUBLIC_TYPE_NAMES)
        ):
            raise UnsafePayloadError("session_like_value")
    elif isinstance(value, (float, Decimal)):
        if not math.isfinite(value):
            raise UnsafePayloadError("non_finite_number")
    elif value is not None and not isinstance(value, (int, bool)):
        raise UnsafePayloadError("non_json_value")
