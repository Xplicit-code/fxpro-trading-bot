from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("Timestamps must include a UTC offset, for example 2026-01-01T12:00:00Z")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class Candle:
    time: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float = 0

    def __post_init__(self):
        if self.time.tzinfo is None:
            raise ValueError("Candle timestamps must be timezone-aware")
        if not all(math.isfinite(x) and x > 0 for x in [self.open, self.high, self.low, self.close]):
            raise ValueError("OHLC prices must be positive finite numbers")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Invalid OHLC price range")
        if not math.isfinite(self.volume) or self.volume < 0:
            raise ValueError("Volume must be a nonnegative finite number")


@dataclass(frozen=True)
class Quote:
    bid: float
    ask: float
    time: datetime

    def __post_init__(self):
        if not all(math.isfinite(x) and x > 0 for x in [self.bid, self.ask]) or self.ask < self.bid:
            raise ValueError("Quote must have positive finite prices with ask >= bid")
        if self.time.tzinfo is None:
            raise ValueError("Quote timestamps must be timezone-aware")

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Symbol:
    name: str
    tick_size: float
    tick_value: float  # Account-currency cash value per tick per lot.
    volume_min: float
    volume_max: float
    volume_step: float
    spread_ticks: float
    digits: int
    currency: str = "USD"
    margin_per_lot: float = 1000.0  # Simulation only; MT5 uses order_calc_margin.
    min_stop_distance: float = 0.0

    def __post_init__(self):
        for key in ["tick_size", "tick_value", "volume_min", "volume_max", "volume_step", "margin_per_lot"]:
            value = getattr(self, key)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{key} must be positive and finite")
        if self.volume_max < self.volume_min or self.spread_ticks < 0 or not math.isfinite(self.spread_ticks):
            raise ValueError("Invalid symbol volume limits or spread")
        if not math.isfinite(self.min_stop_distance) or self.min_stop_distance < 0:
            raise ValueError("Invalid minimum stop distance")
        if not isinstance(self.digits, int) or not 0 <= self.digits <= 10:
            raise ValueError("digits must be between 0 and 10")

    def pnl(self, side: str, entry: float, exit_price: float, volume: float) -> float:
        sign = 1 if side == "buy" else -1
        return sign * (exit_price - entry) / self.tick_size * self.tick_value * volume

    def floor_volume(self, volume: float) -> float:
        # Decimal arithmetic prevents rounding upward through a risk limit.
        step = Decimal(str(self.volume_step))
        capped = min(Decimal(str(volume)), Decimal(str(self.volume_max)))
        result = (capped / step).to_integral_value(rounding=ROUND_FLOOR) * step
        return float(result) if result >= Decimal(str(self.volume_min)) else 0.0


@dataclass
class Position:
    ticket: str
    symbol: str
    side: str
    volume: float
    entry: float
    stop: float
    take: float
    opened_at: str


@dataclass
class Trade:
    ticket: str
    symbol: str
    side: str
    volume: float
    entry: float
    exit: float
    stop: float
    take: float
    opened_at: str
    closed_at: str
    pnl: float
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)
