"""Model prices, the rupee rate and cost arithmetic (BETA_12 §6; BETA_01 §4.1 `common.costs`).

* Prices come only from `config/model_prices.yaml` (`REFLEXO_MODEL_PRICES`); every metered row stores the file's
  `version`. A model id is matched exactly, else by the longest price key it starts with.
* The rupee rate is the latest `fx_rates` row on or before today (written daily by the job `costs.fx_refresh`),
  so a call is priced at the rate of its day and history is never re-priced.
* Cost of one call (BETA_12 §6.3):
  usd = (input - cached) × input_per_m / 1e6 + cached × cached_input_per_m / 1e6 + output × output_per_m / 1e6
        + audio_seconds / 60 × audio_per_min + characters / 1000 × per_1k_chars
  cost_micro_inr = round(usd × fx_rate × 1e6)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field
from sqlalchemy import text

from reflexo.common.config import get_settings
from reflexo.common.db import ServiceReason, get_db
from reflexo.common.timeutil import utcnow

FX_CACHE_S = 600.0
MICRO = Decimal(1_000_000)


class PriceMissing(LookupError):
    """A model in use has no price: the price file must be updated before it can be metered."""


class Price(BaseModel):
    input_per_m: Decimal = Decimal(0)
    cached_input_per_m: Decimal | None = None  # None: cached input is billed as normal input
    output_per_m: Decimal = Decimal(0)
    audio_input_per_m: Decimal = Decimal(0)  # audio input tokens (not used by the POC's calls yet)
    audio_per_min: Decimal = Decimal(0)
    text_out_per_min: Decimal = Decimal(0)
    per_min: Decimal = Decimal(0)  # a session minute (LiveKit), applied to audio_seconds like audio_per_min
    per_1k_chars: Decimal = Decimal(0)


class Usage(BaseModel):
    """What one metered call consumed. Tokens from the provider's own usage report; never estimated in prompts."""

    input_tokens: int = Field(default=0, ge=0)
    cached_input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    audio_seconds: Decimal = Field(default=Decimal(0), ge=0)
    characters: int = Field(default=0, ge=0)


@dataclass(frozen=True)
class PriceBook:
    version: str
    prices: dict[tuple[str, str], Price]

    def price(self, provider: str, model: str) -> Price:
        exact = self.prices.get((provider, model))
        if exact is not None:
            return exact
        prefixes = [m for (p, m) in self.prices if p == provider and model.startswith(m)]
        if not prefixes:
            raise PriceMissing(f"no price for {provider}/{model} in the price file")
        return self.prices[(provider, max(prefixes, key=len))]


def load_price_book(path: str | Path) -> PriceBook:
    raw: dict[str, Any] = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    prices = {
        (provider, model): Price.model_validate({k: Decimal(str(v)) for k, v in fields.items()})
        for provider, spec in (raw.get("providers") or {}).items()
        for model, fields in (spec.get("models") or {}).items()
    }
    return PriceBook(version=str(raw["version"]), prices=prices)


@lru_cache(maxsize=1)
def price_book() -> PriceBook:
    return load_price_book(get_settings().model_prices)


def cost_usd(price: Price, usage: Usage) -> Decimal:
    uncached = max(usage.input_tokens - usage.cached_input_tokens, 0)
    cached_rate = price.input_per_m if price.cached_input_per_m is None else price.cached_input_per_m
    minutes = usage.audio_seconds / 60
    return (
        Decimal(uncached) * price.input_per_m / MICRO
        + Decimal(usage.cached_input_tokens) * cached_rate / MICRO
        + Decimal(usage.output_tokens) * price.output_per_m / MICRO
        + minutes * (price.audio_per_min + price.text_out_per_min + price.per_min)
        + Decimal(usage.characters) / 1000 * price.per_1k_chars
    )


def micro_inr(usd: Decimal, fx_rate: Decimal) -> int:
    return int((usd * fx_rate * MICRO).quantize(Decimal(1)))


# ---------------------------------------------------------------- the rupee rate

_fx: tuple[float, date, Decimal] | None = None  # (fetched at monotonic, for day, rate)


async def fx_rate(today: date | None = None) -> Decimal:
    """USD->INR for `today`: the latest `fx_rates` row on or before it (cached in-process for 10 minutes)."""
    global _fx
    day = today or utcnow().date()
    if _fx is not None and _fx[1] == day and time.monotonic() - _fx[0] < FX_CACHE_S:
        return _fx[2]
    async with get_db().service_session(ServiceReason.COSTS) as s:
        rate = (
            await s.execute(text("SELECT usd_inr FROM fx_rates WHERE day <= :d ORDER BY day DESC LIMIT 1"), {"d": day})
        ).scalar_one_or_none()
    if rate is None:
        raise LookupError("fx_rates is empty: run costs.fx_refresh (migration 0008 seeds the first rate)")
    _fx = (time.monotonic(), day, Decimal(str(rate)))
    return _fx[2]


def forget_fx_cache() -> None:
    global _fx
    _fx = None


@dataclass(frozen=True)
class Priced:
    cost_micro_inr: int
    price_version: str
    fx_rate: Decimal


async def price_call(provider: str, model: str, usage: Usage) -> Priced:
    book = price_book()
    rate = await fx_rate()
    return Priced(micro_inr(cost_usd(book.price(provider, model), usage), rate), book.version, rate)
