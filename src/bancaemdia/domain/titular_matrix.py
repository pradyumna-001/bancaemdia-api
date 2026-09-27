"""Public holder/account matrix derived from stable accounts and usage intervals."""

from __future__ import annotations

from datetime import datetime
from typing import TypedDict

from bancaemdia import models


class Intervalo(TypedDict):
    vigente_de: datetime | None
    vigente_ate: datetime | None
    origem: str


def account_row(
    account: models.ContaCasa,
    house_name: str,
    holder_name: str | None,
    holder_archived: bool,
    history: list[models.UsoContaCasa],
    *,
    another_current: bool,
) -> dict[str, object]:
    intervals: list[Intervalo] = [
        {"vigente_de": item.vigente_de, "vigente_ate": item.vigente_ate, "origem": item.origem}
        for item in history
    ]
    active = next((item for item in intervals if item["vigente_ate"] is None), None)
    actions: list[str] = []
    if not holder_archived and account.estado == "DISPONIVEL" and active is None:
        actions.append("TROCAR_PARA" if another_current else "ATIVAR")
    if active is not None and account.estado == "EM_USO":
        actions.append("TROCAR_DE")
    if not holder_archived and active is None and account.estado != "EM_USO":
        actions.append("EDITAR")
    return {
        "conta_casa_id": account.id,
        "titular_id": account.titular_id,
        "titular_nome": holder_name,
        "casa_id": account.casa_id,
        "casa_nome": house_name,
        "apelido": account.apelido,
        "estado": account.estado,
        "ativa": account.ativa,
        "intervalo_ativo": active,
        "historico_uso": intervals,
        "acoes_validas": actions,
    }
