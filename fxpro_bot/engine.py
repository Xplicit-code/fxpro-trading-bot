from __future__ import annotations

import threading
from dataclasses import asdict, replace
from datetime import datetime, timedelta

from .backtest import Replay, backtest
from .config import RiskConfig, policy_key
from .domain import Candle, Symbol, utc_now
from .journal import Journal
from .market import DEMO_SYMBOLS, demo_candles
from .model import SignalModel, atr
from .mt5 import BrokerError, MT5Broker, TradingDisabled
from .risk import RiskRejected, account_guard, plan_order


class Engine:
    def __init__(self, journal: Journal, config: RiskConfig, symbol: str = "EURUSD",
                 bars: list[Candle] | None = None, spec: Symbol | None = None,
                 broker: MT5Broker | None = None, interval: float = 1.0,
                 allow_live_connections: bool = False, terminal_path: str | None = None):
        self.journal, self.config, self.broker = journal, config, broker
        self.interval = interval
        self.lock = threading.RLock()
        self._control_lock = threading.Lock()
        self._stop_generation = 0
        self.trading_enabled = threading.Event()
        self.allow_live_connections = allow_live_connections
        self.terminal_path = terminal_path
        self._paper_state: dict | None = None
        self._paper_interval = interval
        if broker:
            broker.trading_enabled = self.trading_enabled
            broker.authorization_lock = self._control_lock
        self.closed = threading.Event()
        self.running = False
        self.reason = "Trading is stopped. Press Start Trading to enable automatic execution."
        self.error: str | None = None
        self.source = "mt5" if broker else "csv" if bars else "synthetic"
        self.symbol_name = symbol
        self.model = SignalModel()
        self.live_score: float | None = None
        self.live_curve: list[dict] = []
        self.last_bar: str | None = None
        if broker:
            self.spec = broker.symbol(symbol)
            self.bars = broker.bars(symbol)
            self.symbol_names = broker.symbols()
            self.replay = None
            broker.account()  # Persist initial risk baselines before any start.
        else:
            self.spec = spec or DEMO_SYMBOLS[symbol]
            self.bars = bars or demo_candles(symbol)
            self.symbol_names = [symbol] if bars else list(DEMO_SYMBOLS)
            self.replay = Replay(self.bars, self.spec, config)
        self.policy_key = policy_key(self.mode, broker.expected_server, broker.expected_login) if broker else policy_key("paper")
        self.journal.set(self.policy_key, asdict(config))
        self.journal.event("info", f"Initialized {self.mode} for {symbol}; data source: {self.source}")
        self.thread = threading.Thread(target=self._loop, name="trading-engine", daemon=True)
        self.thread.start()

    @property
    def mode(self):
        return self.broker.mode if self.broker else "paper"

    def start(self):
        with self._control_lock:
            generation = self._stop_generation
        with self.lock:
            if self.closed.is_set():
                raise ValueError("The trading process is shutting down")
            if self.broker:
                self.broker.verify_account()
                halt = self.journal.get(f"halt:{self.broker.account_key}")
                if halt:
                    raise ValueError(f"Trading is latched off: {halt}. Inspect MT5, then use the documented CLI halt-clear command.")
                account_guard(self.broker.account(), self.config)
            elif self.replay.finished or self.replay.halted:
                raise ValueError("Replay ended or hit its drawdown limit. Reset the paper account to start a new run.")
            with self._control_lock:
                if generation != self._stop_generation:
                    raise ValueError("Start cancelled because Stop Trading was requested")
                self.trading_enabled.set()
                self.running, self.error = True, None
            self.reason = "Automatic execution enabled"
            self.journal.event("start", self.reason)

    def _revoke_trading(self):
        with self._control_lock:
            self._stop_generation += 1
            self.trading_enabled.clear()

    def stop(self):
        # Revoke immediately, before waiting for model/broker work holding lock.
        self._revoke_trading()
        with self.lock:
            self.running = False
            self.reason = "Trading stopped. Automatic bot orders are disabled; broker SL/TP remain active." if self.broker else "Trading stopped. The paper replay is paused."
            self.journal.event("stop", self.reason)

    def pause(self):
        self.stop()

    def emergency_stop(self):
        self._revoke_trading()
        with self.lock:
            self.running = False
            if self.broker:
                self.journal.set(f"halt:{self.broker.account_key}", "Emergency stop requested")
                try:
                    self.broker.close_all("Emergency stop", allow_stopped=True)
                except BrokerError as error:
                    self.error = str(error)
                    self.reason = "Emergency stop paused entries; closing was not confirmed. Inspect MT5 immediately."
                    self.journal.event("error", self.reason)
                    raise
            else:
                p = self.replay.broker.position
                if p:
                    bar = self.bars[self.replay.index-1]
                    quote = self.replay.broker.quote(bar.close, bar.time)
                    trade = self.replay.broker.close(quote.bid if p.side == "buy" else quote.ask, bar.time, "emergency_stop")
                    self.replay.curve[-1]["equity"] = self.replay.broker.account(quote).equity
                    self.journal.event("close", f"Paper emergency close: {trade.pnl:.2f} {self.spec.currency}")
                self.replay.halted = True
            self.reason = "Emergency stop complete. Automatic entries are disabled."
            self.journal.event("stop", self.reason)

    def connection_status(self) -> dict:
        return {
            **MT5Broker.connection_support(),
            "attached": self.broker is not None,
            "live_allowed": self.allow_live_connections,
            "login": str(self.broker.expected_login) if self.broker else None,
            "server": self.broker.expected_server if self.broker else None,
        }

    def connect_mt5(self, payload: dict):
        """Use credentials for one native login request; never journal/echo them."""
        password = None
        candidate = None
        try:
            # Consume the request dictionary so passwords cannot escape through
            # later generic error handling or saved configuration.
            data = dict(payload)
            payload.clear()
            password = data.pop("password", None)
            support = MT5Broker.connection_support()
            if not support["available"]:
                raise BrokerError(support["message"])
            if set(data)-{"login", "server", "mode", "symbol"}:
                raise ValueError("Unsupported connection field")
            login = data.get("login")
            if not isinstance(login, str) or not 1 <= len(login) <= 16 or not login.isascii() or not login.isdigit() or not 0 < int(login) <= 2**53-1:
                raise ValueError("Enter a valid numeric MT5 account login")
            if not isinstance(password, str) or not 1 <= len(password) <= 1024:
                raise ValueError("Enter your MT5 trading password")
            server = data.get("server")
            if not isinstance(server, str) or not 1 <= len(server.strip()) <= 128 or any(ord(c) < 32 for c in server):
                raise ValueError("Enter the exact FxPro MT5 server name")
            server = server.strip()
            mode = data.get("mode", "mt5-demo")
            if mode not in {"mt5-demo", "mt5-live"}:
                raise ValueError("Choose an MT5 demo or live account")
            if mode == "mt5-live" and not self.allow_live_connections:
                raise ValueError("Live connection is locked. Restart the local server with both documented live-trading flags to enable it.")
            symbol = data.get("symbol", self.symbol_name)
            if not isinstance(symbol, str) or not 1 <= len(symbol.strip()) <= 64 or any(ord(c) < 32 for c in symbol):
                raise ValueError("Enter the exact broker instrument name")
            symbol = symbol.strip()
            with self.lock:
                if self.running or self.trading_enabled.is_set():
                    raise ValueError("Press Stop Trading before connecting an account")
                if self.broker:
                    raise ValueError("Disconnect the current MT5 account before connecting another")
                if self.replay.broker.position:
                    raise ValueError("Use Emergency Close to close the paper position before connecting MT5")
                risk = RiskConfig(**self.journal.get(policy_key(mode, server, int(login)), asdict(self.config)))
                try:
                    candidate = MT5Broker(mode, self.journal, risk, int(login), server,
                                          terminal_path=self.terminal_path,
                                          live_enabled=self.allow_live_connections, password=password)
                    # Initialize everything before replacing the working paper engine.
                    spec = candidate.symbol(symbol)
                    bars = candidate.bars(symbol)
                    symbols = candidate.symbols()
                    candidate.account()
                except Exception:
                    if candidate:
                        try:
                            candidate.shutdown()
                        except Exception:
                            pass
                    raise BrokerError("MT5 connection failed. Check your login, password, exact server, instrument, account type, and terminal settings.") from None
                self._paper_state = {name: getattr(self, name) for name in ["config", "source", "spec", "bars", "symbol_name", "symbol_names", "replay", "policy_key", "interval"]}
                self._revoke_trading()
                candidate.trading_enabled = self.trading_enabled
                candidate.authorization_lock = self._control_lock
                self.broker, self.config, self.spec, self.bars = candidate, risk, spec, bars
                self.source, self.symbol_name, self.symbol_names = "mt5", symbol, symbols
                self.replay, self.interval = None, max(5, self.interval)
                self.policy_key = policy_key(mode, server, int(login))
                self.model, self.last_bar, self.live_score, self.live_curve = SignalModel(), None, None, []
                self.running, self.error = False, None
                self.reason = "MT5 connected. Trading is stopped until you press Start Trading."
                self.journal.event("connection", "MT5 account connected; automatic trading remains stopped")
        finally:
            password = None

    def disconnect_mt5(self):
        self._revoke_trading()
        with self.lock:
            self.running = False
            if not self.broker:
                raise ValueError("No MT5 account is connected")
            if self.broker.own_positions():
                raise ValueError("Close the bot's MT5 positions with Emergency Close before disconnecting")
            self.broker.shutdown()
            self.broker = None
            if self._paper_state:
                for name, value in self._paper_state.items():
                    setattr(self, name, value)
                self._paper_state = None
            else:
                self.config = RiskConfig(**self.journal.get(policy_key("paper"), {}))
                self.symbol_name, self.source = "EURUSD", "synthetic"
                self.spec, self.bars = DEMO_SYMBOLS["EURUSD"], demo_candles("EURUSD")
                self.symbol_names = list(DEMO_SYMBOLS)
                self.policy_key, self.interval = policy_key("paper"), self._paper_interval
                self.replay = Replay(self.bars, self.spec, self.config)
            self.error = None
            self.reason = "MT5 disconnected. Paper trading is stopped."
            self.journal.event("connection", self.reason)

    def reset_paper(self):
        with self.lock:
            if self.broker:
                raise ValueError("Broker accounts cannot be reset")
            if self.running or self.replay.broker.position:
                raise ValueError("Pause and close any paper position before resetting")
            self.replay = Replay(self.bars, self.spec, self.config)
            self.reason, self.error = "Paper account reset to 10,000. No real funds are used.", None
            self.journal.event("reset", self.reason)

    def configure(self, payload: dict):
        with self.lock:
            if self.running:
                raise ValueError("Pause trading before changing settings")
            allowed = {"symbol", "risk_fraction", "daily_loss_fraction", "threshold", "stop_atr", "reward_risk", "commission_per_lot", "slippage_ticks"}
            if not payload or set(payload)-allowed:
                raise ValueError("Unsupported configuration field")
            risk = replace(self.config, **{k: v for k, v in payload.items() if k != "symbol"})
            symbol = payload.get("symbol", self.symbol_name)
            if symbol not in self.symbol_names:
                raise ValueError("Select an available symbol")
            if self.broker:
                if self.broker.own_positions():
                    raise ValueError("Close existing bot positions before changing configuration")
                spec = self.broker.symbol(symbol)
                bars = self.broker.bars(symbol)
                self.broker.config = risk
            else:
                if self.replay.broker.position:
                    raise ValueError("Close the current paper position before changing configuration")
                if self.replay.index != self.replay.start:
                    raise ValueError("Reset the paper account before changing configuration")
                spec = DEMO_SYMBOLS[symbol] if self.source == "synthetic" else self.spec
                bars = demo_candles(symbol) if self.source == "synthetic" else self.bars
            self.spec, self.bars, self.symbol_name, self.config = spec, bars, symbol, risk
            self.journal.set(self.policy_key, asdict(risk))
            self.model, self.last_bar, self.live_score = SignalModel(), None, None
            if not self.broker:
                self.replay = Replay(bars, spec, risk)
            self.reason = "Settings updated"
            self.journal.event("config", f"Updated settings for {symbol}")

    def run_backtest(self):
        with self.lock:
            if self.broker:
                bars = self.broker.bars(self.symbol_name)
            else:
                bars = list(self.bars)
            spec, config, source = self.spec, self.config, self.source
        result = backtest(bars, spec, config)
        result["source"] = source
        result["limitations"] = "Synthetic demonstration only; not evidence of real-market performance." if source == "synthetic" else "Walk-forward historical simulation with fixed spread and estimated costs; no swaps, variable spreads, latency, or market impact. Live results can differ."
        self.journal.event("backtest", f"Evaluated {result['evaluation_bars']} bars, {result['metrics']['trade_count']} closed trades")
        return result

    def _mt5_step(self):
        if not self.trading_enabled.is_set() or not self.running or self.closed.is_set():
            return
        broker = self.broker
        if self.journal.get(f"halt:{broker.account_key}"):
            raise BrokerError("A persistent trading halt is active")
        account = broker.account()
        try:
            account_guard(account, self.config)
        except RiskRejected:
            # Latch BEFORE attempting closure, including if the close fails.
            self.journal.set(f"halt:{broker.account_key}", "Account risk limit reached")
            broker.close_all("Account risk limit")
            raise
        self.live_curve.append({"time": utc_now().isoformat(), "equity": account.equity})
        self.live_curve = self.live_curve[-1000:]
        positions = broker.own_positions()
        for position in positions:
            if not position.stop or not position.take:
                self.journal.set(f"halt:{broker.account_key}", "Unprotected bot position detected")
                broker.close_all("Missing protection")
                raise BrokerError("Unprotected bot position found; emergency close attempted")
            if utc_now()-datetime.fromisoformat(position.opened_at) >= timedelta(minutes=5*self.config.max_holding_bars):
                broker.close_position(position, "Maximum holding time")
        bars = broker.bars(self.symbol_name)
        last = bars[-1].time.isoformat()
        self.bars = bars
        if last == self.last_bar:
            self.reason = "Waiting for the next closed M5 candle; monitoring account risk"
            return
        self.last_bar = last
        # MT5 bar timestamps mark OPEN; their close is five minutes later.
        age = (utc_now()-(bars[-1].time+timedelta(minutes=5))).total_seconds()
        if age > self.config.max_bar_age_seconds or age < -5:
            raise BrokerError("Latest closed M5 candle is stale or in the future; market may be closed")
        self.model.fit(bars)
        if not self.trading_enabled.is_set():
            return
        score = self.model.predict(bars)
        self.live_score = score
        quote = broker.quote(self.symbol_name)
        self.spec = broker.symbol(self.symbol_name)  # Refresh contract/tick values.
        try:
            plan = plan_order(self.spec, quote, atr(bars), score, broker.account(), self.config, utc_now(),
                              loss_per_lot=lambda side, entry, stop: broker.loss_per_lot(self.symbol_name, side, entry, stop),
                              margin_per_lot=lambda side, entry: broker.margin_per_lot(self.symbol_name, side, entry))
        except RiskRejected as error:
            self.reason = str(error)
            return
        if not self.journal.reserve(broker.account_key, self.symbol_name, last):
            self.reason = "This signal was already processed; no duplicate order submitted"
            return
        try:
            broker.open(self.symbol_name, plan)
        except TradingDisabled:
            self.journal.finish(broker.account_key, self.symbol_name, last, "cancelled", "Trading stopped before submission")
            raise
        except Exception:
            self.journal.finish(broker.account_key, self.symbol_name, last, "unconfirmed", "Inspect MT5; never retry automatically")
            raise
        self.journal.finish(broker.account_key, self.symbol_name, last, "filled", plan.side)
        self.reason = f"Broker confirmed {plan.side.upper()} with stop-loss and take-profit"
        self.journal.event("order", f"{self.symbol_name} {plan.side} {plan.volume} lots; budget {plan.cash_risk:.2f} {self.spec.currency}")

    def _loop(self):
        while not self.closed.wait(self.interval):
            with self.lock:
                if not self.running or not self.trading_enabled.is_set():
                    continue
                try:
                    if self.broker:
                        self._mt5_step()
                    else:
                        before = len(self.replay.broker.trades)
                        result = self.replay.step()
                        self.reason = self.replay.last_reason
                        if len(self.replay.broker.trades) > before:
                            trade = self.replay.broker.trades[-1]
                            self.journal.event("close", f"{trade.side.upper()} closed: {trade.pnl:.2f} {self.spec.currency} ({trade.reason})")
                        if result["finished"] or self.replay.halted:
                            self._revoke_trading()
                            self.running = False
                            self.reason = "Replay complete" if result["finished"] else "Drawdown limit reached; paper execution halted"
                except TradingDisabled:
                    self._revoke_trading()
                    self.running = False
                    self.reason = "Trading stopped before order submission. No automatic retry."
                except Exception as error:
                    self._revoke_trading()
                    self.running, self.error = False, str(error)
                    self.reason = "Trading halted after an error. Review the activity log and terminal."
                    if self.broker:
                        self.journal.set(f"halt:{self.broker.account_key}", str(error))
                    self.journal.event("error", str(error))

    def status(self):
        with self.lock:
            if self.broker:
                try:
                    account = self.broker.account()
                    positions = [asdict(p) for p in self.broker.own_positions()]
                    connected = True
                    data_error = None
                except (BrokerError, ValueError) as error:
                    account, positions, connected, data_error = None, [], False, str(error)
                account_data = asdict(account) if account else None
                result_metrics = {"net_pnl": round(account.equity-account.day_start_equity, 2) if account else None,
                                  "return_pct": None, "win_rate": None, "trade_count": None,
                                  "max_drawdown_pct": round((1-account.equity/account.peak_equity)*100, 3) if account else None,
                                  "profit_factor": None}
                curve, trades = self.live_curve, []
                score = self.live_score
                bars = self.bars
                model = {"samples": self.model.samples, "trained_through": self.model.trained_through}
                progress = None
                halted = bool(self.journal.get(f"halt:{self.broker.account_key}"))
            else:
                result = self.replay.result()
                bar = self.bars[self.replay.index-1]
                account_data = asdict(self.replay.broker.account(self.replay.broker.quote(bar.close, bar.time)))
                positions = [asdict(self.replay.broker.position)] if self.replay.broker.position else []
                result_metrics, curve, trades = result["metrics"], result["equity_curve"], result["trades"]
                score = self.replay.score
                bars = self.bars[:self.replay.index]
                model = result["model"]
                connected, data_error, halted = True, None, self.replay.halted
                progress = {"completed": self.replay.index-self.replay.start, "total": len(self.bars)-self.replay.start, "finished": self.replay.finished}
            return {
                "mode": self.mode, "source": self.source, "symbol": self.symbol_name, "symbols": self.symbol_names,
                "spec": asdict(self.spec), "running": self.running and self.trading_enabled.is_set(), "halted": halted, "connected": connected,
                "trading_enabled": self.trading_enabled.is_set(), "mt5_connection": self.connection_status(),
                "reason": self.reason, "error": self.error or data_error, "currency": self.spec.currency,
                "account": account_data, "metrics": result_metrics, "positions": positions, "trades": trades[-100:],
                "equity_curve": curve, "candles": [{"time": b.time.isoformat(), "open": b.open, "high": b.high, "low": b.low, "close": b.close} for b in bars[-120:]],
                "score": score, "model": model, "config": asdict(self.config), "progress": progress,
                "events": self.journal.events(), "server_time": utc_now().isoformat(),
            }

    def shutdown(self):
        self._revoke_trading()
        self.running = False
        self.closed.set()
        self.thread.join(timeout=15)
        if self.thread.is_alive():
            # The journal/API must stay alive while an in-flight order completes.
            raise RuntimeError("Trading worker is still finishing a broker request. Inspect MT5 before restarting.")
        if self.broker:
            self.broker.shutdown()
