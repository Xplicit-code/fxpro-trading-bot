from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .config import RiskConfig
from .domain import Quote, Symbol


@dataclass(frozen=True)
class Account:
    balance: float
    equity: float
    free_margin: float
    day_start_equity: float
    peak_equity: float
    positions: int


@dataclass(frozen=True)
class OrderPlan:
    side: str
    volume: float
    entry: float
    stop: float
    take: float
    cash_risk: float


class RiskRejected(ValueError):
    pass


def account_guard(account: Account, config: RiskConfig) -> None:
    if not all(math.isfinite(x) for x in [account.balance, account.equity, account.free_margin, account.day_start_equity, account.peak_equity]):
        raise RiskRejected("Account data is invalid")
    if min(account.equity, account.day_start_equity, account.peak_equity) <= 0:
        raise RiskRejected("Account has no available equity")
    if account.equity <= account.day_start_equity * (1-config.daily_loss_fraction):
        raise RiskRejected("Daily loss limit reached")
    if account.equity <= account.peak_equity * (1-config.max_drawdown_fraction):
        raise RiskRejected("Maximum drawdown limit reached")


def plan_order(spec: Symbol, quote: Quote, volatility: float, score: float,
               account: Account, config: RiskConfig, now: datetime,
               loss_per_lot=None, margin_per_lot=None) -> OrderPlan:
    account_guard(account, config)
    age = (now-quote.time).total_seconds()
    if age > config.max_quote_age_seconds or age < -5:
        raise RiskRejected("Quote is stale or has a future timestamp")
    if account.positions >= config.max_positions:
        raise RiskRejected("Position limit reached")
    if not math.isfinite(volatility) or volatility <= 0:
        raise RiskRejected("Volatility data is invalid")
    if quote.spread > volatility * config.max_spread_atr:
        raise RiskRejected("Spread exceeds the volatility-based limit")
    if not math.isfinite(score) or not 0 <= score <= 1:
        raise RiskRejected("Model score is invalid")
    if score >= config.threshold:
        side, entry = "buy", quote.ask
    elif score <= 1-config.threshold:
        side, entry = "sell", quote.bid
    else:
        raise RiskRejected("Signal is below the entry threshold")
    distance = max(volatility * config.stop_atr, spec.min_stop_distance + quote.spread + spec.tick_size)
    sign = 1 if side == "buy" else -1
    # Round protective prices outward, then size against the actual distance.
    stop_raw = (entry-sign*distance)/spec.tick_size
    take_raw = (entry+sign*distance*config.reward_risk)/spec.tick_size
    stop = round((math.floor(stop_raw) if side == "buy" else math.ceil(stop_raw))*spec.tick_size, spec.digits)
    take = round((math.ceil(take_raw) if side == "buy" else math.floor(take_raw))*spec.tick_size, spec.digits)
    if min(stop, take) <= 0:
        raise RiskRejected("Protective prices would be invalid")
    risk = abs(loss_per_lot(side, entry, stop)) if loss_per_lot else abs(spec.pnl(side, entry, stop, 1))
    risk += config.commission_per_lot + 2*config.slippage_ticks*spec.tick_value
    if not math.isfinite(risk) or risk <= 0:
        raise RiskRejected("Cannot calculate the cash risk")
    remaining_daily = account.equity-account.day_start_equity*(1-config.daily_loss_fraction)
    remaining_drawdown = account.equity-account.peak_equity*(1-config.max_drawdown_fraction)
    budget = min(account.equity*config.risk_fraction, remaining_daily, remaining_drawdown)
    margin = margin_per_lot(side, entry) if margin_per_lot else spec.margin_per_lot
    if not math.isfinite(margin) or margin <= 0:
        raise RiskRejected("Cannot calculate required margin")
    max_margin = min(account.free_margin, account.equity*config.max_margin_fraction)
    volume = spec.floor_volume(min(budget/risk, max_margin/margin))
    if volume == 0:
        raise RiskRejected("Minimum lot size exceeds the risk or margin budget")
    return OrderPlan(side, volume, entry, stop, take, volume*risk)
