from __future__ import annotations

import math
from dataclasses import asdict, dataclass


def policy_key(mode: str, server: str = "", account: int = 0) -> str:
    return "risk:paper" if mode == "paper" else f"risk:{mode}:{server}:{account}"


@dataclass(frozen=True)
class RiskConfig:
    risk_fraction: float = 0.005
    daily_loss_fraction: float = 0.02
    max_drawdown_fraction: float = 0.10
    max_positions: int = 1
    max_spread_atr: float = 0.15
    stop_atr: float = 1.8
    reward_risk: float = 1.8
    max_margin_fraction: float = 0.25
    max_quote_age_seconds: float = 20
    max_bar_age_seconds: float = 900
    commission_per_lot: float = 7.0  # Round trip, account currency; configure for actual account.
    slippage_ticks: float = 2.0
    threshold: float = 0.57
    max_holding_bars: int = 24

    def __post_init__(self):
        for key, value in asdict(self).items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a finite number")
        if not 0 < self.risk_fraction <= 0.02:
            raise ValueError("Risk per trade must be between 0 and 2%")
        if not 0 < self.daily_loss_fraction <= 0.1:
            raise ValueError("Daily loss limit must be between 0 and 10%")
        if not 0 < self.max_drawdown_fraction <= 0.5:
            raise ValueError("Drawdown limit must be between 0 and 50%")
        if self.max_positions != 1:
            raise ValueError("This version supports one position at a time")
        if not 0.5 < self.threshold < 1:
            raise ValueError("Signal threshold must be between 0.5 and 1")
        for key in ["max_spread_atr", "stop_atr", "reward_risk", "max_margin_fraction", "max_quote_age_seconds", "max_bar_age_seconds"]:
            if getattr(self, key) <= 0:
                raise ValueError(f"{key} must be positive")
        if self.max_margin_fraction > 1:
            raise ValueError("Margin fraction must not exceed 1")
        if self.commission_per_lot < 0 or self.slippage_ticks < 0:
            raise ValueError("Costs cannot be negative")
        if self.max_bar_age_seconds > 900 or self.max_quote_age_seconds > 60:
            raise ValueError("Freshness limits cannot exceed 900 seconds for bars or 60 seconds for quotes")
        if not isinstance(self.max_holding_bars, int) or self.max_holding_bars < 1:
            raise ValueError("max_holding_bars must be a positive integer")
