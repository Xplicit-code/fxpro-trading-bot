import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import patch

from fxpro_bot.config import RiskConfig
from fxpro_bot.domain import utc_now
from fxpro_bot.engine import Engine
from fxpro_bot.journal import Journal
from fxpro_bot.market import demo_candles
from fxpro_bot.mt5 import MAGIC, BrokerError, MT5Broker, OrderUncertain, TradingDisabled
from fxpro_bot.risk import plan_order


class FakeMT5:
    """Terminal contract double; it never connects to a broker."""
    ACCOUNT_TRADE_MODE_DEMO, ACCOUNT_TRADE_MODE_REAL = 0, 2
    SYMBOL_TRADE_MODE_FULL, SYMBOL_TRADE_EXECUTION_MARKET, SYMBOL_ORDERS_GTC = 4, 2, 0
    ORDER_FILLING_FOK, ORDER_FILLING_IOC, ORDER_FILLING_RETURN = 0, 1, 2
    ORDER_TYPE_BUY, ORDER_TYPE_SELL, POSITION_TYPE_BUY = 0, 1, 0
    ORDER_TIME_GTC, TRADE_ACTION_DEAL, TIMEFRAME_M5 = 0, 1, 5
    TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL = 10009, 10010

    def __init__(self):
        self.a = NS(login=123, server="FxPro-MT5 Demo", company="FxPro Financial Services", trade_mode=0, trade_allowed=True, trade_expert=True, balance=10000, equity=10000, margin_free=10000, currency="USD")
        self.terminal = NS(connected=True, trade_allowed=True, tradeapi_disabled=False)
        self.info = NS(name="EURUSD", trade_mode=4, order_mode=49, order_gtc_mode=0, volume_min=.01, volume_max=10, volume_step=.01, trade_tick_size=.00001, trade_tick_value_loss=1, trade_tick_value=1, spread=12, point=.00001, digits=5, trade_stops_level=10, filling_mode=3, trade_exemode=2)
        self.positions, self.pending, self.sent = [], [], []
        self.disconnected = False
        self.send_none = self.no_protection = self.partial_close = False
        self.check_code = 0
        self.login_ok = True
        self.tick = NS(bid=1.1, ask=1.10012, time_msc=int(utc_now().timestamp()*1000))
        source = demo_candles("EURUSD", 240)
        start = utc_now()-timedelta(minutes=5*(len(source)+1))
        self.rates = [dict(time=int((start+timedelta(minutes=5*i)).timestamp()), open=b.open, high=b.high, low=b.low, close=b.close, tick_volume=b.volume) for i, b in enumerate(source)]

    def initialize(self, *args, **kwargs): return True
    def login(self, login, *, password, server, timeout):
        return self.login_ok and login == self.a.login and server == self.a.server and bool(password)
    def shutdown(self): self.disconnected = True
    def account_info(self): return self.a
    def terminal_info(self): return self.terminal
    def symbol_info(self, name): return self.info if name == "EURUSD" else None
    def symbol_select(self, name, selected): return name == "EURUSD"
    def symbols_get(self): return [self.info]
    def positions_get(self): return self.positions
    def orders_get(self): return self.pending
    def symbol_info_tick(self, symbol): return self.tick
    def copy_rates_from_pos(self, symbol, timeframe, start, count):
        assert timeframe == self.TIMEFRAME_M5 and start == 1
        return self.rates[-count:]
    def order_calc_profit(self, action, symbol, volume, entry, exit):
        return (1 if action == 0 else -1)*(exit-entry)/.00001*volume
    def order_calc_margin(self, action, symbol, volume, price): return 3500*volume
    def order_check(self, request): return NS(retcode=self.check_code)
    def order_send(self, request):
        self.sent.append(request)
        if self.send_none:
            return None
        if "position" in request:
            if self.partial_close:
                self.positions[0].volume /= 2
                return NS(retcode=10010)
            self.positions = [p for p in self.positions if p.ticket != request["position"]]
        else:
            self.positions.append(NS(ticket=1, magic=MAGIC, symbol=request['symbol'], type=request['type'], volume=request['volume'], price_open=request['price'], sl=0 if self.no_protection else request['sl'], tp=request['tp'], time=int(utc_now().timestamp())))
        return NS(retcode=10009, order=1)


