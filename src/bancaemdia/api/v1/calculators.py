"""Thin authenticated/stateless HTTP adapter for the four selected calculators."""

from fastapi import APIRouter, HTTPException, Security

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import security
from bancaemdia.api.v1.schemas.calculators import (
    BankrollRequest,
    CalculationResponse,
    HedgeRequest,
    MarketRequest,
    StakeMarketRequest,
)
from bancaemdia.domain.calculators import allocation as alloc
from bancaemdia.domain.calculators import planning, probability
from bancaemdia.domain.calculators.core import Calculation, CalculatorInputError, serialize

router = APIRouter(
    prefix="/api/v1/calculadoras",
    tags=["calculadoras"],
    dependencies=[Security(security)],
    responses=AUTHENTICATED_ERROR_RESPONSES,
)


def _respond(operation: Calculation) -> CalculationResponse:
    return CalculationResponse(
        data=serialize(operation.data),
        method=operation.method,
        precision=operation.precision,
        rounding=operation.rounding,
        assumptions=list(operation.assumptions),
        warnings=list(operation.warnings),
    )


def _invalid(error: CalculatorInputError) -> HTTPException:
    return HTTPException(status_code=422, detail=str(error))


@router.post("/mercado-justo", response_model=CalculationResponse)
def fair(body: MarketRequest) -> CalculationResponse:
    try:
        return _respond(probability.fair([(item.name, item.odd) for item in body.outcomes]))
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/distribuir-entre-resultados", response_model=CalculationResponse)
def distribute(body: StakeMarketRequest) -> CalculationResponse:
    try:
        return _respond(
            alloc.distribute(
                [(item.name, item.odd) for item in body.outcomes],
                body.total_stake_centavos,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/cobertura-ao-vivo", response_model=CalculationResponse)
def hedge(body: HedgeRequest) -> CalculationResponse:
    try:
        return _respond(
            planning.live_hedge(
                body.original_stake_centavos,
                body.original_odd,
                body.opposing_odd,
                body.commission_percentage,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/percentual-banca", response_model=CalculationResponse)
def bankroll(body: BankrollRequest) -> CalculationResponse:
    try:
        return _respond(
            planning.bankroll_percent(
                body.bankroll_centavos,
                percent=body.percentage,
                stake_cents=body.stake_centavos,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error
