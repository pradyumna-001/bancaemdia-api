"""Historical read operation explicitly available in FULL_WRITE and READ_ONLY."""

from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES, ErrorResponse
from bancaemdia.api.deps import get_current_user_snapshot
from bancaemdia.api.v1.painel_filtrado import _privado, _snapshot
from bancaemdia.db.session import get_db_snapshot
from bancaemdia.domain.access import require_operation_access
from bancaemdia.domain.filtros_apostas import VisibilidadeApostas, filtros_do_site
from bancaemdia.domain.hipotetica import Motivo
from bancaemdia.domain.painel import PeriodoPainel
from bancaemdia.domain.registros import Usuario
from bancaemdia.models.aposta import ESTADOS, ORIGENS
from bancaemdia.repositories.hipotetica_repo import HipoteticaRepo

router = APIRouter(
    responses={
        **{
            code: definition
            for code, definition in AUTHENTICATED_ERROR_RESPONSES.items()
            if code != 402
        },
        404: {
            "model": ErrorResponse,
            "description": "Identificador de filtro inexistente ou não autorizado; mensagem idêntica.",
        },
    }
)
type CentavosHipotetica = Annotated[str, Field(pattern=r"^-?(0|[1-9][0-9]*)$", max_length=50)]
type IdentificadorHipotetica = Annotated[str, Field(pattern=r"^[1-9][0-9]{0,18}$", max_length=19)]
type ContagemHipotetica = Annotated[int, Field(ge=0, le=10_000)]


class ModeloHipotetica(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FiltrosHipotetica(ModeloHipotetica):
    periodo: PeriodoPainel | None = None
    desde: datetime | None = None
    ate: datetime | None = None
    casa_id: IdentificadorHipotetica | None = None
    tipster_id: IdentificadorHipotetica | None = None
    mercado_id: IdentificadorHipotetica | None = None
    competicao_id: IdentificadorHipotetica | None = None
    titular_id: IdentificadorHipotetica | None = None
    conta_casa_id: IdentificadorHipotetica | None = None
    grupo_id: IdentificadorHipotetica | None = None
    banca_id: IdentificadorHipotetica | None = None
    estado: str | None = None
    origem: str | None = None
    revisao_grave: bool | None = None
    visibilidade: VisibilidadeApostas
    incluir_apagadas: bool


class CenarioHipotetica(ModeloHipotetica):
    total_apostas: ContagemHipotetica
    liquidadas: ContagemHipotetica
    pendentes: ContagemHipotetica
    em_revisao: ContagemHipotetica
    fora_do_financeiro: ContagemHipotetica
    anuladas: ContagemHipotetica
    resultados_desconhecidos: ContagemHipotetica
    freebets: ContagemHipotetica
    giro_centavos: CentavosHipotetica | None
    base_roi_centavos: CentavosHipotetica | None
    retorno_centavos: CentavosHipotetica | None
    lucro_centavos: CentavosHipotetica | None
    exposicao_pendente_centavos: CentavosHipotetica | None
    face_pendente_centavos: CentavosHipotetica | None
    roi: str | None
    win_rate: str | None


class DiferencasHipotetica(ModeloHipotetica):
    giro_centavos: CentavosHipotetica | None
    base_roi_centavos: CentavosHipotetica | None
    retorno_centavos: CentavosHipotetica | None
    lucro_centavos: CentavosHipotetica | None
    exposicao_pendente_centavos: CentavosHipotetica | None


class LimitacaoHipotetica(ModeloHipotetica):
    motivo: Motivo
    quantidade: ContagemHipotetica


class HipoteticaSaida(ModeloHipotetica):
    regra: Literal["stake_dica_confirmada_odd_resultado_reais_v1"] = (
        "stake_dica_confirmada_odd_resultado_reais_v1"
    )
    estado: Literal[
        "sem_apostas",
        "sem_vinculos_elegiveis",
        "sem_stakes_diferentes",
        "comparado",
        "parcial",
        "indisponivel",
    ]
    escopo_financeiro: Literal["liquidadas_sem_revisao_sem_dica_consolidada"] = (
        "liquidadas_sem_revisao_sem_dica_consolidada"
    )
    moeda: Literal["BRL"] = "BRL"
    unidade: Literal["centavos"] = "centavos"
    arredondamento: Literal["ROUND_HALF_EVEN_por_etapa_de_liquidacao"] = (
        "ROUND_HALF_EVEN_por_etapa_de_liquidacao"
    )
    precisao_decimal: Literal[80] = 80
    real: CenarioHipotetica
    hipotetico: CenarioHipotetica
    diferencas: DiferencasHipotetica
    apostas_consideradas: ContagemHipotetica
    vinculos_confirmados: ContagemHipotetica
    stakes_diferentes: ContagemHipotetica
    apostas_alteradas: ContagemHipotetica
    apostas_indisponiveis: ContagemHipotetica
    limitacoes: list[LimitacaoHipotetica]
    filtros: FiltrosHipotetica
    criterio_temporal: Literal["data_aposta"] = "data_aposta"
    fuso_horario: Literal["America/Sao_Paulo"] = "America/Sao_Paulo"
    fonte_dados: Literal["apostas_ao_vivo"] = "apostas_ao_vivo"
    snapshot_em: datetime
    respondido_em: datetime


@router.get(
    "/api/v1/painel/hipotetica",
    response_model=HipoteticaSaida,
    openapi_extra={
        "x-commercial-operation": "hipotetica_historica",
        "x-commercial-access": ["FULL_WRITE", "READ_ONLY"],
        "x-maximum-population": 10_000,
    },
)
async def consultar_hipotetica(
    request: Request,
    response: Response,
    usuario: Annotated[Usuario, Depends(get_current_user_snapshot)],
    session: Annotated[AsyncSession, Depends(get_db_snapshot)],
    filtros: Annotated[dict[str, object], Depends(filtros_do_site)],
    fresh: bool = False,
) -> HipoteticaSaida:
    _ = fresh  # Router chooses primary; never refresh MVs or commit writes.
    _privado(response)
    if set(request.query_params) - (set(FiltrosHipotetica.model_fields) | {"fresh"}):
        raise HTTPException(422, "parametro_nao_suportado")
    if len(request.query_params.multi_items()) != len(request.query_params):
        raise HTTPException(422, "parametro_repetido")
    if (filtros.get("estado") is not None and filtros["estado"] not in ESTADOS) or (
        filtros.get("origem") is not None and filtros["origem"] not in ORIGENS
    ):
        raise HTTPException(422, "estado_ou_origem_invalido")
    normalized = {
        k: str(v) if k.endswith("_id") and v is not None else v for k, v in filtros.items()
    }
    normalized["periodo"] = request.query_params.get("periodo")
    try:
        await require_operation_access(session, usuario.id, "hipotetica_historica")
        return HipoteticaSaida.model_validate({
            **await HipoteticaRepo().consultar(session, usuario.id, filtros),
            "filtros": normalized,
            **await _snapshot(session),
        })
    except DBAPIError as exc:
        raise HTTPException(503, "hipotetica_temporariamente_indisponivel") from exc
