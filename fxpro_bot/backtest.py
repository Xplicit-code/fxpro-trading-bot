from __future__ import annotations

from dataclasses import asdict
from statistics import fmean

from .config import RiskConfig
from .domain import Candle, Symbol
from .market import validate_series
from .model import LOOKBACK, MIN_TRAIN, SignalModel, atr
from .paper import PaperBroker
from .risk import RiskRejected, account_guard, plan_order


def metrics(trades, initial: float, equity: float, curve: list[float]) -> dict:
    wins = sum(t.pnl > 0 for t in trades)
    gross_profit = sum(max(t.pnl, 0) for t in trades)
    gross_loss = -sum(min(t.pnl, 0) for t in trades)
    peak, drawdown = initial, 0.0
    for value in curve:
        peak = max(peak, value)
        drawdown = max(drawdown, (peak-value)/peak)
    return {
        "net_pnl": round(equity-initial, 2), "return_pct": round((equity/initial-1)*100, 3),
        "win_rate": round(wins/len(trades)*100, 2) if trades else None,
        "trade_count": len(trades), "wins": wins, "losses": sum(t.pnl < 0 for t in trades),
        "breakeven": sum(t.pnl == 0 for t in trades),
        "profit_factor": round(gross_profit/gross_loss, 3) if gross_loss > 0 else None,
        "max_drawdown_pct": round(drawdown*100, 3),
        "average_trade": round(fmean(t.pnl for t in trades), 2) if trades else None,
    }


class Replay:
    """Predict at the prior bar's close; execute at the NEXT bar's open."""
    def __init__(self, bars: list[Candle], spec: Symbol, config: RiskConfig, balance: float = 10000, start: int | None = None):
        self.bars = validate_series(bars)
        self.spec, self.config = spec, config
        self.index = start if start is not None else max(MIN_TRAIN+LOOKBACK+2, int(len(bars)*0.6))
        if not MIN_TRAIN+LOOKBACK+1 <= self.index < len(bars):
            raise ValueError("Not enough training or evaluation bars")
        self.start = self.index
        self.broker = PaperBroker(spec, config, balance)
        self.model = SignalModel()
        self.score: float | None = None
        self.last_reason = "Ready to evaluate the next closed candle"
        self.curve = [{"time": bars[self.index-1].time.isoformat(), "equity": balance}]
        self.signals: list[dict] = []
        self.halted = False

    @property
    def finished(self) -> bool:
        return self.index >= len(self.bars)

    def step(self) -> dict:
        if self.finished:
            return {"finished": True}
        bar = self.bars[self.index]
        history = self.bars[:self.index]
        quote = self.broker.quote(bar.open, bar.time)
        # Retrain chronologically, using past labels only.
        if not self.model.trained_through or (self.index-self.start) % 24 == 0:
            self.model.fit(history)
        self.score = self.model.predict(history)
        account = self.broker.account(quote)
        try:
            account_guard(account, self.config)
        except RiskRejected as error:
            self.last_reason = str(error)
            if self.broker.position:
                p = self.broker.position
                self.broker.close(quote.bid if p.side == "buy" else quote.ask, bar.time, "risk_limit")
            if "drawdown" in self.last_reason.lower():
                self.halted = True
        if not self.broker.position and not self.halted:
            try:
                plan = plan_order(self.spec, quote, atr(history), self.score, self.broker.account(quote), self.config, bar.time)
                self.broker.open(plan, quote)
                self.last_reason = f"Opened {plan.side.upper()} with protective stop and take-profit"
            except RiskRejected as error:
                self.last_reason = str(error)
        elif self.broker.position:
            self.last_reason = "Managing protected position"
        # Intrabar fills happen AFTER the open-time decision, never before it.
        closed = self.broker.process_bar(bar)
        if closed:
            self.last_reason = f"Closed position: {closed.reason}"
        self.index += 1
        end_quote = self.broker.quote(bar.close, bar.time)
        if self.finished and self.broker.position:
            p = self.broker.position
            self.broker.close(end_quote.bid if p.side == "buy" else end_quote.ask, bar.time, "end_of_data")
        equity = self.broker.account(end_quote).equity
        self.curve.append({"time": bar.time.isoformat(), "equity": round(equity, 4)})
        signal = {"time": history[-1].time.isoformat(), "score": self.score, "reason": self.last_reason}
        self.signals.append(signal)
        return {"finished": self.finished, **signal}

    def result(self) -> dict:
        last = self.bars[min(self.index-1, len(self.bars)-1)]
        equity = self.broker.account(self.broker.quote(last.close, last.time)).equity
        return {
            "symbol": self.spec.name, "currency": self.spec.currency,
            "metrics": metrics(self.broker.trades, self.broker.initial_balance, equity, [p["equity"] for p in self.curve]),
            "equity_curve": self.curve, "trades": [t.to_dict() for t in self.broker.trades],
            "training_bars": self.start, "evaluation_bars": self.index-self.start,
            "model": {"name": "Regularized logistic regression", "samples": self.model.samples, "trained_through": self.model.trained_through, "score": self.score, "calibrated": False},
            "config": asdict(self.config), "finished": self.finished,
        }


def backtest(bars: list[Candle], spec: Symbol, config: RiskConfig, balance: float = 10000) -> dict:
    replay = Replay(bars, spec, config, balance)
    while not replay.finished:
        replay.step()
    return replay.result()
