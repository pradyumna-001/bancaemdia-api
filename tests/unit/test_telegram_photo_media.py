"""Photo variant, bounded download and extraction mapping contracts."""

import httpx
import pytest

from bancaemdia.integrations.telegram.client import TelegramApiError, TelegramClient
from bancaemdia.integrations.telegram.media import download_photo, largest_variant
from bancaemdia.services.telegram_photo_intake import candidates_from_reading


def test_variant_uses_largest_bytes_then_dimensions() -> None:
    chosen = largest_variant([
        {"file_id": "last", "file_size": 300, "width": 100, "height": 100},
        {
            "file_id": "largest",
            "file_unique_id": "stable",
            "file_size": 900,
            "width": 10,
            "height": 10,
        },
        {"file_id": "thumbnail", "file_size": 100},
    ])
    assert chosen is not None
    assert chosen.file_id == "largest" and chosen.file_unique_id == "stable"


@pytest.mark.asyncio
async def test_download_uses_getfile_and_rejects_non_image_or_oversize() -> None:
    calls: list[str] = []

    def respond(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "photos/slip.jpg"}}
            )
        return httpx.Response(200, content=b"\xff\xd8\xff\xe0bet")

    client = TelegramClient("test-token", transport=httpx.MockTransport(respond))
    try:
        image, mime = await download_photo(client, "file-id")
        assert image.startswith(b"\xff\xd8\xff") and mime == "image/jpeg"
        assert calls == ["/bottest-token/getFile", "/file/bottest-token/photos/slip.jpg"]
        with pytest.raises(TelegramApiError, match="file_too_large"):
            await client.download_file("file-id", max_bytes=3)
    finally:
        await client.aclose()


def test_coupons_remain_separate_and_unreadable_has_no_fields() -> None:
    reading = {
        "cupons": [
            {"bilhete": {"casa": "Betano", "odd_total": 1.8, "confianca": 0.94}},
            {"bilhete": {"casa": "Bet365", "odd_total": 2.1, "confianca": 0.92}},
        ]
    }
    candidates = candidates_from_reading(reading)
    assert len(candidates) == 2
    assert candidates[0]["fields"]["odd"] == pytest.approx(1.8)
    assert candidates[1]["fields"]["odd"] == pytest.approx(2.1)
    assert candidates[0]["confidence"]["casa"] == pytest.approx(0.94)
    assert candidates_from_reading({"bilhete": {"ilegivel": True}}) == []
