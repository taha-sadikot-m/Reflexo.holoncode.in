"""Time helpers: injectable clock, business time zone, demo time scaling (POC_01 §6, POC_06 §7.2)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from reflexo.common.config import get_settings

IST = ZoneInfo("Asia/Kolkata")


class Clock(Protocol):
    def now(self) -> datetime: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FixedClock:
    """Test clock; advance explicitly."""

    def __init__(self, at: datetime) -> None:
        if at.tzinfo is None:
            raise ValueError("FixedClock requires an aware datetime")
        self._now = at

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta) -> None:
        self._now += delta


_clock: Clock = SystemClock()


def clock() -> Clock:
    return _clock


def set_clock(c: Clock) -> None:
    global _clock
    _clock = c


def utcnow() -> datetime:
    return _clock.now()


def to_business_tz(ts: datetime, tz: str = "Asia/Kolkata") -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetimes are not allowed")
    return ts.astimezone(ZoneInfo(tz))


def scaled(delta: timedelta, scale: float | None = None) -> timedelta:
    """Apply demo time scaling (REFLEXO_DEMO_TIME_SCALE); 1.0 = real time."""
    factor = get_settings().demo_time_scale if scale is None else scale
    return timedelta(seconds=delta.total_seconds() * factor)
