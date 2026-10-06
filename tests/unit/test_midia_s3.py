from unittest.mock import Mock, patch

import pytest

from bancaemdia.storage import midia_s3


def test_key_rejects_non_sha256_and_signed_url_expires_after_five_minutes() -> None:
    with pytest.raises(ValueError):
        midia_s3.key_for_hash("../other-tenant")
    client = Mock()
    with patch.object(midia_s3, "_client", return_value=client):
        midia_s3.signed_url("private", "media/" + "a" * 64, "image/png")
    assert client.generate_presigned_url.call_args.kwargs["ExpiresIn"] == 300
    assert client.generate_presigned_url.call_args.kwargs["Params"]["Bucket"] == "private"
