"""Run local chaos tests or an explicitly targeted staging RDS failover.

Local: python scripts/chaos_inject.py local --verify-all
Staging dry run: python scripts/chaos_inject.py staging-failover --rds-instance NAME
    --aws-profile PROFILE --region REGION --ready-url https://staging.example/ready
Staging execution additionally requires --execute --confirm-instance NAME.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import urlopen


def _integrity() -> None:
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL is required for the integrity check")
    subprocess.run([sys.executable, "scripts/conferir_numeros.py", "--todos"], check=True)


def _ready(url: str) -> bool:
    try:
        with urlopen(url, timeout=3) as response:
            return response.status == 200 and json.load(response).get("status") == "ready"
    except (HTTPError, URLError, TimeoutError, ValueError):
        return False


def _staging(args: argparse.Namespace) -> None:
    if urlsplit(args.ready_url).scheme != "https":
        raise ValueError("staging readiness URL must use HTTPS")
    command = [
        "aws",
        "--profile",
        args.aws_profile,
        "--region",
        args.region,
        "rds",
        "reboot-db-instance",
        "--db-instance-identifier",
        args.rds_instance,
        "--force-failover",
        "--no-cli-pager",
    ]
    if not args.execute:
        sys.stdout.write("Dry run: " + " ".join(command) + "\n")
        sys.stdout.write("Readiness: " + args.ready_url + "\n")
        return
    if args.confirm_instance != args.rds_instance:
        raise ValueError("--confirm-instance must exactly match --rds-instance")
    if os.environ.get("APP_ENV") != "staging":
        raise RuntimeError("APP_ENV must be staging")
    if not os.environ.get("DATABASE_URL"):
        raise RuntimeError("DATABASE_URL must point to staging for integrity checks")
    if not _ready(args.ready_url):
        raise RuntimeError("staging readiness is not healthy before failover")
    _integrity()
    # RDS Multi-AZ instances support reboot with forced failover; a single-AZ instance does not.
    description = subprocess.run(
        [
            "aws",
            "--profile",
            args.aws_profile,
            "--region",
            args.region,
            "rds",
            "describe-db-instances",
            "--db-instance-identifier",
            args.rds_instance,
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    instances = json.loads(description.stdout)["DBInstances"]
    if len(instances) != 1 or not instances[0]["MultiAZ"]:
        raise RuntimeError("target must be exactly one Multi-AZ RDS instance")
    if instances[0]["DBInstanceStatus"] != "available":
        raise RuntimeError("RDS instance must be available before failover")
    tags = subprocess.run(
        [
            "aws",
            "--profile",
            args.aws_profile,
            "--region",
            args.region,
            "rds",
            "list-tags-for-resource",
            "--resource-name",
            instances[0]["DBInstanceArn"],
            "--output",
            "json",
            "--no-cli-pager",
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    environment_tag = {
        item["Key"].lower(): item["Value"].lower() for item in json.loads(tags.stdout)["TagList"]
    }
    if environment_tag.get("environment") != "staging":
        raise RuntimeError("RDS instance must carry Environment=staging")
    started = time.monotonic()
    subprocess.run(command, check=True, capture_output=True, text=True)
    observed_unready = False
    deadline = started + args.timeout_seconds
    while time.monotonic() < deadline:
        ready = _ready(args.ready_url)
        observed_unready |= not ready
        if observed_unready and ready:
            break
        time.sleep(5)
    else:
        raise TimeoutError("staging readiness did not fail and recover within the timeout")
    recovery_seconds = round(time.monotonic() - started, 1)
    _integrity()
    sys.stdout.write(f"Readiness recovered in {recovery_seconds}s; integrity check passed\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="scenario", required=True)
    local = subparsers.add_parser("local", help="run reproducible chaos tests")
    local.add_argument("--verify-all", action="store_true")
    staging = subparsers.add_parser("staging-failover", help="RDS Multi-AZ failover in staging")
    staging.add_argument("--rds-instance", required=True)
    staging.add_argument("--aws-profile", required=True)
    staging.add_argument("--region", required=True)
    staging.add_argument("--ready-url", required=True)
    staging.add_argument("--execute", action="store_true")
    staging.add_argument("--confirm-instance")
    staging.add_argument("--timeout-seconds", type=int, default=900)
    args = parser.parse_args()
    if args.scenario == "local":
        subprocess.run([sys.executable, "-m", "pytest", "tests/chaos", "-n", "0"], check=True)
        if args.verify_all:
            _integrity()
    else:
        _staging(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
