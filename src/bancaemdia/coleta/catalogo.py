"""Exact-host catalog contracts. Regulatory observations never grant technical support."""

import hashlib
import json
import re
import unicodedata
from datetime import datetime
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

type Support = Literal[
    "nao_avaliado",
    "precisa_captura",
    "em_desenvolvimento",
    "suportado",
    "bloqueado_externo",
    "regressao",
]
type Regulatory = Literal[
    "autorizada",
    "judicial",
    "solicitante",
    "suspensa",
    "revogada",
    "expirada",
    "removida_da_fonte",
    "acesso_confirmado",
    "nao_avaliada",
]
UFS = tuple(
    "AC AL AP AM BA CE DF ES GO MA MT MS MG PA PB PR PE PI RJ RN RS RO RR SC SP SE TO".split()
)


def exact_host(value: str) -> str:
    if value != value.strip() or any(c in value for c in "*/:@?#\\%"):
        raise ValueError(
            "an exact hostname, without URL, wildcard, port or credentials, is required"
        )
    host = value.removesuffix(".").encode("idna").decode("ascii").lower()
    if (
        len(host) > 253
        or "." not in host
        or not all(
            re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
            for label in host.split(".")
        )
    ):
        raise ValueError("invalid hostname")
    try:
        ip_address(host)
    except ValueError:
        pass
    else:
        raise ValueError("IP addresses are not catalog hosts")
    if host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("private hostname")
    return host


def safe_url(value: str) -> str:
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("public HTTPS URL required")
    if parsed.port not in (None, 443) or parsed.query or parsed.fragment:
        raise ValueError("source URLs must not contain credentials, query or fragment")
    exact_host(parsed.hostname)
    return value


def canonical(value: object) -> bytes:
    # JCS-compatible subset: ASCII property names, strings, bool/null and safe integers; no floats.
    def check(item: object) -> None:
        if item is None or isinstance(item, (str, bool)):
            return
        if isinstance(item, int) and abs(item) <= 2**53 - 1:
            return
        if isinstance(item, list):
            for child in item:
                check(child)
            return
        if isinstance(item, dict) and all(isinstance(k, str) and k.isascii() for k in item):
            for child in item.values():
                check(child)
            return
        raise ValueError("catalog canonical subset excludes floats and non-ASCII property names")

    check(value)
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode()


def sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Technical(StrictModel):
    support: Support = "nao_avaliado"
    rollout: Literal["disabled", "canary", "enabled", "revoked"] = "disabled"
    adapter: str | None = Field(default=None, pattern=r"^[a-z0-9_-]{1,64}$")
    adapter_version: str | None = Field(default=None, pattern=r"^\d+\.\d+\.\d+$")
    schema_version: int = Field(default=1, ge=1, le=1000)
    minimum_client_version: str = Field(default="1.0.0", pattern=r"^\d+\.\d+\.\d+$")
    capture_evidence_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    aliases: list[str] = Field(default_factory=list, max_length=20)
    redirect_chain: list[str] = Field(default_factory=list, max_length=10)

    @field_validator("adapter_version", "minimum_client_version")
    @classmethod
    def stable_versions(cls, value: str | None) -> str | None:
        if value is not None:
            version(value)
        return value

    @field_validator("aliases")
    @classmethod
    def validate_aliases(cls, values: list[str]) -> list[str]:
        return sorted({exact_host(v) for v in values})

    @field_validator("redirect_chain")
    @classmethod
    def validate_chain(cls, values: list[str]) -> list[str]:
        for value in values:
            safe_url(value)
        if len(values) != len(set(values)):
            raise ValueError("redirect loop")
        return values

    @model_validator(mode="after")
    def supported_requires_evidence(self) -> "Technical":
        if self.support == "suportado" and not all((
            self.adapter,
            self.adapter_version,
            self.capture_evidence_sha256,
        )):
            raise ValueError("support requires a versioned adapter and fresh capture evidence")
        if self.rollout in ("enabled", "canary") and self.support != "suportado":
            raise ValueError("rollout requires proven technical support")
        return self


class Observation(StrictModel):
    brand: str = Field(min_length=1, max_length=120)
    hostname: str
    company: str = Field(default="", max_length=300)
    situation: Regulatory

    @field_validator("hostname")
    @classmethod
    def host(cls, value: str) -> str:
        return exact_host(value)

    @field_validator("brand")
    @classmethod
    def brand_name(cls, value: str) -> str:
        value = unicodedata.normalize("NFC", value).strip().upper()
        if not value or "|" in value or any(unicodedata.category(c).startswith("C") for c in value):
            raise ValueError("invalid brand")
        return value

    @property
    def key(self) -> str:
        return f"{self.brand}|{self.hostname}"


class Source(StrictModel):
    source_key: str = Field(pattern=r"^[a-z0-9_-]{1,80}$")
    kind: Literal["federal", "judicial", "state", "manual", "applicants"]
    jurisdiction: str
    url: str
    consulted_at: datetime
    valid_until: datetime
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    observations: list[Observation] = Field(max_length=20000)
    unresolved_domains: list[dict[str, str]] = Field(default_factory=list, max_length=1000)

    @field_validator("url")
    @classmethod
    def url_valid(cls, value: str) -> str:
        return safe_url(value)

    @model_validator(mode="after")
    def validate_source(self) -> "Source":
        if (
            self.consulted_at.utcoffset() is None
            or self.valid_until.utcoffset() is None
            or self.valid_until <= self.consulted_at
        ):
            raise ValueError("source requires timezone-aware consultation and bounded validity")
        if self.jurisdiction not in (*UFS, "BR", "manual"):
            raise ValueError("unknown jurisdiction")
        expected = {
            "federal": "autorizada",
            "judicial": "judicial",
            "applicants": "solicitante",
            "manual": "acesso_confirmado",
        }.get(self.kind)
        if expected and any(o.situation != expected for o in self.observations):
            raise ValueError("source cannot promote applicants/manual access into authorization")
        if len({o.key for o in self.observations}) != len(self.observations):
            raise ValueError("duplicate brand/hostname in source")
        allowed_origins = {"www.gov.br"}
        if self.kind == "applicants":
            allowed_origins.add("sigap.fazenda.gov.br")
        if (
            self.kind in ("federal", "judicial", "applicants")
            and urlsplit(self.url).hostname not in allowed_origins
        ):
            raise ValueError("federal evidence must originate from gov.br")
        return self


def normalize_redirects(chain: list[str]) -> tuple[str, list[str]]:
    if not chain or len(chain) > 10 or len(set(chain)) != len(chain):
        raise ValueError("bounded non-looping redirect evidence required")
    hosts = []
    for url in chain:
        safe_url(url)
        hosts.append(exact_host(urlsplit(url).hostname or ""))
    return hosts[-1], sorted(set(hosts[:-1]) - {hosts[-1]})


def source_diff(
    previous: list[Observation], source: Source, manual: list[str]
) -> dict[str, list[str]]:
    old = {o.key: o.model_dump() for o in previous}
    new = {o.key: o.model_dump() for o in source.observations}
    return {
        "added": sorted(new.keys() - old.keys()),
        "changed": sorted(k for k in old.keys() & new.keys() if old[k] != new[k]),
        "removed_from_source": sorted(old.keys() - new.keys()),
        "manual_unchanged": sorted(manual),
    }


def version(value: str) -> tuple[int, ...]:
    if re.fullmatch(r"(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)", value) is None:
        raise ValueError("three-component stable client version required")
    return tuple(int(part) for part in value.split("."))
