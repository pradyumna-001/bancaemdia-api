"""Reproduce the complete pinned Week 7 assembly outside the product checkout."""

import hashlib
import io
import json
import subprocess
import sys
import tarfile
from pathlib import Path

CONSOLIDATION = "db7f63eccd03635bac20235270bffa30351e41a9"
RECONCILIATION = "0dfd5a6cfe70a94bb813d47dac50b2f64ed3b073"
COLLECTION = "9210cef7e1b64664ac8ad347afa69ad02c056cef"
ACCOUNTS = "427e6af8d6664bd2076bfe6518be754b5934d77b"
CLI_FILES = (
    "src/bancaemdia/cli/reconciliar_casa_telegram.py",
    "src/bancaemdia/cli/reconciliacao_report.py",
    "src/bancaemdia/cli/reconciliacao_journal.py",
    "alembic/versions/r111journal2026_reconciliacao_chunks.py",
    "tests/cli/test_reconciliar_casa_telegram.py",
    "tests/cli/reconciliation_acceptance.py",
)


def git(root: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def prepare(target: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    bootstrap = target.with_name(target.name + "-pr166")
    if target.exists() or bootstrap.exists():
        raise RuntimeError("Both disposable destinations must be new")
    for sha in (CONSOLIDATION, RECONCILIATION, COLLECTION, ACCOUNTS):
        git(root, "cat-file", "-e", sha)
    git(root, "clone", "--no-checkout", "--shared", "--no-hardlinks", str(root), str(bootstrap))
    git(bootstrap, "config", "core.autocrlf", "false")
    git(bootstrap, "checkout", "--detach", CONSOLIDATION)
    # Read the reviewed patch bytes directly: Windows checkout line endings are not patch data.
    (bootstrap / "scripts/convergence/resolution.patch").write_bytes(
        git(root, "show", CONSOLIDATION + ":scripts/convergence/resolution.patch")
    )
    subprocess.run(
        [sys.executable, str(bootstrap / "scripts/prepare_convergence.py"), str(target)],
        check=True,
    )
    archive = git(root, "archive", RECONCILIATION, *CLI_FILES)
    with tarfile.open(fileobj=io.BytesIO(archive)) as bundle:
        bundle.extractall(target, filter="data")
    (target / "alembic/versions/r112integration_merge.py").write_text(
        '"""Disposable complete Week 7 migration convergence."""\n'
        'revision = "r112integration"\n'
        'down_revision = ("c110fact2026", "r111journal2026")\n'
        "branch_labels = None\ndepends_on = None\n"
        "def upgrade() -> None:\n    pass\n"
        "def downgrade() -> None:\n    pass\n",
        encoding="utf-8",
    )
    for relative in ("tests/integrity", "tests/fixtures/coleta_v2"):
        import shutil

        shutil.copytree(root / relative, target / relative)
    # Published tests retain every migration assertion; only the expected assembly head changes.
    for filename in ("test_pairing.py", "test_contract_v2.py"):
        path = target / "tests/integration/coleta" / filename
        path.write_text(
            path.read_text(encoding="utf-8").replace('== "c110fact2026"', '== "r112integration"'),
            encoding="utf-8",
        )
    fixes = root / "scripts/integrity/fixes.patch"
    git(target, "apply", "--whitespace=error", str(fixes))
    manifest = json.loads((target / "convergence-heads.json").read_text(encoding="utf-8"))
    manifest.update({
        "integrity": git(root, "rev-parse", "HEAD").decode().strip(),
        "reconciliation": RECONCILIATION,
        "migration_head": "r112integration",
        "fixes_sha256": hashlib.sha256(fixes.read_bytes()).hexdigest(),
        "scope": "disposable full Week 7 integration; product PR remains based directly on main",
    })
    (target / "integrity-heads.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve())