class MT5Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name)/"journal.sqlite3")
        self.api = FakeMT5()
        self.config = RiskConfig()
        self.broker = self.connect()
        self.broker.trading_enabled.set()  # Low-level execution tests explicitly arm.

    def connect(self, **kw):
        return MT5Broker(kw.pop("mode", "mt5-demo"), self.journal, self.config, kw.pop("expected_login", 123), "FxPro-MT5 Demo", api=self.api, **kw)

    def tearDown(self):
        self.journal.close()
        self.tmp.cleanup()

    def plan(self):
        return plan_order(self.broker.symbol("EURUSD"), self.broker.quote("EURUSD"), .002, .8, self.broker.account(), self.config, utc_now(),
                          loss_per_lot=lambda *args: self.broker.loss_per_lot("EURUSD", *args),
                          margin_per_lot=lambda *args: self.broker.margin_per_lot("EURUSD", *args))

    def test_account_and_live_mode_are_fail_closed(self):
        with self.assertRaises(BrokerError): self.connect(expected_login=999)
        with self.assertRaises(BrokerError): self.connect(mode="mt5-live")
        with self.assertRaises(BrokerError): self.connect(mode="mt5-live", live_enabled=True)
        self.api.a.company = "Another Broker"; self.api.a.server = "Another Server"
        with self.assertRaises(BrokerError): self.broker.verify_account()

    def test_disabled_algo_and_disconnection_block_trading(self):
        self.api.terminal.tradeapi_disabled = True
        with self.assertRaises(BrokerError): self.broker.verify_account()
        self.api.terminal.tradeapi_disabled = False; self.api.terminal.connected = False
        with self.assertRaises(BrokerError): self.broker.verify_account()

    def test_requires_market_orders_and_persistent_protection(self):
        self.api.info.order_mode = 1
        with self.assertRaises(BrokerError): self.broker.symbol("EURUSD")
        self.api.info.order_mode = 49; self.api.info.order_gtc_mode = 2
        with self.assertRaisesRegex(BrokerError, "expire"):
            self.broker.symbol("EURUSD")

    def test_invalid_broker_contract_is_rejected(self):
        self.api.info.trade_tick_size = 0
        with self.assertRaisesRegex(BrokerError, "tick size"):
            self.broker.symbol("EURUSD")

    def test_filling_flags_are_not_order_enums(self):
        self.assertEqual(self.broker.fill_policy("EURUSD"), 0)
        self.api.info.filling_mode = 2
        self.assertEqual(self.broker.fill_policy("EURUSD"), 1)
        self.api.info.filling_mode = 0
        with self.assertRaises(BrokerError): self.broker.fill_policy("EURUSD")

    def test_reads_only_closed_bars_and_broker_currency_values(self):
        self.assertEqual(len(self.broker.bars("EURUSD")), 240)
        self.assertAlmostEqual(self.broker.loss_per_lot("EURUSD", "buy", 1.1, 1.099), 100)
        self.assertEqual(self.broker.margin_per_lot("EURUSD", "buy", 1.1), 3500)

    def test_rejects_preflight_without_sending(self):
        plan = self.plan(); self.api.check_code = 10019
        with self.assertRaises(BrokerError): self.broker.open("EURUSD", plan)
        self.assertEqual(len(self.api.sent), 0)

    def test_protected_entry_and_ticket_specific_close(self):
        self.broker.open("EURUSD", self.plan())
        self.assertEqual(len(self.api.sent), 1)
        request = self.api.sent[0]
        self.assertGreater(request["sl"], 0); self.assertGreater(request["tp"], 0)
        self.broker.close_all("test")
        self.assertEqual(len(self.api.positions), 0)
        self.assertEqual(self.api.sent[-1]["position"], 1)

    def test_existing_manual_position_or_pending_order_blocks_entry(self):
        plan = self.plan()
        self.api.positions = [NS(ticket=22, magic=0)]
        with self.assertRaises(BrokerError): self.broker.open("EURUSD", plan)
        self.api.positions = []; self.api.pending = [NS(ticket=33)]
        with self.assertRaises(BrokerError): self.broker.open("EURUSD", plan)
        self.assertEqual(len(self.api.sent), 0)

    def test_missing_protection_triggers_emergency_close(self):
        self.api.no_protection = True
        with self.assertRaises(OrderUncertain): self.broker.open("EURUSD", self.plan())
        self.assertEqual(len(self.api.positions), 0)
        self.assertEqual(len(self.api.sent), 2)

    def test_missing_response_and_partial_close_are_not_retried(self):
        plan = self.plan(); self.api.send_none = True
        with self.assertRaises(OrderUncertain): self.broker.open("EURUSD", plan)
        self.assertEqual(len(self.api.sent), 1)
        self.api.send_none = False
        self.broker.open("EURUSD", plan)
        self.api.partial_close = True
        with self.assertRaises(BrokerError): self.broker.close_all("test")
        self.assertEqual(len(self.api.sent), 3)

    def test_manual_positions_are_never_closed(self):
        self.api.positions = [NS(ticket=22, magic=0)]
        self.broker.close_all("test")
        self.assertEqual(len(self.api.positions), 1)
        self.assertEqual(len(self.api.sent), 0)

    def test_account_risk_baselines_are_durable(self):
        account = self.broker.account()
        self.api.a.equity = 9900
        second = self.broker.account()
        self.assertEqual(second.day_start_equity, account.day_start_equity)
        self.assertEqual(second.peak_equity, 10000)

    def test_processed_signal_is_not_resubmitted_after_restart(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=3600)
        try:
            engine.start()
            with patch.object(engine.model, "predict", return_value=.8), patch("fxpro_bot.engine.atr", return_value=.002):
                engine._mt5_step()
                self.assertEqual(len(self.api.sent), 1)
                self.api.positions = []  # Position closed by broker SL/TP.
                engine.last_bar = None  # Equivalent to a process restart.
                engine._mt5_step()
                self.assertEqual(len(self.api.sent), 1)
                self.assertIn("duplicate", engine.reason)
        finally:
            engine.shutdown()

    def test_stale_market_blocks_orders(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=3600)
        try:
            engine.start()
            for r in self.api.rates: r['time'] -= 86400
            with self.assertRaisesRegex(BrokerError, "stale"):
                engine._mt5_step()
            self.assertEqual(len(self.api.sent), 0)
        finally:
            engine.shutdown()

    def test_uncertain_order_latches_worker_off_without_retry(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=.05)
        self.api.send_none = True
        try:
            with patch.object(engine.model, "predict", return_value=.8), patch("fxpro_bot.engine.atr", return_value=.002):
                engine.start()
                deadline = time.monotonic()+3
                while engine.running and time.monotonic() < deadline:
                    time.sleep(.02)
                self.assertFalse(engine.running)
                self.assertEqual(len(self.api.sent), 1)
                self.assertIn("No order response", self.journal.get(f"halt:{self.broker.account_key}"))
                with self.assertRaisesRegex(ValueError, "latched off"):
                    engine.start()
                self.assertEqual(len(self.api.sent), 1)
        finally:
            engine.shutdown()

    def test_daily_limit_latches_before_closing_positions(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=3600)
        try:
            engine.start()
            self.broker.open("EURUSD", self.plan())
            self.api.a.equity = 9700
            with self.assertRaisesRegex(ValueError, "Daily loss"):
                engine._mt5_step()
            self.assertEqual(self.api.positions, [])
            self.assertTrue(self.journal.get(f"halt:{self.broker.account_key}"))
        finally:
            engine.shutdown()

    def test_broker_initialization_and_login_do_not_authorize_trading(self):
        candidate = self.connect(password="unit-test-password")
        self.assertFalse(candidate.trading_enabled.is_set())
        with self.assertRaises(TradingDisabled):
            candidate.open("EURUSD", self.plan())
        self.assertEqual(self.api.sent, [])
        self.assertNotIn("password", vars(candidate))

    def test_stop_during_preflight_prevents_order_send(self):
        plan = self.plan()
        def revoke(request):
            self.broker.trading_enabled.clear()
            return NS(retcode=0)
        with patch.object(self.api, "order_check", side_effect=revoke):
            with self.assertRaises(TradingDisabled):
                self.broker.open("EURUSD", plan)
        self.assertEqual(self.api.sent, [])

    def test_engine_step_never_trades_before_start_or_after_stop(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=3600)
        try:
            with patch.object(engine.model, "predict", return_value=.8), patch("fxpro_bot.engine.atr", return_value=.002):
                engine._mt5_step()
                self.assertEqual(self.api.sent, [])
                engine.start()
                engine._mt5_step()
                self.assertEqual(len(self.api.sent), 1)
                engine.stop()
                self.api.positions = []
                engine.last_bar = None
                engine._mt5_step()
                self.assertEqual(len(self.api.sent), 1)
                self.assertFalse(engine.status()["trading_enabled"])
                self.assertIsNone(self.journal.get(f"halt:{self.broker.account_key}"))
        finally:
            engine.shutdown()

    def test_stop_cancels_a_start_still_verifying_the_account(self):
        engine = Engine(self.journal, self.config, broker=self.broker, interval=3600)
        verifying, release, revoked = threading.Event(), threading.Event(), threading.Event()
        errors = []
        verify = self.broker.verify_account
        revoke = engine._revoke_trading
        def slow_verify():
            verifying.set()
            if not release.wait(3):
                raise RuntimeError("Test account verification did not resume")
            return verify()
        def record_revoke():
            revoke()
            revoked.set()
        def start():
            try:
                engine.start()
            except ValueError as error:
                errors.append(str(error))
        start_thread = threading.Thread(target=start, daemon=True)
        stop_thread = threading.Thread(target=engine.stop, daemon=True)
        try:
            with patch.object(self.broker, "verify_account", side_effect=slow_verify), patch.object(engine, "_revoke_trading", side_effect=record_revoke):
                start_thread.start()
                self.assertTrue(verifying.wait(2))
                stop_thread.start()
                self.assertTrue(revoked.wait(2))
                release.set()
                start_thread.join(3); stop_thread.join(3)
            self.assertFalse(start_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(any("cancelled" in message for message in errors))
            self.assertFalse(engine.running)
            self.assertFalse(engine.trading_enabled.is_set())
            self.assertEqual(self.api.sent, [])
        finally:
            release.set()
            engine.shutdown()


if __name__ == "__main__":
    unittest.main()
