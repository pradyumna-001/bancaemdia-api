"""Private, content-addressed S3 storage for uploaded betting-slip images."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, cast


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3

    return boto3.client("s3")


def key_for_hash(hash_: str) -> str:
    if len(hash_) != 64 or any(character not in "0123456789abcdef" for character in hash_):
        raise ValueError("media hash must be a SHA-256 hex digest")
    return f"media/{hash_}"


async def upload(bucket: str, hash_: str, content: bytes) -> str:
    key = key_for_hash(hash_)
    await asyncio.to_thread(
        _client().put_object,
        Bucket=bucket,
        Key=key,
        Body=content,
        ContentType="application/octet-stream",
    )
    return key


def signed_url(bucket: str, key: str, content_type: str) -> str:
    return cast(
        str,
        _client().generate_presigned_url(
            "get_object",
            Params={
                "Bucket": bucket,
                "Key": key,
                "ResponseContentType": content_type,
                "ResponseContentDisposition": "inline",
            },
            ExpiresIn=300,
        ),
    )
