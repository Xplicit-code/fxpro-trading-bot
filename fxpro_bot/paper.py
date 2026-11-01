from __future__ import annotations

from .config import RiskConfig
from .domain import Candle, Position, Quote, Symbol, Trade
from .risk import Account, OrderPlan


class PaperBroker:
    def __init__(self, spec: Symbol, config: RiskConfig, balance: float = 10000):
        self.spec, self.config = spec, config
        self.initial_balance = self.balance = balance
        self.peak_equity = self.day_start_equity = balance
        self.day: str | None = None
        self.position: Position | None = None
        self.trades: list[Trade] = []
        self.sequence = 0
        self.held_bars = 0

    def quote(self, bid: float, time) -> Quote:
        return Quote(bid, bid + self.spec.spread_ticks*self.spec.tick_size, time)

    def account(self, quote: Quote) -> Account:
        floating = margin = exit_fee = 0.0
        if self.position:
            p = self.position
            floating = self.spec.pnl(p.side, p.entry, quote.bid if p.side == "buy" else quote.ask, p.volume)
            margin = p.volume * self.spec.margin_per_lot
            exit_fee = p.volume*self.config.commission_per_lot/2
        equity = self.balance + floating - exit_fee
        self.peak_equity = max(self.peak_equity, equity)
        if self.day != quote.time.date().isoformat():
            self.day = quote.time.date().isoformat()
            self.day_start_equity = equity
        return Account(self.balance, equity, max(0, equity-margin), self.day_start_equity, self.peak_equity, int(self.position is not None))

    def open(self, plan: OrderPlan, quote: Quote) -> Position:
        if self.position:
            raise ValueError("A paper position is already open")
        self.sequence += 1
        sign = 1 if plan.side == "buy" else -1
        entry = plan.entry + sign*self.config.slippage_ticks*self.spec.tick_size
        self.position = Position(f"paper-{self.sequence}", self.spec.name, plan.side, plan.volume, entry, plan.stop, plan.take, quote.time.isoformat())
        self.balance -= plan.volume*self.config.commission_per_lot/2
        self.held_bars = 0
        return self.position

    def close(self, price: float, time, reason: str, apply_slippage: bool = True) -> Trade | None:
        if not self.position:
            return None
        p = self.position
        if apply_slippage:
            price += (-1 if p.side == "buy" else 1)*self.config.slippage_ticks*self.spec.tick_size
        gross = self.spec.pnl(p.side, p.entry, price, p.volume)
        self.balance += gross-p.volume*self.config.commission_per_lot/2
        trade = Trade(p.ticket, p.symbol, p.side, p.volume, p.entry, price, p.stop, p.take, p.opened_at, time.isoformat(), gross-p.volume*self.config.commission_per_lot, reason)
        self.trades.append(trade)
        self.position = None
        self.held_bars = 0
        return trade

    def process_bar(self, bar: Candle) -> Trade | None:
        """Bid OHLC; synthesize ask using fixed spread. Gaps fill at open.

        If both stop and take occur in a bar, assume stop hit first. This is
        conservative because OHLC data cannot establish intrabar order.
        """
        if not self.position:
            return None
        p = self.position
        offset = self.spec.spread_ticks*self.spec.tick_size if p.side == "sell" else 0
        opening, high, low = bar.open+offset, bar.high+offset, bar.low+offset
        self.held_bars += 1
        if p.side == "buy":
            if opening <= p.stop:
                return self.close(opening, bar.time, "stop_gap")
            if opening >= p.take:
                return self.close(p.take, bar.time, "take_profit", False)
            if low <= p.stop:
                return self.close(p.stop, bar.time, "stop_loss")
            if high >= p.take:
                return self.close(p.take, bar.time, "take_profit", False)
        else:
            if opening >= p.stop:
                return self.close(opening, bar.time, "stop_gap")
            if opening <= p.take:
                return self.close(p.take, bar.time, "take_profit", False)
            if high >= p.stop:
                return self.close(p.stop, bar.time, "stop_loss")
            if low <= p.take:
                return self.close(p.take, bar.time, "take_profit", False)
        # A time exit is at this bar's CLOSE, after checking both protections.
        if self.held_bars >= self.config.max_holding_bars:
            return self.close(bar.close+offset, bar.time, "time_exit")
        return None
