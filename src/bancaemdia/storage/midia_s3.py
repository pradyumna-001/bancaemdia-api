"""Private, content-addressed S3 storage for uploaded betting-slip images."""

from __future__ import annotations

import asyncio
from functools import lru_cache
from typing import Any, cast

from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError

from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES


class PhotoObjectMissingError(RuntimeError):
    pass


class PhotoStorageUnavailableError(RuntimeError):
    pass


class PhotoObjectInvalidError(ValueError):
    pass


@lru_cache(maxsize=1)
def _client() -> Any:
    import boto3

    return boto3.client(
        "s3", config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 1})
    )


def _download(bucket: str, key: str) -> bytes:
    try:
        response = _client().get_object(Bucket=bucket, Key=key)
        body = response["Body"]
        try:
            if response["ContentLength"] > MAX_PHOTO_BYTES:
                raise PhotoObjectInvalidError
            content: bytes = body.read(MAX_PHOTO_BYTES + 1)
            if len(content) > MAX_PHOTO_BYTES:
                raise PhotoObjectInvalidError
            return content
        finally:
            body.close()
    except ClientError as error:
        if error.response.get("Error", {}).get("Code") in {"NoSuchKey", "404"}:
            raise PhotoObjectMissingError from None
        raise PhotoStorageUnavailableError from None
    except (BotoCoreError, OSError):
        raise PhotoStorageUnavailableError from None


async def download(bucket: str, key: str) -> bytes:
    async with asyncio.timeout(10):
        return await asyncio.to_thread(_download, bucket, key)


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
