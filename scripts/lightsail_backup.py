"""Create and verify a PostgreSQL dump, then upload it to private object storage."""

from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

BUCKET = re.compile(r"^[a-z0-9][a-z0-9-]{1,52}[a-z0-9]$")


def backup(directory: Path, bucket: str, *, now: datetime | None = None) -> str:
    if not BUCKET.fullmatch(bucket):
        raise ValueError("invalid private bucket name")
    backup_dir = directory / "backups-local"
    backup_dir.mkdir(mode=0o700, exist_ok=True)
    Path(backup_dir).chmod(0o700)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"bancaemdia-{stamp}.dump"
    dump = backup_dir / name
    checksum = backup_dir / f"{name}.sha256"
    target = f"s3://{bucket}/backups/{name}"
    try:
        with dump.open("xb") as output:
            Path(dump).chmod(0o600)
            subprocess.run(
                [
                    "docker",
                    "compose",
                    "--env-file",
                    "compose.env",
                    "-f",
                    "compose.yml",
                    "exec",
                    "-T",
                    "-u",
                    "postgres",
                    "postgres",
                    "pg_dump",
                    "-U",
                    "postgres",
                    "-d",
                    "bancaemdia",
                    "--format=custom",
                    "--no-owner",
                ],
                cwd=directory,
                stdout=output,
                check=True,
            )
        subprocess.run(["pg_restore", "--list", str(dump)], stdout=subprocess.DEVNULL, check=True)
        hasher = hashlib.sha256()
        with dump.open("rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                hasher.update(chunk)
        digest = hasher.hexdigest()
        checksum.write_text(f"{digest}  {name}\n", encoding="ascii")
        Path(checksum).chmod(0o600)
        subprocess.run(["aws", "s3", "cp", str(dump), target, "--only-show-errors"], check=True)
        subprocess.run(
            ["aws", "s3", "cp", str(checksum), f"{target}.sha256", "--only-show-errors"], check=True
        )
        result = subprocess.run(
            [
                "aws",
                "s3api",
                "head-object",
                "--bucket",
                bucket,
                "--key",
                f"backups/{name}",
                "--query",
                "ContentLength",
                "--output",
                "text",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        if int(result.stdout.strip()) != dump.stat().st_size:
            raise RuntimeError("remote backup size does not match the verified local dump")
    except Exception:
        # Keep the local dump for operator inspection when an upload or check fails.
        raise
    dump.unlink()
    checksum.unlink()
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bucket", required=True)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy" / "lightsail",
    )
    args = parser.parse_args()
    print(backup(args.directory.resolve(), args.bucket))  # ruff: ignore[print]


if __name__ == "__main__":
    main()
