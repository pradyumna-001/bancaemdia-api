"""Count uploads stuck in processing on the Phase 1 PostgreSQL host."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

QUERY = """
SELECT COUNT(*)::bigint,
       COALESCE(SUM(octet_length(raw.conteudo)), 0)::bigint
  FROM public.uploads AS u
  LEFT JOIN public.upload_arquivos AS raw ON raw.upload_id = u.id
 WHERE u.status = 'processing'
   AND u.criado_em < clock_timestamp() - interval '1 hour'
"""


def inspect(directory: Path) -> tuple[int, int]:
    result = subprocess.run(
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
            "psql",
            "-X",
            "-Atq",
            "-U",
            "postgres",
            "-d",
            "bancaemdia",
            "-c",
            QUERY,
        ],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    fields = result.stdout.strip().split("|")
    if len(fields) != 2:
        raise RuntimeError("unexpected upload watchdog query result")
    count, bytes_retained = (int(field) for field in fields)
    if count < 0 or bytes_retained < 0:
        raise RuntimeError("invalid upload watchdog counters")
    return count, bytes_retained


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--directory",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "deploy" / "lightsail",
    )
    args = parser.parse_args()
    count, bytes_retained = inspect(args.directory.resolve())
    print(  # ruff: ignore[print]
        json.dumps({"stuck_uploads": count, "retained_raw_bytes": bytes_retained})
    )
    return int(count > 0)


if __name__ == "__main__":
    raise SystemExit(main())
