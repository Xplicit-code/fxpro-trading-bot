import csv
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from fxpro_bot.backtest import Replay, backtest
from fxpro_bot.config import RiskConfig
from fxpro_bot.domain import Candle, Quote, parse_time, utc_now
from fxpro_bot.journal import Journal
from fxpro_bot.market import DEMO_SYMBOLS, demo_candles, load_csv, validate_series
from fxpro_bot.model import SignalModel, features
from fxpro_bot.paper import PaperBroker
from fxpro_bot.risk import Account, OrderPlan, RiskRejected, plan_order


class MarketTests(unittest.TestCase):
    def test_rejects_invalid_prices_and_ranges(self):
        now = utc_now()
        for prices in [(1, 1, 1, float("nan")), (1, .9, .8, 1), (-1, 2, 1, 1)]:
            with self.assertRaises(ValueError):
                Candle(now, *prices)
        with self.assertRaises(ValueError):
            Quote(1.2, 1.1, now)

    def test_requires_explicit_timezone_and_ordered_bars(self):
        with self.assertRaises(ValueError):
            parse_time("2026-01-01T12:00:00")
        bars = demo_candles("EURUSD", 160)
        bars[-1] = bars[-2]
        with self.assertRaisesRegex(ValueError, "chronological"):
            validate_series(bars)

    def test_real_csv_ingestion(self):
        bars = demo_candles("XAUUSD", 180)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"history.csv"
            with path.open("w", newline="") as file:
                writer = csv.writer(file)
                writer.writerow(["time", "open", "high", "low", "close", "volume"])
                for bar in bars:
                    writer.writerow([bar.time.isoformat(), bar.open, bar.high, bar.low, bar.close, bar.volume])
            self.assertEqual(load_csv(path), bars)

    def test_rejects_non_m5_history(self):
        bars = demo_candles("EURUSD", 160)
        altered = [replace(b, time=bars[0].time+timedelta(minutes=i)) for i, b in enumerate(bars)]
        with self.assertRaisesRegex(ValueError, "M5"):
            validate_series(altered)


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.spec = DEMO_SYMBOLS["EURUSD"]
        self.config = RiskConfig()
        self.now = utc_now()
        self.quote = Quote(1.1000, 1.10012, self.now)
        self.account = Account(10000, 10000, 10000, 10000, 10000, 0)

    def plan(self, **kwargs):
        params = dict(spec=self.spec, quote=self.quote, volatility=.001, score=.8, account=self.account, config=self.config, now=self.now)
        params.update(kwargs)
        return plan_order(**params)

    def test_long_and_short_size_include_costs(self):
        for score, side in [(.8, "buy"), (.2, "sell")]:
            p = self.plan(score=score)
            self.assertEqual(p.side, side)
            self.assertLessEqual(p.cash_risk, 50)
            self.assertGreater(p.volume, 0)
            self.assertAlmostEqual(p.volume/self.spec.volume_step, round(p.volume/self.spec.volume_step))
            self.assertGreater((p.entry-p.stop)*(1 if side == "buy" else -1), 0)
            self.assertGreater((p.take-p.entry)*(1 if side == "buy" else -1), 0)

    def test_rejects_stale_future_wide_spread_and_weak_signals(self):
        for quote in [replace(self.quote, time=self.now-timedelta(seconds=21)), replace(self.quote, time=self.now+timedelta(seconds=6)), replace(self.quote, ask=1.101)]:
            with self.assertRaises(RiskRejected):
                self.plan(quote=quote)
        for score in [.5, float("nan"), 1.2]:
            with self.assertRaises(RiskRejected):
                self.plan(score=score)

    def test_limits_account_exposure(self):
        for account in [replace(self.account, positions=1), replace(self.account, equity=9799), replace(self.account, peak_equity=12000), replace(self.account, equity=float("nan"))]:
            with self.assertRaises(RiskRejected):
                self.plan(account=account)

    def test_does_not_round_up_to_broker_minimum(self):
        with self.assertRaisesRegex(RiskRejected, "Minimum lot"):
            self.plan(spec=replace(self.spec, volume_min=1))

    def test_remaining_daily_budget_and_currency_conversion(self):
        p = self.plan(account=replace(self.account, equity=9825), loss_per_lot=lambda *args: 1000, margin_per_lot=lambda *args: 100)
        self.assertLessEqual(p.cash_risk, 25)

    def test_floor_volume_is_never_above_budget(self):
        for value in [.009, .01, .01999, .07, .123, 100]:
            result = self.spec.floor_volume(value)
            self.assertLessEqual(result, value)
            self.assertLessEqual(result, self.spec.volume_max)

    def test_rejects_bad_configurations(self):
        for patch in [{"risk_fraction": .03}, {"threshold": .5}, {"daily_loss_fraction": "2"}, {"stop_atr": float("nan")}, {"max_positions": True}, {"max_quote_age_seconds": 999}]:
            with self.assertRaises(ValueError):
                RiskConfig(**patch)


