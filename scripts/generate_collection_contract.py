"""Generate/check the canonical N/N-1 collection artifact and release metadata."""

import argparse
from pathlib import Path

from generate_openapi import _configure_generation_environment

from bancaemdia.api.collection_contract import artifact, release, serialized

ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    _configure_generation_environment()
    for name, value in (
        ("extension-collection-v2.json", artifact()),
        ("extension-collection-release.json", release().model_dump()),
    ):
        path = ROOT / "openapi" / name
        expected = serialized(value)
        if args.check:
            if not path.exists() or path.read_bytes() != expected:
                raise SystemExit(f"stale collection artifact: {path}")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(expected)


if __name__ == "__main__":
    main()
