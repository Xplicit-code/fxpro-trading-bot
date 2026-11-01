"""Native MT5 adapter. Requires Windows and a local terminal.

An optional password is passed directly to the local terminal API and is never
retained on this adapter. The terminal manages its own login session/storage.
Account-currency calculations and executable quotes come from the broker.
"""
from __future__ import annotations

import importlib.util
import math
import platform
import threading
from datetime import datetime, timezone

from .config import RiskConfig
from .domain import Candle, Position, Quote, Symbol, utc_now
from .journal import Journal
from .market import validate_series
from .risk import Account, OrderPlan

MAGIC = 870031
# MQL5 SYMBOL_ORDER_MODE bit flags. The Python package does not export these
# in every release: MARKET=1, SL=16, TP=32.
REQUIRED_ORDER_MODES = 1 | 16 | 32


class BrokerError(RuntimeError):
    pass


class OrderUncertain(BrokerError):
    """Never automatically retry an order whose outcome may be ambiguous."""


class TradingDisabled(BrokerError):
    """A stopped bot must not submit new automatic orders."""


def load_api():
    if platform.system() != "Windows":
        raise BrokerError("MT5 connection requires Windows and the local MetaTrader 5 terminal. This preview supports paper trading.")
    try:
        import MetaTrader5
    except ImportError:
        raise BrokerError('Install the MT5 extra on Windows: py -m pip install -e ".[mt5]"') from None
    return MetaTrader5


