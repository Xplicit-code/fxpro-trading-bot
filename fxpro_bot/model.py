"""Small regularized logistic model with training-only feature normalization.

The score is uncalibrated and is not a promise of profitability. No future bars
are used to construct features, fit scalers, or label training observations.
"""
from __future__ import annotations

import math
from statistics import fmean, pstdev

from .domain import Candle

LOOKBACK = 24
MIN_TRAIN = 100


def atr(bars: list[Candle], period: int = 14) -> float:
    if len(bars) < period + 1:
        raise ValueError("Not enough bars for ATR")
    return fmean(max(bars[i].high - bars[i].low, abs(bars[i].high - bars[i-1].close), abs(bars[i].low - bars[i-1].close)) for i in range(len(bars)-period, len(bars)))


def features(bars: list[Candle]) -> list[float]:
    if len(bars) < LOOKBACK + 1:
        raise ValueError("Not enough historical bars")
    closes = [b.close for b in bars[-LOOKBACK-1:]]
    returns = [math.log(b/a) for a, b in zip(closes, closes[1:])]
    scale = max(pstdev(returns), 1e-8)
    last = bars[-1]
    width = max(last.high-last.low, last.close * 1e-8)
    return [
        returns[-1] / scale,
        sum(returns[-3:]) / (scale * math.sqrt(3)),
        sum(returns[-12:]) / (scale * math.sqrt(12)),
        (closes[-1]/fmean(closes[-20:])-1) / scale,
        (last.close-last.open) / width,
        (last.close-last.low) / width - 0.5,
    ]


class SignalModel:
    def __init__(self):
        self.weights = [0.0] * 6
        self.bias = 0.0
        self.means = [0.0] * 6
        self.scales = [1.0] * 6
        self.trained_through: str | None = None
        self.samples = 0

    @staticmethod
    def sigmoid(value: float) -> float:
        return 1 / (1 + math.exp(-max(-35, min(35, value))))

    def fit(self, closed_bars: list[Candle]) -> None:
        bars = closed_bars[-425:]
        # Last label uses the last CLOSED candle. The final candle itself has
        # no observed next close yet and is never a labeled training row.
        rows = [(features(bars[:i+1]), float(bars[i+1].close > bars[i].close)) for i in range(LOOKBACK, len(bars)-1)]
        if len(rows) < MIN_TRAIN:
            raise ValueError(f"Model needs at least {MIN_TRAIN+LOOKBACK+1} closed bars")
        self.means = [fmean(row[0][j] for row in rows) for j in range(6)]
        self.scales = [max(pstdev(row[0][j] for row in rows), 1e-6) for j in range(6)]
        xs = [[(x[j]-self.means[j])/self.scales[j] for j in range(6)] for x, _ in rows]
        ys = [y for _, y in rows]
        self.weights, self.bias = [0.0] * 6, 0.0
        for _ in range(90):
            gradient, bias_gradient = [0.0] * 6, 0.0
            for x, y in zip(xs, ys):
                error = self.sigmoid(self.bias + sum(w*v for w, v in zip(self.weights, x))) - y
                bias_gradient += error
                for j in range(6):
                    gradient[j] += error * x[j]
            n = len(rows)
            self.weights = [w - 0.12 * (g/n + 0.035*w) for w, g in zip(self.weights, gradient)]
            self.bias -= 0.12 * bias_gradient/n
        self.samples = len(rows)
        self.trained_through = closed_bars[-1].time.isoformat()

    def predict(self, closed_bars: list[Candle]) -> float:
        if not self.trained_through:
            raise ValueError("Train the signal model first")
        x = features(closed_bars)
        normalized = [(x[j]-self.means[j])/self.scales[j] for j in range(6)]
        return self.sigmoid(self.bias + sum(w*v for w, v in zip(self.weights, normalized)))
