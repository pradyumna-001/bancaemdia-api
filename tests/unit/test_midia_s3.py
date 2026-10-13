import hashlib
import io
import struct
import zlib
from unittest.mock import Mock, patch

import pytest
from PIL import Image

from bancaemdia.domain.foto_aposta import MAX_PHOTO_PIXELS, InvalidPhotoError, validate_photo
from bancaemdia.integrations.telegram.media import MAX_PHOTO_BYTES
from bancaemdia.storage import midia_s3


def test_key_rejects_non_sha256_and_signed_url_expires_after_five_minutes() -> None:
    with pytest.raises(ValueError):
        midia_s3.key_for_hash("../other-tenant")
    client = Mock()
    with patch.object(midia_s3, "_client", return_value=client):
        midia_s3.signed_url("private", "media/" + "a" * 64, "image/png")
    assert client.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300
    assert client.generate_presigned_url.call_args.kwargs["Params"]["Bucket"] == "private"


@pytest.mark.parametrize(
    "length,actual_length", [(MAX_PHOTO_BYTES + 1, 0), (1, MAX_PHOTO_BYTES + 1)]
)
def test_photo_sdk_bounded_read_always_closes_body(length: int, actual_length: int) -> None:
    body = Mock()
    body.read.return_value = b"x" * actual_length
    client = Mock()
    client.get_object.return_value = {"ContentLength": length, "Body": body}
    with (
        patch.object(midia_s3, "_client", return_value=client),
        pytest.raises(midia_s3.PhotoObjectInvalidError),
    ):
        midia_s3._download("synthetic-private", "media/" + "a" * 64)
    body.close.assert_called_once()
    if length <= MAX_PHOTO_BYTES:
        body.read.assert_called_once_with(MAX_PHOTO_BYTES + 1)
    else:
        body.read.assert_not_called()


def test_real_png_dimensions_above_pixel_budget_are_rejected() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (1, 1)).save(buffer, format="PNG")
    content = bytearray(buffer.getvalue())
    content[16:24] = struct.pack(">II", MAX_PHOTO_PIXELS + 1, 1)
    content[29:33] = struct.pack(">I", zlib.crc32(content[12:29]))
    data = bytes(content)
    with pytest.raises(InvalidPhotoError):
        validate_photo(data, "image/png", hashlib.sha256(data).hexdigest())


def test_valid_animated_gif_stays_supported_with_bounded_decoding() -> None:
    buffer = io.BytesIO()
    first = Image.new("RGB", (8, 5), "red")
    first.save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=[Image.new("RGB", (8, 5), "blue")],
        disposal=2,
    )
    data = buffer.getvalue()
    assert validate_photo(data, "image/gif", hashlib.sha256(data).hexdigest()) == "image/gif"
