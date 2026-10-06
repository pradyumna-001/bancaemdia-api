"""Run the shipped recovery and edge configuration, without AWS or production data."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from scripts import lightsail_backup, lightsail_restore_drill

ROOT = Path(__file__).resolve().parents[2]
pytestmark = [pytest.mark.integration, pytest.mark.xdist_group("phase1")]


def pull_image(image):
    # Registry/network failures are transient; every test still runs the real image.
    for attempt in range(3):
        result = subprocess.run(["docker", "pull", image], capture_output=True, text=True)
        if result.returncode == 0:
            return
        if attempt < 2:
            time.sleep(3 * (attempt + 1))
    pytest.fail(f"Could not obtain required operational image: {result.stderr}")


@pytest.fixture
def docker_available():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        if os.environ.get("PHASE1_ACCEPTANCE_REQUIRED") == "1":
            pytest.fail("Operational acceptance requires real Docker services")
        pytest.skip("Operational acceptance requires disposable Docker services")


def test_real_dump_roundtrip_through_private_object_transport(
    tmp_path, monkeypatch, docker_available
):
    source = "phase1-backup-" + uuid4().hex
    run = subprocess.run
    objects = tmp_path / "objects"
    objects.mkdir()
    restored = []
    pull_image("postgres:16")
    run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--network",
            "none",
            "--name",
            source,
            "-e",
            "POSTGRES_PASSWORD=synthetic-disposable",
            "postgres:16",
        ],
        check=True,
        capture_output=True,
    )

    def transport(command, **kwargs):
        args = list(command)
        if args[:3] == ["aws", "s3", "cp"]:
            remote = args[2 + 1] if args[3].startswith("s3://") else args[4]
            local = args[4] if args[3].startswith("s3://") else args[3]
            obj = objects / remote.rsplit("/", 1)[1]
            shutil.copyfile(
                obj if args[3].startswith("s3://") else local,
                local if args[3].startswith("s3://") else obj,
            )
            return subprocess.CompletedProcess(args, 0, stdout="")
        if args[:2] == ["aws", "s3api"]:
            obj = objects / args[args.index("--key") + 1].rsplit("/", 1)[1]
            return subprocess.CompletedProcess(args, 0, stdout=str(obj.stat().st_size))
        if args[:2] == ["docker", "compose"]:
            args = [
                "docker",
                "exec",
                source,
                "pg_dump",
                "-U",
                "postgres",
                "-d",
                "postgres",
                "--format=custom",
                "--no-owner",
            ]
        elif args[:2] == ["pg_restore", "--list"]:
            args = [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "-v",
                f"{Path(args[2]).resolve()}:/dump:ro",
                "postgres:16",
                "pg_restore",
                "--list",
                "/dump",
            ]
        elif args[:2] == ["docker", "stop"] and args[2] != source:
            result = run(
                [
                    "docker",
                    "exec",
                    args[2],
                    "psql",
                    "-U",
                    "postgres",
                    "-d",
                    "bancaemdia",
                    "-At",
                    "-c",
                    "SELECT string_agg(email, ',' ORDER BY id) FROM usuarios",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            restored.append(result.stdout.strip())
        return run(args, **kwargs)

    try:
        for _ in range(30):
            if (
                run(
                    ["docker", "exec", source, "pg_isready", "-h", "127.0.0.1", "-U", "postgres"],
                    capture_output=True,
                ).returncode
                == 0
            ):
                break
            time.sleep(1)
        else:
            pytest.fail("Disposable source PostgreSQL did not become ready")
        run(
            [
                "docker",
                "exec",
                source,
                "psql",
                "-U",
                "postgres",
                "-c",
                "CREATE TABLE usuarios(id bigint PRIMARY KEY, email text NOT NULL UNIQUE);"
                "INSERT INTO usuarios VALUES (1,'first@example.invalid'),(2,'second@example.invalid');",
            ],
            check=True,
            capture_output=True,
        )
        monkeypatch.setattr(subprocess, "run", transport)
        uri = lightsail_backup.backup(tmp_path, "phase1-synthetic-bucket")
        assert not list((tmp_path / "backups-local").iterdir())
        assert (
            lightsail_restore_drill.restore_drill(
                "phase1-synthetic-bucket", uri.rsplit("/", 1)[1], "postgres:16"
            )
            == 2
        )
        assert restored == ["first@example.invalid,second@example.invalid"]
    finally:
        run(["docker", "stop", source], check=False, capture_output=True)


def test_actual_caddy_filters_sensitive_logs_and_hides_metrics(tmp_path, docker_available):
    name = "phase1-edge-" + uuid4().hex
    config = (ROOT / "deploy/lightsail/Caddyfile").read_text()
    # Only replace the upstream/site, preserving the actual logging and metrics directives.
    config = config.replace("{$SITE_DOMAIN}", ":8080").replace(
        "reverse_proxy api:8000", "respond 200"
    )
    path = tmp_path / "Caddyfile"
    path.write_text(config)
    run = subprocess.run
    pull_image("caddy:2.10")
    try:
        started = run(
            [
                "docker",
                "run",
                "-d",
                "--rm",
                "--name",
                name,
                "-p",
                "127.0.0.1::8080",
                "-v",
                f"{path.resolve()}:/etc/caddy/Caddyfile:ro",
                "caddy:2.10",
            ],
            capture_output=True,
            text=True,
        )
        assert started.returncode == 0, started.stderr
        port = (
            run(["docker", "port", name, "8080"], check=True, capture_output=True, text=True)
            .stdout.strip()
            .split(":")[-1]
        )
        base = f"http://127.0.0.1:{port}"
        for _ in range(30):
            try:
                if httpx.get(base + "/health").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(1)
        else:
            pytest.fail("Actual Caddy configuration did not start")
        assert httpx.get(base + "/metrics").status_code == 404
        assert httpx.get(base + "/metrics/private").status_code == 404
        sentinel = "PHASE1_PRIVATE_SENTINEL"
        assert (
            httpx.get(
                base + f"/ready?token={sentinel}",
                headers={
                    "Authorization": sentinel,
                    "Cookie": sentinel,
                    "X-Coleta-Token": sentinel,
                    "X-Telegram-Bot-Api-Secret-Token": sentinel,
                },
            ).status_code
            == 200
        )
        logs = run(["docker", "logs", name], capture_output=True, text=True, check=True)
        assert sentinel not in logs.stdout + logs.stderr
        records = [
            json.loads(line)
            for line in (logs.stdout + logs.stderr).splitlines()
            if line.startswith("{")
        ]
        access = [r for r in records if r.get("logger", "").startswith("http.log.access")]
        assert any(r.get("status") == 404 for r in access)
        assert any(r.get("status") == 200 for r in access)
        assert all("headers" not in r["request"] and "uri" not in r["request"] for r in access)
    finally:
        run(["docker", "stop", name], check=False, capture_output=True)
