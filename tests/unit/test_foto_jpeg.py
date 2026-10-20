"""Decode success alone does not prove a complete original JPEG stream."""

import hashlib
import io

import pytest
from PIL import Image

from bancaemdia.domain import foto_aposta
from bancaemdia.domain.foto_aposta import InvalidPhotoError, validate_photo


def test_decodable_jpeg_without_closing_marker_is_rejected() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 5), (17, 83, 126)).save(buffer, format="JPEG")
    data = buffer.getvalue()[:-2]
    with Image.open(io.BytesIO(data)) as image:
        image.load()  # This fixture specifically reproduces the permissive decoder.
    with pytest.raises(InvalidPhotoError):
        validate_photo(data, "image/jpeg", hashlib.sha256(data).hexdigest())


@pytest.mark.parametrize("complete", [True, False])
def test_photo_verification_closes_all_owned_buffers(
    monkeypatch: pytest.MonkeyPatch, complete: bool
) -> None:
    with io.BytesIO() as buffer:
        Image.new("RGB", (8, 5), (17, 83, 126)).save(buffer, format="JPEG")
        data = buffer.getvalue()
    if not complete:
        data = data[:-2]
    streams: list[io.BytesIO] = []

    def opened(content: bytes) -> io.BytesIO:
        stream = io.BytesIO(content)
        streams.append(stream)
        return stream

    monkeypatch.setattr(foto_aposta, "BytesIO", opened)
    if complete:
        assert validate_photo(data, "image/jpeg", hashlib.sha256(data).hexdigest()) == "image/jpeg"
    else:
        with pytest.raises(InvalidPhotoError):
            validate_photo(data, "image/jpeg", hashlib.sha256(data).hexdigest())
    assert len(streams) == (2 if complete else 1)
    assert all(stream.closed for stream in streams)
