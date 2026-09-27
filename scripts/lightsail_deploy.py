"""Deploy one reviewed image digest to the single-host Phase 1 stack."""

from __future__ import annotations

import argparse
import re
import subprocess
import time
import urllib.request
from pathlib import Path

IMAGE = re.compile(r"^ghcr\.io/pradyumna-001/bancaemdia-api@sha256:[0-9a-f]{64}$")
SERVICES = ("api", "extraction", "materialization", "painel_refresh")


def _value(lines: list[str], key: str) -> str:
    values = [line.partition("=")[2].strip() for line in lines if line.startswith(f"{key}=")]
    if len(values) != 1 or not values[0]:
        raise ValueError(f"{key} must appear exactly once in compose.env")
    return values[0]


def _replace_image(path: Path, image: str) -> None:
    lines = path.read_text(encoding="utf-8").splitlines()
    _value(lines, "APP_IMAGE")
    rendered = "\n".join(f"APP_IMAGE={image}" if x.startswith("APP_IMAGE=") else x for x in lines)
    temporary = path.with_suffix(".env.next")
    try:
        temporary.write_text(rendered + "\n", encoding="utf-8")
        Path(temporary).chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _compose(directory: Path, *arguments: str) -> None:
    subprocess.run(
        ["docker", "compose", "--env-file", "compose.env", "-f", "compose.yml", *arguments],
        cwd=directory,
        check=True,
    )


def _ready(domain: str, *, attempts: int = 12, delay: float = 5) -> bool:
    url = f"https://{domain}/ready"
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=5) as response:
                if response.status == 200:
                    return True
        except (OSError, ValueError):
            pass
        if attempt + 1 < attempts:
            time.sleep(delay)
    return False


def deploy(directory: Path, image: str) -> None:
    if not IMAGE.fullmatch(image):
        raise ValueError("use the reviewed GHCR image pinned by a sha256 digest")
    env_path = directory / "compose.env"
    lines = env_path.read_text(encoding="utf-8").splitlines()
    previous = _value(lines, "APP_IMAGE")
    domain = _value(lines, "SITE_DOMAIN")
    if not IMAGE.fullmatch(previous):
        raise ValueError("the installed image must be digest-pinned for automatic rollback")
    if image == previous:
        raise ValueError("the requested image is already installed")
    _replace_image(env_path, image)
    started = False
    try:
        _compose(directory, "pull", *SERVICES)
        started = True
        _compose(directory, "up", "-d", "--no-deps", *SERVICES)
        if not _ready(domain):
            raise RuntimeError("the new release did not pass /ready")
    except Exception as error:
        _replace_image(env_path, previous)
        if started:
            _compose(directory, "up", "-d", "--no-deps", *SERVICES)
            if not _ready(domain):
                raise RuntimeError(
                    "new release failed and the previous release is not ready"
                ) from error
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="ghcr.io/...@sha256:<64 lowercase hexadecimal digits>")
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy" / "lightsail",
    )
    args = parser.parse_args()
    deploy(args.directory.resolve(), args.image)


if __name__ == "__main__":
    main()
