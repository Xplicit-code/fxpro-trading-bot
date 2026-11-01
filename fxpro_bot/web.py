from __future__ import annotations

import hmac
import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .engine import Engine
from .mt5 import BrokerError

STATIC = Path(__file__).with_name("static")


def make_server(engine: Engine, host: str = "127.0.0.1", port: int = 8080) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("The trading dashboard must bind to loopback. Do not expose unauthenticated trading controls publicly.")
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Request logs are intentionally quiet; never log token headers.

        def host_allowed(self):
            value = self.headers.get("Host", "")
            return value in {f"127.0.0.1:{self.server.server_port}", f"localhost:{self.server.server_port}"}

        def respond(self, code: int, body: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            self.wfile.write(body)

        def json(self, code: int, value):
            self.respond(code, json.dumps(value, allow_nan=False).encode(), "application/json; charset=utf-8")

        def do_GET(self):
            if not self.host_allowed():
                return self.json(403, {"error": "Invalid dashboard host"})
            path = urlsplit(self.path).path
            if path == "/api/status":
                try:
                    return self.json(200, {**engine.status(), "csrf_token": token})
                except Exception:
                    return self.json(503, {"error": "Status is unavailable; check the trading process and MT5 terminal"})
            assets = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if path in assets:
                file, kind = assets[path]
                return self.respond(200, (STATIC/file).read_bytes(), kind+"; charset=utf-8")
            self.json(404, {"error": "Not found"})

        def do_POST(self):
            if not self.host_allowed():
                return self.json(403, {"error": "Invalid dashboard host"})
            origin = self.headers.get("Origin")
            if origin and origin not in {f"http://127.0.0.1:{self.server.server_port}", f"http://localhost:{self.server.server_port}"}:
                return self.json(403, {"error": "Cross-origin commands are forbidden"})
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token", "").encode(), token.encode()):
                return self.json(403, {"error": "Refresh the dashboard before issuing a command"})
            if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
                return self.json(415, {"error": "JSON is required"})
            path = urlsplit(self.path).path
            if path == "/api/connect":
                support = engine.connection_status()
                if not support["available"]:
                    # Refuse unsupported hosts before reading credentials.
                    return self.json(409, {"error": support["message"]})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 32768:
                    return self.json(413, {"error": "Invalid request size"})
                payload = json.loads(self.rfile.read(length))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                if path == "/api/connect":
                    try:
                        engine.connect_mt5(payload)
                    except Exception:
                        # Never echo SDK errors, request values, or a password.
                        return self.json(400, {"error": "MT5 connection failed. Check your login, password, exact FxPro server, instrument and account type. Stop trading and disconnect any existing account first."})
                    finally:
                        payload.clear()
                    return self.json(200, {"ok": True})
                actions = {"/api/start": engine.start, "/api/pause": engine.pause,
                           "/api/stop": engine.stop, "/api/emergency-stop": engine.emergency_stop,
                           "/api/disconnect": engine.disconnect_mt5, "/api/reset": engine.reset_paper}
                if path in actions:
                    actions[path]()
                    return self.json(200, {"ok": True})
                if path == "/api/config":
                    engine.configure(payload)
                    return self.json(200, {"ok": True})
                if path == "/api/backtest":
                    return self.json(200, engine.run_backtest())
                return self.json(404, {"error": "Not found"})
            except (ValueError, TypeError, BrokerError) as error:
                return self.json(400, {"error": str(error)})
            except Exception:
                return self.json(500, {"error": "Command failed. Check the activity log and MT5 before retrying."})

    return ThreadingHTTPServer((host, port), Handler)
