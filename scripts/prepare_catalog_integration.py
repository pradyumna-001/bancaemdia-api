"""Pin #107 in a disposable directory. Product branch stays directly on main."""

import argparse
import ast
import io
import json
import shutil
import subprocess
import tarfile
from pathlib import Path

PARENT = "a824c8e85638ebb3823990886e9514aa07f424cc"
ROOT = Path(__file__).resolve().parents[1]
FILES = (
    "src/bancaemdia/coleta/catalogo.py",
    "src/bancaemdia/coleta/catalogo_fontes.py",
    "src/bancaemdia/models/casa_dominio.py",
    "src/bancaemdia/services/catalogo.py",
    "src/bancaemdia/services/catalogo_assinatura.py",
    "src/bancaemdia/api/v1/admin_casas.py",
    "src/bancaemdia/api/v1/coleta_catalogo.py",
    "alembic/versions/c113catalog2026_catalogo_casas.py",
    "scripts/sincronizar_catalogo_casas.py",
    "docs/catalogo/fontes-estaduais.json",
)


def prepare(destination: Path) -> None:
    if destination.exists():
        raise ValueError("use a fresh disposable integration directory")
    destination.mkdir(parents=True)
    subprocess.run(["git", "fetch", "origin", "refs/pull/163/head"], cwd=ROOT, check=True)
    archive = subprocess.check_output(["git", "archive", PARENT], cwd=ROOT)
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        tar.extractall(destination, filter="data")
    for name in FILES:
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / name, target)
    shutil.copytree(
        ROOT / "tests/catalogo",
        destination / "tests/catalogo",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    for name, transform in (
        (
            "src/bancaemdia/models/__init__.py",
            lambda s: (
                s
                + "\nfrom bancaemdia.models.casa_dominio import CasaDominio, CatalogoOperador, CatalogoFonte, CatalogoSnapshot, CatalogoPublicacao, CatalogoConfirmacao, CatalogoAuditoria\n"
            ),
        ),
        (
            "src/bancaemdia/main.py",
            lambda s: (
                s
                + "\nfrom bancaemdia.api.v1 import coleta_catalogo, admin_casas\napp.include_router(coleta_catalogo.router)\napp.include_router(admin_casas.router)\n"
            ),
        ),
        (
            "src/bancaemdia/auth/middleware.py",
            lambda s: s.replace(
                '"/api/v1/coleta",', '"/api/v1/coleta",\n    "/api/v1/coleta/catalogo",'
            ),
        ),
        (
            "src/bancaemdia/middleware/rate_limit.py",
            lambda s: s.replace(
                'if path in {"/coleta", "/api/v1/coleta"}:',
                'if path == "/api/v1/coleta/catalogo":\n        return COLETA_POLICY if request.method == "GET" else None\n    if path in {"/coleta", "/api/v1/coleta"}:',
            ),
        ),
    ):
        path = destination / name
        original = path.read_text(encoding="utf-8")
        updated = transform(original)
        if updated == original:
            raise ValueError(f"integration boundary changed: {name}")
        path.write_text(updated, encoding="utf-8")
    # Preserve #107's published migration; explicitly merge both branches in this fixture only.
    merge = destination / "alembic/versions/c113integration2026_merge_catalog_pairing.py"
    merge.write_text(
        '"""Disposable catalog/pairing composition."""\nrevision = "c113integration2026"\ndown_revision = ("c107pair2026", "c113catalog2026")\nbranch_labels = depends_on = None\ndef upgrade(): pass\ndef downgrade(): pass\n',
        encoding="utf-8",
    )
    # #107's OpenAPI contains its own operations; add exactly the new contract metadata.
    source = ast.parse((ROOT / "src/bancaemdia/api/openapi.py").read_text(encoding="utf-8"))
    metadata = {}
    for node in source.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "OPERATION_DOCUMENTATION"
        ):
            metadata["OPERATION_DOCUMENTATION"] = {
                k: v
                for k, v in ast.literal_eval(node.value).items()
                if "catalogo" in k[1] or "admin/casas" in k[1]
            }
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "PARAMETER_DESCRIPTIONS" for t in node.targets
        ):
            metadata["PARAMETER_DESCRIPTIONS"] = ast.literal_eval(node.value)
    path = destination / "src/bancaemdia/api/openapi.py"
    with path.open("a", encoding="utf-8") as out:
        for name, values in metadata.items():
            out.write(f"\n{name}.update({values!r})\n")
    evidence = {
        "parent_pr": 163,
        "parent_sha": PARENT,
        "product_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "product_base": "main",
        "overlay": list(FILES),
        "published_migration_ids_preserved": True,
    }
    (destination / "catalog-composition.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    prepare(parser.parse_args().destination.resolve())
