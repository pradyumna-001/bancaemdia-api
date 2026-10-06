"""Operational entry point for stored-media re-reading."""

from bancaemdia.cli.reprocess import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(["reler-todas", *sys.argv[1:]]))
