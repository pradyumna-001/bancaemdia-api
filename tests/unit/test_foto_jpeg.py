"""Decode success alone does not prove a complete original JPEG stream."""

import hashlib
import io

import pytest
from PIL import Image

from bancaemdia.domain.foto_aposta import InvalidPhotoError, validate_photo


def test_decodable_jpeg_without_closing_marker_is_rejected() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 5), (17, 83, 126)).save(buffer, format="JPEG")
    data = buffer.getvalue()[:-2]
    with Image.open(io.BytesIO(data)) as image:
        image.load()  # This fixture specifically reproduces the permissive decoder.
    with pytest.raises(InvalidPhotoError):
        validate_photo(data, "image/jpeg", hashlib.sha256(data).hexdigest())
