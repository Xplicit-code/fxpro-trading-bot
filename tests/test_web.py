import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fxpro_bot.config import RiskConfig
from fxpro_bot.engine import Engine
from fxpro_bot.journal import Journal
from fxpro_bot.mt5 import TradingDisabled
from fxpro_bot.web import make_server
from tests.test_mt5 import FakeMT5


class DashboardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = Journal(Path(self.tmp.name)/"journal.sqlite3")
        self.engine = Engine(self.journal, RiskConfig(), interval=.05)
        self.server = make_server(self.engine, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.token = self.request("GET", "/api/status")[1]["csrf_token"]

    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.thread.join()
        self.engine.shutdown(); self.journal.close(); self.tmp.cleanup()

    def request(self, method, path, payload=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        base_headers = {"Content-Type": "application/json"}
        if hasattr(self, "token"):
            base_headers["X-CSRF-Token"] = self.token
        base_headers.update(headers or {})
        connection.request(method, path, body=json.dumps(payload or {}) if method == "POST" else None, headers=base_headers)
        response = connection.getresponse()
        body = response.read()
        result = json.loads(body) if "application/json" in response.getheader("Content-Type", "") else body
        code = response.status
        connection.close()
        return code, result

    def test_dashboard_and_initial_status(self):
        code, html = self.request("GET", "/")
        self.assertEqual(code, 200)
        self.assertIn(b"Synthetic market simulation", html)
        code, status = self.request("GET", "/api/status")
        self.assertFalse(status["running"])
        self.assertEqual(status["source"], "synthetic")
        self.assertEqual(status["account"]["equity"], 10000)
        self.assertIsNone(status["metrics"]["win_rate"])

    def test_rejects_cross_origin_missing_token_and_dns_rebinding(self):
        code, _ = self.request("POST", "/api/start", headers={"X-CSRF-Token": ""})
        self.assertEqual(code, 403)
        code, _ = self.request("POST", "/api/start", headers={"Origin": "https://attacker.invalid"})
        self.assertEqual(code, 403)
        code, _ = self.request("GET", "/api/status", headers={"Host": "attacker.invalid"})
        self.assertEqual(code, 403)
        self.assertFalse(self.engine.running)

    def test_start_pause_stop_and_reset(self):
        self.assertEqual(self.request("POST", "/api/start")[0], 200)
        deadline = time.monotonic()+3
        while self.engine.replay.index == self.engine.replay.start and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertGreater(self.engine.replay.index, self.engine.replay.start)
        self.assertEqual(self.request("POST", "/api/config", {"risk_fraction": .01})[0], 400)
        self.assertEqual(self.request("POST", "/api/pause")[0], 200)
        self.assertFalse(self.engine.running)
        self.assertEqual(self.request("POST", "/api/stop")[0], 200)
        self.assertFalse(self.engine.running)
        self.assertFalse(self.engine.trading_enabled.is_set())
        self.assertFalse(self.engine.replay.halted)
        self.assertEqual(self.request("POST", "/api/emergency-stop")[0], 200)
        self.assertIsNone(self.engine.replay.broker.position)
        self.assertTrue(self.engine.replay.halted)
        self.assertEqual(self.request("POST", "/api/reset")[0], 200)
        self.assertEqual(self.engine.replay.index, self.engine.replay.start)
        self.assertEqual(self.engine.replay.broker.balance, 10000)

    def test_config_and_backtest_use_selected_instrument(self):
        code, _ = self.request("POST", "/api/config", {"symbol": "XAUUSD", "risk_fraction": .0025})
        self.assertEqual(code, 200)
        code, result = self.request("POST", "/api/backtest")
        self.assertEqual(code, 200)
        self.assertEqual(result["symbol"], "XAUUSD")
        self.assertTrue(result["finished"])
        self.assertEqual(result["config"]["risk_fraction"], .0025)
        self.assertEqual(self.journal.get("risk:paper")["risk_fraction"], .0025)
        self.assertGreater(result["evaluation_bars"], 0)
        self.assertIn("Synthetic", result["limitations"])

    def test_invalid_configuration_does_not_change_risk(self):
        for payload in [{"risk_fraction": .5}, {"symbol": "UNKNOWN"}, {"commission_per_lot": -1}, {"threshold": True}]:
            self.assertEqual(self.request("POST", "/api/config", payload)[0], 400)
        self.assertEqual(self.engine.config.risk_fraction, .005)
        self.assertEqual(self.engine.symbol_name, "EURUSD")

    def test_local_only_binding_and_static_path_allowlist(self):
        with self.assertRaises(ValueError): make_server(self.engine, "0.0.0.0", 0)
        self.assertEqual(self.request("GET", "/../pyproject.toml")[0], 404)

    def connection_payload(self):
        return {"login": "123", "password": "unit-test-password-DO-NOT-ECHO", "server": "FxPro-MT5 Demo", "mode": "mt5-demo", "symbol": "EURUSD"}

    def test_unsupported_preview_rejects_credentials(self):
        with patch("fxpro_bot.engine.MT5Broker.connection_support", return_value={"available": False, "message": "Use a local Windows MT5 terminal"}):
            code, result = self.request("POST", "/api/connect", self.connection_payload())
        self.assertEqual(code, 409)
        self.assertIn("Windows", result["error"])
        self.assertIsNone(self.engine.broker)

    def test_connection_leaves_orders_disabled_until_start_and_stop_revokes(self):
        api = FakeMT5()
        with patch("fxpro_bot.engine.MT5Broker.connection_support", return_value={"available": True, "message": "Local MT5"}), patch("fxpro_bot.mt5.load_api", return_value=api):
            code, result = self.request("POST", "/api/connect", self.connection_payload())
            self.assertEqual(code, 200, result)
            status = self.request("GET", "/api/status")[1]
            self.assertEqual(status["mode"], "mt5-demo")
            self.assertFalse(status["running"])
            self.assertFalse(status["trading_enabled"])
            self.assertTrue(status["mt5_connection"]["attached"])
            self.assertNotIn(self.connection_payload()["password"], json.dumps(status))
            with patch.object(self.engine.model, "predict", return_value=.8), patch("fxpro_bot.engine.atr", return_value=.002):
                self.engine._mt5_step()
                self.assertEqual(api.sent, [])
                self.assertEqual(self.request("POST", "/api/start")[0], 200)
                self.engine._mt5_step()
                self.assertEqual(len(api.sent), 1)
                self.assertEqual(self.request("POST", "/api/stop")[0], 200)
                self.assertEqual(len(api.sent), 1)  # Stop does not submit closes.
                self.engine._mt5_step()
                with self.assertRaises(TradingDisabled):
                    self.engine.broker.open("EURUSD", None)
                self.assertEqual(len(api.sent), 1)
            self.assertEqual(self.request("POST", "/api/disconnect")[0], 400)  # Position still open.
            self.assertEqual(self.request("POST", "/api/emergency-stop")[0], 200)
            self.assertEqual(api.positions, [])
            self.assertEqual(self.request("POST", "/api/disconnect")[0], 200)
            self.assertEqual(self.engine.mode, "paper")
            self.assertFalse(self.engine.running)

    def test_failed_sdk_login_never_echoes_or_journals_password(self):
        api = FakeMT5()
        password = self.connection_payload()["password"]
        with patch("fxpro_bot.engine.MT5Broker.connection_support", return_value={"available": True, "message": "Local MT5"}), patch("fxpro_bot.mt5.load_api", return_value=api), patch.object(api, "login", side_effect=RuntimeError(password)):
            code, result = self.request("POST", "/api/connect", self.connection_payload())
        self.assertEqual(code, 400)
        self.assertNotIn(password, json.dumps(result))
        self.assertNotIn(password, json.dumps(self.engine.status()))
        self.assertNotIn(password, "\n".join(self.journal.db.iterdump()))
        self.assertIsNone(self.engine.broker)
        self.assertFalse(self.engine.running)

    def test_live_connection_cannot_be_unlocked_by_the_form(self):
        payload = self.connection_payload()
        payload["mode"] = "mt5-live"
        with patch("fxpro_bot.engine.MT5Broker.connection_support", return_value={"available": True, "message": "Local MT5"}), patch("fxpro_bot.mt5.load_api") as load:
            self.assertEqual(self.request("POST", "/api/connect", payload)[0], 400)
            load.assert_not_called()
        self.assertFalse(self.engine.allow_live_connections)

    def test_connection_requires_csrf_even_when_supported(self):
        code, _ = self.request("POST", "/api/connect", self.connection_payload(), headers={"X-CSRF-Token": ""})
        self.assertEqual(code, 403)
        self.assertIsNone(self.engine.broker)


if __name__ == "__main__":
    unittest.main()
