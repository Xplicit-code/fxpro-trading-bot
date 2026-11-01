"""Strict historical-data ingestion. Demo prices are explicitly synthetic."""
from __future__ import annotations

import csv
import math
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import Candle, Symbol, parse_time


DEMO_SYMBOLS = {
    "EURUSD": Symbol("EURUSD", 0.00001, 1.0, 0.01, 10, 0.01, 12, 5, margin_per_lot=3500),
    "XAUUSD": Symbol("XAUUSD", 0.01, 1.0, 0.01, 10, 0.01, 30, 2, margin_per_lot=10000),
    "BTCUSD": Symbol("BTCUSD", 0.01, 0.01, 0.01, 10, 0.01, 2500, 2, margin_per_lot=18000),
    "US500": Symbol("US500", 0.1, 0.1, 0.1, 100, 0.1, 6, 1, margin_per_lot=250),
}


def validate_series(candles: list[Candle], minimum: int = 160) -> list[Candle]:
    if len(candles) < minimum:
        raise ValueError(f"At least {minimum} closed candles are required; received {len(candles)}")
    if any(a.time >= b.time for a, b in zip(candles, candles[1:])):
        raise ValueError("Candles must be strictly chronological, with no duplicate timestamps")
    intervals = [(b.time-a.time).total_seconds() for a, b in zip(candles, candles[1:])]
    if any(interval < 300 or not math.isclose(interval/300, round(interval/300), abs_tol=1e-8) for interval in intervals):
        raise ValueError("Expected M5 candles; timestamp gaps must be multiples of five minutes")
    return candles


def load_csv(path: str | Path) -> list[Candle]:
    candles = []
    with Path(path).open(newline="", encoding="utf-8-sig") as stream:
        reader = csv.DictReader(stream)
        if not {"time", "open", "high", "low", "close"}.issubset(reader.fieldnames or []):
            raise ValueError("CSV requires time,open,high,low,close columns; volume is optional")
        for line, row in enumerate(reader, 2):
            try:
                candles.append(Candle(parse_time(row["time"]), *(float(row[k]) for k in ["open", "high", "low", "close"]), float(row.get("volume") or 0)))
            except (ValueError, TypeError) as error:
                raise ValueError(f"Invalid CSV row {line}: {error}") from error
    return validate_series(candles)


def demo_candles(symbol: str, count: int = 720, seed: int = 41) -> list[Candle]:
    """Reproducible UI/research fixture, never a market-data source."""
    spec = DEMO_SYMBOLS[symbol]
    rng = random.Random(seed)
    price = {"EURUSD": 1.085, "XAUUSD": 2700.0, "BTCUSD": 82000.0, "US500": 5800.0}[symbol]
    volatility = {"EURUSD": 0.0007, "XAUUSD": 0.001, "BTCUSD": 0.0018, "US500": 0.0007}[symbol]
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    candles = []
    for i in range(count):
        opening = price
        change = rng.gauss(0, volatility) + math.sin(i / 37) * volatility * 0.16
        price = opening * math.exp(change)
        wick = abs(rng.gauss(0, volatility * 0.5)) * opening
        candles.append(Candle(start + timedelta(minutes=5*i), round(opening, spec.digits), round(max(opening, price) + wick, spec.digits), round(min(opening, price) - wick, spec.digits), round(price, spec.digits), rng.randint(80, 1600)))
    return candles
