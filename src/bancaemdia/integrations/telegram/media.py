"""Bounded Telegram photo download and byte-based image validation."""

from dataclasses import dataclass
from typing import Any

from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient

MAX_PHOTO_BYTES = 20 * 1024 * 1024


@dataclass(frozen=True)
class PhotoVariant:
    file_id: str
    file_unique_id: str | None
    stated_size: int | None


def largest_variant(photos: list[object]) -> PhotoVariant | None:
    candidates: list[dict[str, Any]] = []
    for photo in photos:
        if isinstance(photo, dict) and isinstance(photo.get("file_id"), str):
            candidates.append(photo)
    if not candidates:
        return None
    selected = max(
        enumerate(candidates),
        key=lambda pair: (
            pair[1].get("file_size") if type(pair[1].get("file_size")) is int else -1,
            (pair[1].get("width", 0) or 0) * (pair[1].get("height", 0) or 0),
            pair[0],
        ),
    )[1]
    return PhotoVariant(
        selected["file_id"],
        selected.get("file_unique_id") if isinstance(selected.get("file_unique_id"), str) else None,
        selected.get("file_size") if type(selected.get("file_size")) is int else None,
    )


def image_mime(content: bytes) -> str:
    if content.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp"
    raise TelegramApiError("unsupported_image", retryable=False)


async def download_photo(client: TelegramClient, file_id: str) -> tuple[bytes, str]:
    content = await client.download_file(file_id, max_bytes=MAX_PHOTO_BYTES)
    if not content:
        raise TelegramApiError("empty_image", retryable=False)
    return content, image_mime(content)
