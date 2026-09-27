"""Restore a private backup into an isolated local container and verify the schema."""

from __future__ import annotations

import argparse
import hashlib
import re
import secrets
import subprocess
import tempfile
import time
from pathlib import Path

BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,52}[a-z0-9]$")
BACKUP = re.compile(r"^bancaemdia-[0-9]{8}T[0-9]{6}Z\.dump$")


def _run(*command: str, capture: bool = False) -> str:
    result = subprocess.run(command, check=True, capture_output=capture, text=capture)
    return result.stdout.strip() if capture else ""


def restore_drill(bucket: str, name: str, postgres_image: str) -> int:
    if not BUCKET.fullmatch(bucket) or not BACKUP.fullmatch(name):
        raise ValueError("use the exact private bucket and backup filename")
    container = f"bancaemdia-restore-{secrets.token_hex(4)}"
    with tempfile.TemporaryDirectory(prefix="bancaemdia-restore-") as temporary:
        dump = Path(temporary) / name
        checksum = Path(temporary) / f"{name}.sha256"
        source = f"s3://{bucket}/backups/{name}"
        _run("aws", "s3", "cp", source, str(dump), "--only-show-errors")
        _run("aws", "s3", "cp", f"{source}.sha256", str(checksum), "--only-show-errors")
        expected = checksum.read_text(encoding="ascii").split()[0]
        hasher = hashlib.sha256()
        with dump.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                hasher.update(chunk)
        if hasher.hexdigest() != expected:
            raise RuntimeError("backup checksum mismatch")
        _run("pg_restore", "--list", str(dump))
        started = False
        try:
            _run(
                "docker",
                "run",
                "-d",
                "--rm",
                "--network",
                "none",
                "--name",
                container,
                "-e",
                f"POSTGRES_PASSWORD={secrets.token_urlsafe(24)}",
                "-e",
                "POSTGRES_DB=bancaemdia",
                postgres_image,
            )
            started = True
            for _ in range(30):
                result = subprocess.run(
                    [
                        "docker",
                        "exec",
                        container,
                        "pg_isready",
                        "-U",
                        "postgres",
                        "-d",
                        "bancaemdia",
                    ],
                    capture_output=True,
                )
                if result.returncode == 0:
                    break
                time.sleep(1)
            else:
                raise RuntimeError("isolated PostgreSQL did not become ready")
            _run("docker", "cp", str(dump), f"{container}:/tmp/restore.dump")
            _run(
                "docker",
                "exec",
                "-u",
                "postgres",
                container,
                "pg_restore",
                "--exit-on-error",
                "--no-owner",
                "--no-acl",
                "-U",
                "postgres",
                "-d",
                "bancaemdia",
                "/tmp/restore.dump",
            )
            count = _run(
                "docker",
                "exec",
                "-u",
                "postgres",
                container,
                "psql",
                "-U",
                "postgres",
                "-d",
                "bancaemdia",
                "-At",
                "-c",
                "SELECT count(*) FROM usuarios",
                capture=True,
            )
            return int(count)
        finally:
            if started:
                subprocess.run(
                    ["docker", "stop", container],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument("--backup", required=True, help="bancaemdia-YYYYMMDDTHHMMSSZ.dump")
    parser.add_argument("--postgres-image", required=True, help="same reviewed PostgreSQL 16 image")
    args = parser.parse_args()
    count = restore_drill(args.bucket, args.backup, args.postgres_image)
    print(f"isolated restore passed; usuarios={count}")  # ruff: ignore[print]


if __name__ == "__main__":
    main()
