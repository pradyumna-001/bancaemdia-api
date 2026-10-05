"""Build a disposable composition, never change either product branch or revision ID."""

import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PREREQUISITE = "db7f63eccd03635bac20235270bffa30351e41a9"
OVERLAY = (
    "src/bancaemdia/cli/reconciliar_casa_telegram.py",
    "src/bancaemdia/cli/reconciliacao_report.py",
    "src/bancaemdia/cli/reconciliacao_journal.py",
    "alembic/versions/r111journal2026_reconciliacao_chunks.py",
    "scripts/reconciliar_casa_telegram.py",
    "tests/cli/test_reconciliar_casa_telegram.py",
    "tests/cli/reconciliation_acceptance.py",
    "docs/runbooks/reconciliacao-historica.md",
)


def prepare(target: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    if target.exists():
        raise RuntimeError(
            "Composition destination must be new; no existing checkout is overwritten"
        )
    target.mkdir(parents=True)
    archive = subprocess.run(
        ["git", "archive", PREREQUISITE],
        cwd=root,
        check=True,
        capture_output=True,
    ).stdout
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(target, filter="data")
    for relative in OVERLAY:
        destination = target / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(root / relative, destination)
    # Both published migrations remain unchanged. This explicit merge only exists in the
    # integration assembly until the administrator integrates their independent main-based PRs.
    (target / "alembic/versions/r111integration_merge.py").write_text(
        '"""Disposable #110/#111 migration convergence."""\n'
        'revision = "r111integration"\n'
        'down_revision = ("c110fact2026", "r111journal2026")\n'
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()
    (target / "reconciliation-heads.json").write_text(
        json.dumps(
            {
                "prerequisite_pr": 166,
                "prerequisite_sha": PREREQUISITE,
                "cli_sha": head,
                "migration_merge": "r111integration",
                "overlay": OVERLAY,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve())
