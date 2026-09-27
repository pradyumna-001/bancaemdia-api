"""Thin authenticated/stateless HTTP adapter for the nine standard calculators."""

from fastapi import APIRouter, HTTPException, Security

from bancaemdia.api.contracts import AUTHENTICATED_ERROR_RESPONSES
from bancaemdia.api.deps import security
from bancaemdia.api.v1.schemas.calculators import (
    BankrollRequest,
    CalculationResponse,
    HedgeRequest,
    MarketRequest,
    OddRequest,
    SplitRequest,
    StakeMarketRequest,
    TargetRequest,
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


@router.post("/probabilidade-implicita", response_model=CalculationResponse)
def implied(body: OddRequest) -> CalculationResponse:
    try:
        return _respond(probability.implied(body.odd))
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/mercado-justo", response_model=CalculationResponse)
def fair(body: MarketRequest) -> CalculationResponse:
    try:
        return _respond(probability.fair([(item.name, item.odd) for item in body.outcomes]))
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/rtp", response_model=CalculationResponse)
def rtp(body: MarketRequest) -> CalculationResponse:
    try:
        return _respond(probability.rtp([(item.name, item.odd) for item in body.outcomes]))
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/surebet", response_model=CalculationResponse)
def surebet(body: StakeMarketRequest) -> CalculationResponse:
    try:
        return _respond(
            alloc.surebet(
                [(item.name, item.odd) for item in body.outcomes],
                body.total_stake_centavos,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/dutching", response_model=CalculationResponse)
def dutching(body: StakeMarketRequest) -> CalculationResponse:
    try:
        return _respond(
            alloc.dutching(
                [(item.name, item.odd) for item in body.outcomes],
                body.total_stake_centavos,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/dividir-stake", response_model=CalculationResponse)
def split(body: SplitRequest) -> CalculationResponse:
    try:
        return _respond(
            alloc.split(
                body.total_stake_centavos,
                [(item.name, item.value, item.odd) for item in body.selections],
                body.mode,
                body.normalize_weights,
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
                body.objective,
                body.commission_percentage,
            )
        )
    except CalculatorInputError as error:
        raise _invalid(error) from error


@router.post("/lucro-alvo", response_model=CalculationResponse)
def target(body: TargetRequest) -> CalculationResponse:
    try:
        return _respond(planning.target_profit(body.odd, body.target_profit_centavos))
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
