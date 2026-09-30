"""Pin #107 in a disposable directory. Product branch stays directly on main."""

import argparse
import ast
import hashlib
import io
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

PARENT = "a824c8e85638ebb3823990886e9514aa07f424cc"
ROOT = Path(__file__).resolve().parents[1]
AUTH = (ROOT / "src/bancaemdia/auth/middleware.py").read_text(encoding="utf-8")
METHODS = AUTH[AUTH.index("CATALOG_METHODS =") : AUTH.index("def route_path")]
METHOD_GUARD = AUTH[
    AUTH.index("        allowed = CATALOG_METHODS") : AUTH.index(
        "        if route_path(request) in PUBLIC_PATHS:"
    )
]
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
    patch = ROOT / "scripts/catalogo/credential-middleware.patch"
    subprocess.run(["git", "apply", "--check", str(patch)], cwd=destination, check=True)
    subprocess.run(["git", "apply", str(patch)], cwd=destination, check=True)
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
                s.replace(
                    "from bancaemdia.models.casa import Casa",
                    "from bancaemdia.models.casa import Casa\nfrom bancaemdia.models.casa_dominio import CasaDominio, CatalogoOperador, CatalogoFonte, CatalogoSnapshot, CatalogoPublicacao, CatalogoConfirmacao, CatalogoAuditoria",
                )
                + '\n__all__ += ["CasaDominio", "CatalogoOperador", "CatalogoFonte", "CatalogoSnapshot", "CatalogoPublicacao", "CatalogoConfirmacao", "CatalogoAuditoria"]\n'
            ),
        ),
        (
            "src/bancaemdia/main.py",
            lambda s: (
                s.replace(
                    "from bancaemdia.api.v1 import",
                    "from bancaemdia.api.v1 import coleta_catalogo, admin_casas\nfrom bancaemdia.api.v1 import",
                    1,
                )
                + "\napp.include_router(coleta_catalogo.router)\napp.include_router(admin_casas.router)\n"
            ),
        ),
        (
            "src/bancaemdia/auth/middleware.py",
            lambda s: (
                s
                .replace('"/api/v1/coleta",', '"/api/v1/coleta",\n    "/api/v1/coleta/catalogo",')
                .replace("def route_path", METHODS + "def route_path", 1)
                .replace(
                    "        if route_path(request) in PUBLIC_PATHS:",
                    METHOD_GUARD + "        if route_path(request) in PUBLIC_PATHS:",
                    1,
                )
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
    # Preserve the original rollback assertion, checking the exact composed HEAD.
    pairing_test = destination / "tests/integration/coleta/test_pairing.py"
    original = pairing_test.read_text(encoding="utf-8")
    needle = 'await conn.scalar(text("SELECT version_num FROM alembic_version")) == "c107pair2026"'
    if original.count(needle) != 1:
        raise ValueError("pairing migration acceptance boundary changed")
    pairing_test.write_text(
        original.replace(needle, needle.replace("c107pair2026", "c113integration2026")),
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
    original = path.read_text(encoding="utf-8")
    security_function = next(
        node
        for node in ast.parse(original).body
        if isinstance(node, ast.FunctionDef) and node.name == "_install_collection_security"
    )
    lines = original.splitlines(keepends=True)
    assert security_function.end_lineno is not None
    lines.insert(security_function.end_lineno, "    _install_catalog_security(document)\n")
    path.write_text("".join(lines), encoding="utf-8")
    with path.open("a", encoding="utf-8") as out:
        for name, values in metadata.items():
            out.write(f"\n{name}.update({values!r})\n")
        for node in source.body:
            if isinstance(node, ast.FunctionDef) and node.name == "_install_catalog_security":
                out.write("\n" + ast.unparse(node) + "\n")
            if (
                isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)
                and node.target.id == "REQUEST_EXAMPLES"
            ):
                assert isinstance(node.value, ast.Dict)
                selected = ast.Dict(keys=[], values=[])
                for key, value in zip(node.value.keys, node.value.values, strict=True):
                    if key is not None and ast.literal_eval(key) == (
                        "post",
                        "/api/v1/catalogo/candidatos",
                    ):
                        selected.keys.append(key)
                        selected.values.append(value)
                out.write("\nREQUEST_EXAMPLES.update(" + ast.unparse(selected) + ")\n")
    subprocess.run(
        [sys.executable, "-m", "ruff", "check", "src/", "--fix"], cwd=destination, check=True
    )
    subprocess.run([sys.executable, "-m", "ruff", "format", "src/"], cwd=destination, check=True)

    patch_hash = hashlib.sha256(patch.read_bytes()).hexdigest()
    evidence = {
        "parent_pr": 163,
        "parent_sha": PARENT,
        "product_sha": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "product_base": "main",
        "overlay": list(FILES),
        "published_migration_ids_preserved": True,
        "mandatory_credential_patch_sha256": patch_hash,
    }
    (destination / "catalog-composition.json").write_text(
        json.dumps(evidence, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    prepare(parser.parse_args().destination.resolve())