class MT5Broker:
    @staticmethod
    def connection_support() -> dict:
        if platform.system() != "Windows":
            return {"available": False, "message": "Cloud preview: MT5 connection requires this dashboard running locally on Windows with the FxPro MT5 terminal. No credentials are sent from this preview."}
        if importlib.util.find_spec("MetaTrader5") is None:
            return {"available": False, "message": 'Install the MT5 extra on this Windows machine: py -m pip install -e ".[mt5]", then restart the dashboard.'}
        return {"available": True, "message": "Credentials are sent only to the local MT5 terminal. Connecting leaves trading stopped until you press Start Trading."}

    def __init__(self, mode: str, journal: Journal, config: RiskConfig,
                 expected_login: int, expected_server: str, terminal_path: str | None = None,
                 live_enabled: bool = False, api=None, password: str | None = None):
        if mode not in {"mt5-demo", "mt5-live"}:
            raise ValueError("Invalid MT5 mode")
        if mode == "mt5-live" and not live_enabled:
            raise BrokerError("Live trading requires --enable-live and --confirm-live I_ACCEPT_REAL_MONEY_RISK")
        if api is None:
            api = load_api()
        self.api, self.mode, self.journal, self.config = api, mode, journal, config
        self.expected_login, self.expected_server = expected_login, expected_server
        self.trading_enabled = threading.Event()  # Never armed by login/initialization.
        self.authorization_lock = threading.Lock()
        try:
            initialized = api.initialize(terminal_path, timeout=10000) if terminal_path else api.initialize(timeout=10000)
            if not initialized:
                raise BrokerError("MT5 terminal could not initialize. Open your local FxPro MT5 terminal.")
            if password is not None:
                if not api.login(expected_login, password=password, server=expected_server, timeout=10000):
                    raise BrokerError("MT5 sign-in failed. Check your account login, password, server, and terminal settings.")
            self.verify_account()
        except Exception as error:
            # SDK exception text can contain credential arguments. Never expose it.
            try:
                api.shutdown()
            except Exception:
                pass
            if isinstance(error, BrokerError):
                raise error from None
            raise BrokerError("MT5 connection failed. Check your account login, password, server, and terminal settings.") from None
        finally:
            password = None

    def verify_account(self):
        account, terminal = self.api.account_info(), self.api.terminal_info()
        if account is None or terminal is None or not terminal.connected:
            raise BrokerError("MT5 terminal is disconnected")
        if account.login != self.expected_login or account.server != self.expected_server:
            raise BrokerError("MT5 account/server differs from the explicitly selected account")
        if "fxpro" not in (str(account.company) + " " + str(account.server)).lower():
            raise BrokerError("The selected terminal account is not identified as FxPro")
        expected = self.api.ACCOUNT_TRADE_MODE_DEMO if self.mode == "mt5-demo" else self.api.ACCOUNT_TRADE_MODE_REAL
        if account.trade_mode != expected:
            raise BrokerError("Account type does not match the requested demo/live mode")
        if not account.trade_allowed or not account.trade_expert or not terminal.trade_allowed or terminal.tradeapi_disabled:
            raise BrokerError("Enable algorithmic trading and the external Python trading API in MT5")
        return account

    @property
    def account_key(self) -> str:
        return f"{self.expected_server}:{self.expected_login}"

    def symbol(self, name: str) -> Symbol:
        info = self.api.symbol_info(name)
        if info is None or not self.api.symbol_select(name, True):
            raise BrokerError("Symbol is unavailable. Use its exact name, including any broker suffix.")
        if info.trade_mode != self.api.SYMBOL_TRADE_MODE_FULL:
            raise BrokerError("Symbol must allow both buying and selling")
        if info.order_mode & REQUIRED_ORDER_MODES != REQUIRED_ORDER_MODES:
            raise BrokerError("This symbol does not support market orders with stop-loss and take-profit")
        if info.order_gtc_mode != self.api.SYMBOL_ORDERS_GTC:
            raise BrokerError("This symbol can expire protective orders at the end of the day; persistent stops are required")
        if not math.isfinite(info.trade_tick_size) or info.trade_tick_size <= 0:
            raise BrokerError("Broker returned an invalid contract tick size")
        a = self.verify_account()
        return Symbol(name, info.trade_tick_size, info.trade_tick_value_loss or info.trade_tick_value,
                      info.volume_min, info.volume_max, info.volume_step, info.spread*info.point/info.trade_tick_size,
                      info.digits, a.currency, margin_per_lot=1,
                      min_stop_distance=info.trade_stops_level*info.point)

    def symbols(self) -> list[str]:
        self.verify_account()
        result = self.api.symbols_get()
        if result is None:
            raise BrokerError("Could not read broker symbols")
        return sorted(s.name for s in result if s.trade_mode == self.api.SYMBOL_TRADE_MODE_FULL)

    def bars(self, symbol: str, count: int = 720) -> list[Candle]:
        self.verify_account()
        # Position 0 is still forming. Position 1 is the last closed M5 bar.
        rates = self.api.copy_rates_from_pos(symbol, self.api.TIMEFRAME_M5, 1, count)
        if rates is None:
            raise BrokerError("MT5 could not load closed M5 candles")
        bars = [Candle(datetime.fromtimestamp(int(r['time']), timezone.utc), float(r['open']), float(r['high']), float(r['low']), float(r['close']), float(r['tick_volume'])) for r in rates]
        try:
            return validate_series(bars)
        except ValueError as error:
            raise BrokerError(str(error)) from error

    def quote(self, symbol: str) -> Quote:
        self.verify_account()
        tick = self.api.symbol_info_tick(symbol)
        if tick is None:
            raise BrokerError("No executable broker quote is available")
        return Quote(float(tick.bid), float(tick.ask), datetime.fromtimestamp(tick.time_msc/1000, timezone.utc))

    def all_positions(self):
        result = self.api.positions_get()
        if result is None:
            raise BrokerError("Could not reconcile open positions; trading is blocked")
        return list(result)

    def own_positions(self) -> list[Position]:
        return [Position(str(p.ticket), p.symbol, "buy" if p.type == self.api.POSITION_TYPE_BUY else "sell", p.volume, p.price_open, p.sl, p.tp, datetime.fromtimestamp(p.time, timezone.utc).isoformat()) for p in self.all_positions() if p.magic == MAGIC]

    def account(self) -> Account:
        a = self.verify_account()
        key = self.account_key
        today = utc_now().date().isoformat()
        # Baselines survive process restarts. They are account-wide, so manual
        # trades and deposits affect these conservative equity limits too.
        baseline_key = f"daily:{key}:{today}"
        baseline = self.journal.get(baseline_key)
        if baseline is None:
            baseline = a.equity
            self.journal.set(baseline_key, baseline)
        peak_key = f"peak:{key}"
        peak = max(self.journal.get(peak_key, a.equity), a.equity)
        self.journal.set(peak_key, peak)
        return Account(a.balance, a.equity, a.margin_free, baseline, peak, len(self.all_positions()))

    def loss_per_lot(self, symbol: str, side: str, entry: float, stop: float) -> float:
        action = self.api.ORDER_TYPE_BUY if side == "buy" else self.api.ORDER_TYPE_SELL
        reference_volume = self.api.symbol_info(symbol).volume_min
        result = self.api.order_calc_profit(action, symbol, reference_volume, entry, stop)
        if result is None or not math.isfinite(result) or result >= 0:
            raise BrokerError("Broker could not calculate stop-loss exposure in account currency")
        return abs(result)/reference_volume

    def margin_per_lot(self, symbol: str, side: str, price: float) -> float:
        action = self.api.ORDER_TYPE_BUY if side == "buy" else self.api.ORDER_TYPE_SELL
        reference_volume = self.api.symbol_info(symbol).volume_min
        value = self.api.order_calc_margin(action, symbol, reference_volume, price)
        if value is None:
            raise BrokerError("Broker could not calculate required margin")
        return float(value)/reference_volume

    def fill_policy(self, symbol: str):
        info = self.api.symbol_info(symbol)
        if info is None:
            raise BrokerError("Symbol metadata is unavailable")
        # Symbol filling_mode is a bitmask; order filling constants are enums.
        if info.filling_mode & 1:
            return self.api.ORDER_FILLING_FOK
        if info.filling_mode & 2:
            return self.api.ORDER_FILLING_IOC
        # RETURN is explicitly forbidden for Market Execution.
        if info.trade_exemode != self.api.SYMBOL_TRADE_EXECUTION_MARKET:
            return self.api.ORDER_FILLING_RETURN
        raise BrokerError("No supported fill policy is available")

    def _require_trading(self, request: dict, allow_stopped: bool = False):
        if not self.trading_enabled.is_set() and not (allow_stopped and "position" in request):
            raise TradingDisabled("Trading is stopped. Press Start Trading to enable automatic orders.")

    def send(self, request: dict, *, allow_stopped: bool = False):
        self._require_trading(request, allow_stopped)
        self.verify_account()
        checked = self.api.order_check(request)
        if checked is None or checked.retcode != 0:
            raise BrokerError("Broker rejected the order preflight; no order was submitted")
        # Stop can revoke authorization while broker preflight is in progress.
        with self.authorization_lock:
            self._require_trading(request, allow_stopped)
            result = self.api.order_send(request)
        if result is None:
            raise OrderUncertain("No order response. Reconcile MT5 orders/deals before clearing the trading halt; do not retry.")
        if result.retcode not in {self.api.TRADE_RETCODE_DONE, self.api.TRADE_RETCODE_DONE_PARTIAL}:
            raise OrderUncertain(f"Order not confirmed filled (MT5 code {result.retcode}). Reconcile terminal orders/deals before resuming.")
        return result

    def open(self, symbol: str, plan: OrderPlan):
        self._require_trading({})
        self.verify_account()
        # Never merge a trade into another strategy/manual netting position.
        if self.all_positions():
            raise BrokerError("An account position already exists; opening a new position is blocked")
        pending = self.api.orders_get()
        if pending is None or len(pending):
            raise BrokerError("Outstanding or unreadable pending orders block new entries")
        request = {
            "action": self.api.TRADE_ACTION_DEAL, "symbol": symbol, "volume": plan.volume,
            "type": self.api.ORDER_TYPE_BUY if plan.side == "buy" else self.api.ORDER_TYPE_SELL,
            "price": plan.entry, "sl": plan.stop, "tp": plan.take,
            "deviation": 10, "magic": MAGIC, "comment": "FxPro AI guarded entry",
            "type_time": self.api.ORDER_TIME_GTC, "type_filling": self.fill_policy(symbol),
        }
        result = self.send(request)
        # An accepted order is not sufficient: verify attached protections.
        positions = self.own_positions()
        matching = [p for p in positions if p.symbol == symbol and p.side == plan.side]
        if not matching:
            raise OrderUncertain("Fill response received but no position is visible. Reconcile terminal deals before resuming.")
        tolerance = self.symbol(symbol).tick_size*1.01
        for p in matching:
            protected = p.stop > 0 and p.take > 0 and abs(p.stop-plan.stop) <= tolerance and abs(p.take-plan.take) <= tolerance and p.volume <= plan.volume+1e-8
            if protected:
                actual_risk = (self.loss_per_lot(symbol, p.side, p.entry, p.stop) + self.config.commission_per_lot + self.config.slippage_ticks*self.symbol(symbol).tick_value)*p.volume
                protected = actual_risk <= plan.cash_risk + 0.01
            if not protected:
                try:
                    self.close_position(p, "Protection check", allow_stopped=True)
                finally:
                    raise OrderUncertain("Position protection/size differed from the plan. Emergency close attempted; verify the terminal.")
        return result

    def close_position(self, position: Position, reason: str, *, allow_stopped: bool = False):
        self.verify_account()
        current = [p for p in self.own_positions() if p.ticket == position.ticket]
        if not current:
            return
        p = current[0]
        quote = self.quote(p.symbol)
        age = (utc_now()-quote.time).total_seconds()
        if age > self.config.max_quote_age_seconds or age < -5:
            raise BrokerError("Cannot submit a close with a stale quote; inspect MT5")
        request = {
            "action": self.api.TRADE_ACTION_DEAL, "position": int(p.ticket), "symbol": p.symbol,
            "volume": p.volume, "type": self.api.ORDER_TYPE_SELL if p.side == "buy" else self.api.ORDER_TYPE_BUY,
            "price": quote.bid if p.side == "buy" else quote.ask, "deviation": 10,
            "magic": MAGIC, "comment": reason[:30], "type_time": self.api.ORDER_TIME_GTC,
            "type_filling": self.fill_policy(p.symbol),
        }
        self.send(request, allow_stopped=allow_stopped)
        if any(item.ticket == p.ticket for item in self.own_positions()):
            raise OrderUncertain("Close was partial or position remains visible. Reconcile MT5 before resuming.")

    def close_all(self, reason: str, *, allow_stopped: bool = False):
        errors = []
        for position in self.own_positions():
            try:
                self.close_position(position, reason, allow_stopped=allow_stopped)
            except BrokerError as error:
                errors.append(str(error))
        if errors:
            raise BrokerError("; ".join(errors))

    def shutdown(self):
        self.trading_enabled.clear()
        self.api.shutdown()
