from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import fields
from pathlib import Path

from .backtest import backtest
from .config import RiskConfig, policy_key
from .domain import Symbol
from .engine import Engine
from .journal import Journal
from .market import DEMO_SYMBOLS, demo_candles, load_csv
from .mt5 import BrokerError, MT5Broker
from .web import make_server


def load_market(args):
    if args.contract:
        spec = Symbol(**json.loads(Path(args.contract).read_text(encoding="utf-8")))
        if spec.name != args.symbol:
            raise ValueError("Contract name must match --symbol")
    else:
        spec = DEMO_SYMBOLS.get(args.symbol)
    if args.csv:
        if not spec:
            raise ValueError("Custom CSV instruments require --contract with tick/lot/currency specifications")
        return load_csv(args.csv), spec, "csv"
    if args.symbol not in DEMO_SYMBOLS or args.contract:
        raise ValueError("Custom instruments require real historical data via --csv")
    return demo_candles(args.symbol), spec, "synthetic"


def load_risk(path):
    if not path:
        return RiskConfig()
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or set(data)-{f.name for f in fields(RiskConfig)}:
        raise ValueError("Risk configuration contains unknown fields")
    return RiskConfig(**data)


def parser():
    p = argparse.ArgumentParser(description="AI signals, paper trading, and guarded FxPro MT5 execution. No profit guarantees.")
    commands = p.add_subparsers(dest="command", required=True)
    for name in ["serve", "backtest"]:
        cmd = commands.add_parser(name)
        cmd.add_argument("--symbol", default="EURUSD", help="Exact broker symbol name, including suffix")
        cmd.add_argument("--csv", help="Chronological UTC OHLC data; otherwise use labeled synthetic demo prices")
        cmd.add_argument("--contract", help="JSON Symbol contract for a custom CSV instrument")
        cmd.add_argument("--risk-config", help="JSON risk overrides")
        if name == "serve":
            cmd.add_argument("--mode", choices=["paper", "mt5-demo", "mt5-live"], default="paper")
            cmd.add_argument("--host", choices=["127.0.0.1", "localhost"], default="127.0.0.1")
            cmd.add_argument("--port", type=int, default=8080)
            cmd.add_argument("--data-dir", default="data")
            cmd.add_argument("--interval", type=float, default=1.0, help="Paper replay seconds per bar; MT5 polls at least every 5 seconds")
            cmd.add_argument("--account", type=int, help="Expected MT5 account number (not a password)")
            cmd.add_argument("--server", help="Exact FxPro server name shown in MT5")
            cmd.add_argument("--terminal-path", help="Optional full path to terminal64.exe on Windows")
            cmd.add_argument("--enable-live", action="store_true")
            cmd.add_argument("--confirm-live", default="")
        else:
            cmd.add_argument("--output", help="Write the full JSON result to this file")
    clear = commands.add_parser("clear-halt", help="Clear an execution halt ONLY after manually reconciling MT5")
    clear.add_argument("--data-dir", default="data")
    clear.add_argument("--account", type=int, required=True)
    clear.add_argument("--server", required=True)
    clear.add_argument("--confirm-reconciled", required=True, choices=["I_CHECKED_MT5_ORDERS_AND_POSITIONS"])
    return p


def main(argv=None):
    args = parser().parse_args(argv)
    journal = broker = engine = server = None
    try:
        if args.command == "clear-halt":
            journal = Journal(Path(args.data_dir)/"journal.sqlite3")
            key = f"halt:{args.server}:{args.account}"
            if not journal.get(key):
                raise ValueError("No halt exists for this account/server in this data directory")
            journal.set(key, None)
            journal.event("manual", "Execution halt cleared after explicit terminal reconciliation; risk baselines and signal reservations retained")
            print("Halt cleared. Daily-loss/drawdown checks and duplicate-signal protection still apply.")
            return 0
        risk = load_risk(args.risk_config)
        if args.command == "backtest":
            bars, spec, source = load_market(args)
            result = {**backtest(bars, spec, risk), "source": source,
                      "warning": "Historical/synthetic results do not predict future returns. No guaranteed win rate or profit."}
            serialized = json.dumps(result, indent=2, allow_nan=False)
            if args.output:
                Path(args.output).write_text(serialized+"\n", encoding="utf-8")
                print(json.dumps({"source": source, "metrics": result["metrics"], "output": args.output}, indent=2))
            else:
                print(serialized)
            return 0
        if not math.isfinite(args.interval) or args.interval < 0.05:
            raise ValueError("Interval must be finite and at least 0.05 seconds")
        journal = Journal(Path(args.data_dir)/"journal.sqlite3")
        saved_risk = journal.get(policy_key(args.mode, args.server, args.account))
        if saved_risk and not args.risk_config:
            risk = RiskConfig(**saved_risk)
        if args.mode != "paper":
            if args.csv or args.contract:
                raise ValueError("MT5 execution uses broker data and specifications; CSV/contract overrides are forbidden")
            if not args.account or not args.server:
                raise ValueError("MT5 requires --account and --server to prevent trading the wrong login")
            broker = MT5Broker(args.mode, journal, risk, args.account, args.server, args.terminal_path,
                               args.enable_live and args.confirm_live == "I_ACCEPT_REAL_MONEY_RISK")
            engine = Engine(journal, risk, args.symbol, broker=broker, interval=max(5, args.interval),
                            allow_live_connections=args.enable_live and args.confirm_live == "I_ACCEPT_REAL_MONEY_RISK",
                            terminal_path=args.terminal_path)
        else:
            bars, spec, source = load_market(args)
            engine = Engine(journal, risk, args.symbol, bars=bars if source == "csv" else None, spec=spec, interval=args.interval,
                            allow_live_connections=args.enable_live and args.confirm_live == "I_ACCEPT_REAL_MONEY_RISK",
                            terminal_path=args.terminal_path)
        server = make_server(engine, args.host, args.port)
        print(f"FxPro AI dashboard: http://{args.host}:{server.server_port} — {args.mode}; trading STOPPED", flush=True)
        print("No guaranteed profits. Keep the terminal/process running for automated management. Ctrl+C stops the process, not broker positions.", flush=True)
        server.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("\nStopping. Existing MT5 broker stop-loss/take-profit orders remain active; inspect your terminal.")
        return 0
    except (ValueError, KeyError, OSError, BrokerError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 2
    finally:
        if server:
            server.server_close()
        if engine:
            engine.shutdown()
        elif broker:
            broker.shutdown()
        if journal:
            journal.close()


if __name__ == "__main__":
    raise SystemExit(main())
