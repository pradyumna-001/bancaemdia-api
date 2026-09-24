from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import lightsail_backup, lightsail_deploy, lightsail_restore_drill

OLD = "ghcr.io/pradyumna-001/bancaemdia-api@sha256:" + "a" * 64
NEW = "ghcr.io/pradyumna-001/bancaemdia-api@sha256:" + "b" * 64


def test_deploy_restores_previous_digest_when_new_release_is_unhealthy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env = tmp_path / "compose.env"
    env.write_text(f"APP_IMAGE={OLD}\nSITE_DOMAIN=api.example.com\n", encoding="utf-8")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(lightsail_deploy, "_compose", lambda _directory, *args: calls.append(args))
    health = iter([False, True])
    monkeypatch.setattr(lightsail_deploy, "_ready", lambda _domain: next(health))

    with pytest.raises(RuntimeError, match="did not pass /ready"):
        lightsail_deploy.deploy(tmp_path, NEW)

    assert f"APP_IMAGE={OLD}" in env.read_text(encoding="utf-8")
    assert [call[0] for call in calls] == ["pull", "up", "up"]


def test_backup_keeps_local_dump_when_remote_size_is_wrong(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fake_run(command, **kwargs):
        if command[0:2] == ["docker", "compose"]:
            kwargs["stdout"].write(b"valid-pg-dump")
        if command[0:2] == ["aws", "s3api"]:
            return SimpleNamespace(stdout="1\n")
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(lightsail_backup.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="remote backup size"):
        lightsail_backup.backup(tmp_path, "private-banca-bucket")

    assert len(list((tmp_path / "backups-local").glob("*.dump"))) == 1
    assert len(list((tmp_path / "backups-local").glob("*.sha256"))) == 1


def test_restore_drill_rejects_corrupt_backup_before_starting_postgres(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run(*command, capture=False):
        calls.append(command)
        if command[:3] == ("aws", "s3", "cp"):
            target = Path(command[4])
            target.write_bytes(b"corrupt" if target.suffix == ".dump" else b"0" * 64 + b"  file\n")
        return ""

    monkeypatch.setattr(lightsail_restore_drill, "_run", fake_run)
    with pytest.raises(RuntimeError, match="checksum mismatch"):
        lightsail_restore_drill.restore_drill(
            "private-banca-bucket", "bancaemdia-20260924T120000Z.dump", "postgres:16"
        )

    assert all(command[0] != "docker" for command in calls)
