"""Bounded calculator HTTP contracts. Decimal values travel as strings."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


DecimalText = str


class Outcome(StrictModel):
    name: str = Field(min_length=1, max_length=80)
    odd: DecimalText = Field(max_length=22)


class MarketRequest(StrictModel):
    outcomes: list[Outcome] = Field(
        min_length=2,
        max_length=20,
        description="Supply every mutually exclusive outcome. Odds alone cannot prove completeness.",
    )


class StakeMarketRequest(MarketRequest):
    total_stake_centavos: int = Field(gt=0, le=1_000_000_000_000)


class HedgeRequest(StrictModel):
    original_stake_centavos: int = Field(gt=0, le=1_000_000_000_000)
    original_odd: DecimalText = Field(max_length=22)
    opposing_odd: DecimalText = Field(max_length=22)
    commission_percentage: DecimalText = Field(default="0", max_length=22)
    stake_type: Literal["cash"] = "cash"
    market_type: Literal["two_way"] = "two_way"


class BankrollRequest(StrictModel):
    bankroll_centavos: int = Field(gt=0, le=1_000_000_000_000)
    percentage: DecimalText | None = Field(default=None, max_length=22)
    stake_centavos: int | None = Field(default=None, ge=0, le=1_000_000_000_000)


class CalculationResponse(StrictModel):
    data: dict[str, JsonValue]
    method: str
    precision: str
    rounding: str
    assumptions: list[str]
    warnings: list[str]
