"""Bound SQLAlchemy predicates shared by list, summary, charts and XLSX."""

from sqlalchemy import Select, select

from bancaemdia import models
from bancaemdia.domain.filtros_apostas import VisibilidadeApostas
from bancaemdia.repositories.aposta_consolidacao import financial_predicate


def selecionar_apostas(usuario_id: int, filtros: dict[str, object]) -> Select[tuple[models.Aposta]]:
    a = models.Aposta
    stmt = select(a).where(a.usuario_id == usuario_id)
    visibilidade = filtros.get("visibilidade") or (
        VisibilidadeApostas.TODAS if filtros.get("incluir_apagadas") else VisibilidadeApostas.ATIVAS
    )
    if visibilidade != VisibilidadeApostas.TODAS:
        stmt = stmt.where(a.selecionada.is_(visibilidade == VisibilidadeApostas.ATIVAS))
    if visibilidade == VisibilidadeApostas.ATIVAS:
        # Preserve the integrated active-list contract; all/deleted retain sources.
        stmt = stmt.where(financial_predicate())
    for campo in ("estado", "origem", "tipster_id", "mercado_id", "competicao_id", "revisao_grave"):
        if filtros.get(campo) is not None:
            stmt = stmt.where(getattr(a, campo) == filtros[campo])
    for campo in ("conta_casa_id", "titular_id", "casa_id"):
        if filtros.get(campo) is not None:
            coluna = (
                models.ContaCasa.id
                if campo == "conta_casa_id"
                else getattr(models.ContaCasa, campo)
            )
            stmt = stmt.where(
                a.conta_casa_id.in_(
                    select(models.ContaCasa.id).where(
                        models.ContaCasa.usuario_id == usuario_id, coluna == filtros[campo]
                    )
                )
            )
    if filtros.get("banca_id") is not None:
        stmt = stmt.where(
            a.banca_id.in_(
                select(models.Banca.id).where(
                    models.Banca.usuario_id == usuario_id, models.Banca.id == filtros["banca_id"]
                )
            )
        )
    if filtros.get("grupo_id") is not None:
        stmt = stmt.where(
            select(models.ApostaGrupo.aposta_id)
            .where(
                models.ApostaGrupo.usuario_id == usuario_id,
                models.ApostaGrupo.grupo_id == filtros["grupo_id"],
                models.ApostaGrupo.aposta_id == a.id,
            )
            .exists()
        )
    if filtros.get("desde") is not None:
        stmt = stmt.where(a.data_aposta >= filtros["desde"])
    if filtros.get("ate") is not None:
        stmt = stmt.where(a.data_aposta < filtros["ate"])
    return stmt
