"""Prepare an isolated, reproducible merge rehearsal; never change the product checkout."""

import json
import subprocess
import sys
from pathlib import Path

COLLECTION = "9210cef7e1b64664ac8ad347afa69ad02c056cef"
ACCOUNTS = "427e6af8d6664bd2076bfe6518be754b5934d77b"
HOLDERS = "fdb3a4b2d3be7f7241aa5f0c61f354200e5d6fe3"
PAIRING = "a824c8e85638ebb3823990886e9514aa07f424cc"
SOURCE = Path(__file__).resolve().parents[1]
DEST = Path(sys.argv[1]).resolve()
if DEST.exists():
    raise SystemExit("The rehearsal destination must not exist")


def git(*args, cwd=SOURCE):
    return subprocess.check_output(["git", *args], cwd=cwd, text=True, encoding="utf-8")


head = git("rev-parse", "HEAD").strip()
for sha in (COLLECTION, ACCOUNTS, HOLDERS, PAIRING):
    git("cat-file", "-e", sha)
git("clone", "--no-hardlinks", "--shared", str(SOURCE), str(DEST))
git("checkout", "--detach", head, cwd=DEST)
git("config", "user.name", "Synthetic convergence rehearsal", cwd=DEST)
git("config", "user.email", "synthetic@example.invalid", cwd=DEST)
for sha in (COLLECTION, ACCOUNTS):
    git("merge", "--no-ff", "-X", "ours", "-m", "Isolated convergence rehearsal", sha, cwd=DEST)
# #110's complete application paths supersede the older #95 placement-time attribution.
for name in (
    "src/bancaemdia/domain/account_attribution.py",
    "src/bancaemdia/domain/account_attribution_service.py",
    "src/bancaemdia/domain/projecao.py",
    "src/bancaemdia/api/v1/apostas.py",
    "src/bancaemdia/api/v1/revisao.py",
    "src/bancaemdia/cli/replay.py",
    "src/bancaemdia/domain/consolidacao_aposta.py",
):
    (DEST / name).write_bytes((SOURCE / name).read_bytes())
git("apply", "--whitespace=error", str(SOURCE / "scripts/convergence/resolution.patch"), cwd=DEST)
# Only this new, still-unmerged revision is rewired; published dependency IDs stay intact.
migration = DEST / "alembic/versions/c110fact2026_aposta_consolidacoes.py"
migration.write_text(
    (SOURCE / migration.relative_to(DEST))
    .read_text(encoding="utf-8")
    .replace('down_revision = "c109match2026"', 'down_revision = "f110prereq2026"'),
    encoding="utf-8",
)
(DEST / "convergence-heads.json").write_text(
    json.dumps(
        {
            "product": head,
            "collection": COLLECTION,
            "accounts": ACCOUNTS,
            "holders": HOLDERS,
            "pairing": PAIRING,
            "scope": "disposable full dependency composition",
        },
        indent=2,
    ),
    encoding="utf-8",
)
sys.stdout.write(f"Rehearsal prepared at {DEST}\n")
