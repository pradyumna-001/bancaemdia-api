from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest

from bancaemdia.domain.materializar import chave_telegram

PAYLOADS = Path(__file__).resolve().parents[1] / "fixtures" / "idempotency"
ORIGENS = ("telegram", "print", "manual", "planilha", "casa")


@pytest.fixture(params=ORIGENS)
def aposta_payload(request: pytest.FixtureRequest) -> dict[str, object]:
    origem = str(request.param)
    dados: dict[str, object] = json.loads((PAYLOADS / f"{origem}.json").read_text())
    identidade = uuid4().hex
    if origem == "telegram":
        dados["chave"] = chave_telegram(int(dados["chat_id"]), int(dados["message_id"]))
    elif origem == "print":
        dados["chave"] = f"p:{dados['midia_hash']}:0"
    elif origem == "manual":
        dados["chave"] = f"m:{identidade}"
    elif origem == "planilha":
        dados["chave"] = f"s:{dados.pop('linha_hash')}"
    else:
        dados["chave"] = f"c:betano:{dados.pop('identidade')}"
    return dados
