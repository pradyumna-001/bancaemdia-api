"""Validate existing photo evidence without extraction or changing its original bytes."""

import hashlib
import warnings
from dataclasses import dataclass
from io import BytesIO

from PIL import Image, UnidentifiedImageError

from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES

IMAGE_TYPES = {"JPEG": "image/jpeg", "PNG": "image/png", "GIF": "image/gif", "WEBP": "image/webp"}
MAX_PHOTO_PIXELS = 40_000_000
MAX_PHOTO_FRAMES = 256


def photo_path(path: str) -> bool:
    return path.startswith("/api/v1/apostas/") and path.endswith("/foto")


class InvalidPhotoError(ValueError):
    pass


@dataclass(frozen=True)
class StoredPhoto:
    hash: str
    mime: str
    content: bytes | None
    s3_key: str | None = None
    encrypted: bool = False
    size: int = 0


def validate_photo(content: bytes, mime: str, digest: str) -> str:
    if (
        not content
        or len(content) > MAX_PHOTO_BYTES
        or hashlib.sha256(content).hexdigest() != digest
    ):
        raise InvalidPhotoError
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(content)) as image:
                actual = IMAGE_TYPES.get(image.format or "")
                if (
                    actual is None
                    or mime != actual
                    or image.width * image.height > MAX_PHOTO_PIXELS
                ):
                    raise InvalidPhotoError
                if image.format == "JPEG" and not content.endswith(b"\xff\xd9"):
                    # Some complete entropy streams decode despite a missing EOI.
                    # Require the original JPEG's closing marker as well.
                    raise InvalidPhotoError
                image.verify()
            # JPEG's verify() does not decode its stream. Actually load the
            # bounded frames too, so truncated data cannot be published as valid.
            with Image.open(BytesIO(content)) as decoded:
                pixels = 0
                for frame in range(MAX_PHOTO_FRAMES + 1):
                    try:
                        decoded.seek(frame)
                    except EOFError:
                        break
                    pixels += decoded.width * decoded.height
                    if frame == MAX_PHOTO_FRAMES or pixels > MAX_PHOTO_PIXELS:
                        raise InvalidPhotoError
                    decoded.load()
    except (
        OSError,
        ValueError,
        SyntaxError,
        EOFError,
        UnidentifiedImageError,
        Image.DecompressionBombError,
        Image.DecompressionBombWarning,
    ):
        raise InvalidPhotoError from None
    return actual
