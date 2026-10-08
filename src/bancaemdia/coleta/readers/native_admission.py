"""Reviewed native corpus admission is a code-reviewed release decision."""

# Set only together with the exact administrator-reviewed real fixture bundle.
# A deployment flag alone cannot attest human review or activate this candidate.
REVIEWED_BUNDLE_SHA256: str | None = None


def admitted() -> bool:
    return REVIEWED_BUNDLE_SHA256 is not None
