"""Check evidence completeness offline; never grant payment or rollout authority."""

from datetime import date, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

MAX_MANIFEST_BYTES = 16_384
MAX_EVIDENCE_AGE = timedelta(days=30)
CheckName = Literal[
    "account_verification",
    "account_commercial_conditions",
    "market_and_currency_scope",
    "tax_calculation_and_collection",
    "tax_reporting_and_invoicing",
    "tax_failure_and_reconciliation",
    "commercial_catalog",
    "terms_cancellation_refunds",
    "owner_launch_authorization",
]
CHECKS: tuple[CheckName, ...] = (
    "account_verification",
    "account_commercial_conditions",
    "market_and_currency_scope",
    "tax_calculation_and_collection",
    "tax_reporting_and_invoicing",
    "tax_failure_and_reconciliation",
    "commercial_catalog",
    "terms_cancellation_refunds",
    "owner_launch_authorization",
)
Role = Literal["owner", "account_operator", "fiscal_adviser"]


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    status: Literal["pending", "verified"] = "pending"
    evidence_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    reviewed_on: date | None = None
    reviewer_role: Role | None = None


class AccountSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    mode: Literal["unverified", "sandbox", "production"] = "unverified"
    charges_enabled: bool | None = None
    payouts_enabled: bool | None = None
    details_submitted: bool | None = None
    requirements_due_count: int | None = Field(default=None, ge=0)
    requirements_pending_count: int | None = Field(default=None, ge=0)


class PreflightManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(ge=1, le=1)
    seller_country: Literal["BR"]
    account: AccountSnapshot
    checks: dict[CheckName, Evidence]


class PreflightReport(BaseModel):
    state: Literal["INVALID_INPUT", "INCOMPLETE", "OWNER_REVIEW_REQUIRED"]
    checked_on: date
    blocking_checks: list[str]
    # Not a GO decision, credential, account change, or rollout switch.
    production_authorized: Literal[False] = False


def assess_preflight(raw: bytes, *, today: date) -> PreflightReport:
    if len(raw) > MAX_MANIFEST_BYTES:
        return PreflightReport(
            state="INVALID_INPUT", checked_on=today, blocking_checks=["invalid_manifest"]
        )
    try:
        manifest = PreflightManifest.model_validate_json(raw)
    except ValidationError:
        # No raw value, exception message, document identifier or secret is echoed.
        return PreflightReport(
            state="INVALID_INPUT", checked_on=today, blocking_checks=["invalid_manifest"]
        )

    blockers: list[str] = []
    for name in CHECKS:
        evidence = manifest.checks.get(name)
        allowed_roles: set[Role] = {"owner"}
        if name in ("account_verification", "account_commercial_conditions"):
            allowed_roles = {"owner", "account_operator"}
        elif name.startswith("tax_"):
            allowed_roles = {"fiscal_adviser"}
        if (
            evidence is None
            or evidence.status != "verified"
            or evidence.evidence_sha256 is None
            or evidence.reviewed_on is None
            or not today - MAX_EVIDENCE_AGE <= evidence.reviewed_on <= today
            or evidence.reviewer_role not in allowed_roles
        ):
            blockers.append(name)

    account = manifest.account
    if (
        account.mode != "production"
        or account.charges_enabled is not True
        or account.payouts_enabled is not True
        or account.details_submitted is not True
        or account.requirements_due_count != 0
        or account.requirements_pending_count != 0
    ) and "account_verification" not in blockers:
        blockers.insert(0, "account_verification")

    return PreflightReport(
        state="INCOMPLETE" if blockers else "OWNER_REVIEW_REQUIRED",
        checked_on=today,
        blocking_checks=blockers,
    )
