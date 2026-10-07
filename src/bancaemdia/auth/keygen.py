"""Generate server-only identity key files without overwriting existing material."""

import argparse
import base64
import json
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def generate(directory: Path) -> None:
    os.umask(0o077)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    signing = directory / "signing.json"
    encryption = directory / "encryption.json"
    if signing.exists() or encryption.exists():
        raise FileExistsError(
            "Identity key files already exist; use the documented rotation procedure"
        )
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    private = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode()
    for path, value in (
        (signing, {"active": "rsa-" + secrets.token_hex(8), "keys": private}),
        (
            encryption,
            {
                "active": "aes-" + secrets.token_hex(8),
                "keys": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
            },
        ),
    ):
        with path.open("x", encoding="utf-8") as stream:
            json.dump({"active": value["active"], "keys": {value["active"]: value["keys"]}}, stream)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    generate(parser.parse_args().directory)


if __name__ == "__main__":
    main()
