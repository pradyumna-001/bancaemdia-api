import re

COLLECTION_SECRET = re.compile(r"(?:cti_|cpc_)[A-Za-z0-9_-]+")


def redact_collection_secrets(value: str) -> str:
    return COLLECTION_SECRET.sub("[REDACTED]", value)