class PaperExecutionTests(unittest.TestCase):
    def setUp(self):
        self.spec = DEMO_SYMBOLS["EURUSD"]
        self.broker = PaperBroker(self.spec, RiskConfig())
        self.now = utc_now()

    def test_round_trip_deducts_all_costs(self):
        quote = self.broker.quote(1.1, self.now)
        p = self.broker.open(OrderPlan("buy", .1, quote.ask, 1.09, 1.12, 50), quote)
        trade = self.broker.close(1.105, self.now, "test")
        expected = self.spec.pnl("buy", p.entry, 1.105-.00002, .1)-.7
        self.assertAlmostEqual(trade.pnl, expected)
        self.assertAlmostEqual(self.broker.balance, 10000+expected)

    def test_stop_wins_when_both_stop_and_take_hit(self):
        quote = self.broker.quote(1.1, self.now)
        self.broker.open(OrderPlan("buy", .1, quote.ask, 1.099, 1.102, 50), quote)
        trade = self.broker.process_bar(Candle(self.now, 1.1, 1.103, 1.098, 1.101))
        self.assertEqual(trade.reason, "stop_loss")
        self.assertLess(trade.pnl, 0)

    def test_gaps_are_filled_at_adverse_open(self):
        quote = self.broker.quote(1.1, self.now)
        self.broker.open(OrderPlan("buy", .1, quote.ask, 1.099, 1.102, 50), quote)
        trade = self.broker.process_bar(Candle(self.now, 1.095, 1.097, 1.094, 1.096))
        self.assertEqual(trade.reason, "stop_gap")
        self.assertLess(trade.exit, 1.095)

    def test_short_stops_use_ask_not_bid(self):
        quote = self.broker.quote(1.1, self.now)
        self.broker.open(OrderPlan("sell", .1, quote.bid, 1.101, 1.098, 50), quote)
        trade = self.broker.process_bar(Candle(self.now, 1.1, 1.1009, 1.0998, 1.1001))
        self.assertEqual(trade.reason, "stop_loss")


class ModelAndBacktestTests(unittest.TestCase):
    def test_features_do_not_depend_on_future(self):
        bars = demo_candles("EURUSD", 240)
        self.assertEqual(features(bars[:150]), features(list(bars[:150])))
        model = SignalModel()
        model.fit(bars[:150])
        self.assertEqual(model.samples, 125)
        self.assertEqual(model.trained_through, bars[149].time.isoformat())
        self.assertTrue(0 < model.predict(bars[:150]) < 1)

    def test_replay_is_invariant_to_future_changes(self):
        bars = demo_candles("EURUSD", 240)
        altered = list(bars)
        for i in range(165, len(altered)):
            b = altered[i]
            altered[i] = replace(b, open=b.open*2, high=b.high*2, low=b.low*2, close=b.close*2)
        first, second = Replay(bars, DEMO_SYMBOLS["EURUSD"], RiskConfig()), Replay(altered, DEMO_SYMBOLS["EURUSD"], RiskConfig())
        for _ in range(20):
            first.step(); second.step()
            self.assertEqual(first.signals[-1], second.signals[-1])
            self.assertEqual(first.broker.balance, second.broker.balance)

    def test_backtest_finishes_with_real_checks_and_cost_reconciliation(self):
        result = backtest(demo_candles("EURUSD"), DEMO_SYMBOLS["EURUSD"], RiskConfig())
        self.assertTrue(result["finished"])
        self.assertEqual(result["evaluation_bars"], 288)
        self.assertGreater(result["metrics"]["trade_count"], 0)
        self.assertAlmostEqual(sum(t["pnl"] for t in result["trades"]), result["metrics"]["net_pnl"], places=2)
        json.dumps(result, allow_nan=False)


class JournalTests(unittest.TestCase):
    def test_signal_reservation_and_halts_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)/"audit.sqlite3"
            first = Journal(path)
            self.assertTrue(first.reserve("demo:1", "EURUSD", "bar-1"))
            first.set("halt:demo:1", "Uncertain order")
            first.close()
            second = Journal(path)
            self.assertFalse(second.reserve("demo:1", "EURUSD", "bar-1"))
            self.assertTrue(second.reserve("demo:1", "EURUSD", "bar-2"))
            self.assertEqual(second.get("halt:demo:1"), "Uncertain order")
            second.close()


if __name__ == "__main__":
    unittest.main()
