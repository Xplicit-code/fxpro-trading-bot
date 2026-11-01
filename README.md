# FxPro AI trading workspace

A local dashboard, AI signal model, historical backtester, paper-trading engine,
and an optional **FxPro MetaTrader 5** execution connector. Independent software;
not affiliated with or endorsed by FxPro.

**There is no guaranteed 90% win rate or profit.** This baseline model has not
demonstrated a profitable trading edge. A high win rate can still lose money.
Synthetic examples test the software, not the strategy's investment performance.
Do not treat model scores as calibrated probabilities or financial advice.

## Run the dashboard

Python 3.11+ is required. Paper trading, the dashboard, tests, and research use
only the Python standard library. No API keys, external services, or downloads
are needed for the default workflow.

```bash
cd /workspace/fxpro-trading-bot
python3 -m fxpro_bot serve
```

Open `http://127.0.0.1:8080` on the same machine. The dashboard is local-only and
starts **paused**. It deliberately refuses public network binding. Do not put
these trading controls behind a public reverse proxy without adding authentication.

The default feed is reproducible, **synthetic** M5 OHLC data for EURUSD, XAUUSD,
BTCUSD, or US500. Click **Start Trading** to replay one candle per second.
The replay starts with a simulated USD 10,000 account and cannot place real orders.
It pauses when the dataset ends; reset it for a new run.

Use **Strategy research** for a complete walk-forward backtest and downloadable
JSON report. **Risk & connection** edits the paused risk policy. Changing a
started paper run requires resetting it first. The prominent **Start Trading**
and **Stop Trading** buttons are available on every dashboard tab. Start stays
highlighted while automatic execution is authorized; Stop revokes that
authorization and leaves broker stop-loss/take-profit orders active.
**Emergency Close** separately attempts to close this bot's positions and latches
trading off. Verify closure in MT5. Stopping a paper replay also freezes its
simulated clock and protective fills.

Connection and trading are separate: signing in, loading a page, refreshing, or
restarting the process never enables MT5 orders. Stop is checked again immediately
before broker submission, including after preflight. An order already submitted
to the broker cannot be recalled by Stop; existing broker SL/TP can still execute.

## Backtest your own market history

```bash
python3 -m fxpro_bot backtest --symbol EURUSD --output report.json
python3 -m fxpro_bot backtest --symbol EURUSD --csv history.csv \
  --contract examples/contract.json --risk-config examples/risk.json --output report.json
python3 -m fxpro_bot serve --symbol EURUSD --csv history.csv --contract examples/contract.json
```

The first command uses synthetic data. The others use your historical CSV.
CSV columns are `time,open,high,low,close,volume`; `volume` is optional. Supply
at least 160 **closed, bid-price M5 candles**, in strictly increasing timestamp
order with explicit UTC offsets, no duplicates, and positive finite OHLC prices:

```csv
time,open,high,low,close,volume
2026-01-05T00:00:00Z,1.08500,1.08550,1.08470,1.08520,500
2026-01-05T00:05:00Z,1.08520,1.08580,1.08490,1.08560,620
```

The example contract is illustrative. Replace its tick size, account-currency
tick value, volume limits/step, spread, currency, and margin-per-lot with the
actual contract being tested. Custom instruments require `--csv` and `--contract`;
the contract's name must match `--symbol`. Price units and contract multipliers
differ between FX, metals, equities, indices, energy, and crypto. They cannot
safely share generic lot sizing. Incorrect inputs invalidate backtest results.

## Connect an FxPro MT5 account on Windows

The native `MetaTrader5` Python integration requires **Windows and a local MT5
desktop terminal**. Linux/cloud instances support research and paper trading;
they cannot run this connector. This does not automate app screen taps. Orders
go to the same FxPro MT5 account and appear in its terminal/mobile app.

1. Copy/clone this project onto Windows and install 64-bit Python 3.11+.
2. Install and open the FxPro MT5 desktop terminal, enable algorithmic trading,
   and allow external Python trading in MT5 settings.
3. In PowerShell, from the project directory:

```powershell
py -m pip install -e ".[mt5]"
py -m fxpro_bot serve
```

Open the dashboard locally on that Windows machine. The **Overview** connection
form accepts your numeric **MT5 account login**, **password**, exact **FxPro MT5
server**, account type, and initial broker instrument. Choose a demo account first.
Press **Connect MT5**, verify the account shown, then press **Start Trading** when
ready. Connecting leaves the bot stopped. **Stop Trading** allows you to stop and
resume without clearing a risk halt. Disconnect requires no bot positions to be
open; use Emergency Close and verify the terminal before disconnecting if needed.

The cloud/Linux preview shows the fields but disables credential entry/connection
because it cannot access the native Windows terminal. Its server also rejects
connection requests before reading credentials. Install the MT5 extra on Windows
and restart the dashboard to enable the form. No remote broker password should
be entered into a public/cloud preview.

Passwords are submitted in a CSRF-protected POST body to the **loopback-only local
dashboard**, passed once to the native MT5 login API, and cleared from the form
immediately. They never appear in URLs, application logs, status responses,
SQLite, saved configuration, or application browser storage. SDK login exceptions
are sanitized rather than reflected. This app does not save passwords; your MT5
terminal manages its own login session and credential-storage settings. Python
and the browser cannot guarantee erasure of every transient memory copy.

Use `--terminal-path "C:\path\to\terminal64.exe"` when multiple terminals are
installed. The terminal company/server must identify FxPro, and the selected
login, server, and demo/live type must match on every broker operation.

You can also connect an already logged-in demo terminal without entering its
password in the dashboard:

```powershell
py -m fxpro_bot serve --mode mt5-demo --account 12345678 --server "YOUR_EXACT_FXPRO_DEMO_SERVER" --symbol EURUSD
```

The connector reads actual closed M5 bars, bid/ask quotes, symbol specifications,
account currency, profit calculations, and margin requirements from MT5. Select
the exact broker symbol, including suffixes. Broker-enabled FX, metals, indices,
energy, shares, and crypto are supported **only where your account offers them**
and the instrument allows both buy/sell market orders with persistent SL/TP.
Some assets, regions, account types, and trading sessions are unavailable.

Start execution from the local dashboard only after checking the selected
account, instrument, and costs. Use a dedicated account and one bot process;
do not mix manual trades or another strategy on its symbols. New entries are
blocked when any account position or pending order exists. This version trades
**one selected instrument and at most one position at a time**; it is not a
simultaneous multi-asset portfolio manager.

Keep Windows, MT5, and this process running for ongoing signals, account risk
checks, and time exits. If the process stops or you press Stop Trading, server-side SL/TP
can remain active, but the bot cannot monitor risk or perform time exits.

### Optional real-money mode

Live mode is implemented but **has not been broker-validated in this cloud
environment**. Validate fills, symbol sizing, commissions, session behavior,
restart reconciliation, and all protections on an FxPro demo account first.
This model is a research baseline, not a production trading recommendation.

```powershell
py -m fxpro_bot serve --mode mt5-live --account 12345678 --server "YOUR_EXACT_FXPRO_LIVE_SERVER" --symbol EURUSD --enable-live --confirm-live I_ACCEPT_REAL_MONEY_RISK
```

Both live flags and a verified real account are required. The process still
starts stopped. To use the connection form for a live account, start with
`py -m fxpro_bot serve --enable-live --confirm-live I_ACCEPT_REAL_MONEY_RISK`,
select **Live account** in the form, and connect. The browser cannot unlock live
mode by itself. No live account has been connected or traded by this repository's
cloud validation.

## Model, fills, and risk limits

- **Model:** regularized logistic regression on six trailing return, trend, and
  candle-shape features. Normalization uses training data only. The target is
  next-close direction, not net profit. The default buy threshold is 0.57 and
  sell threshold 0.43; these uncalibrated scores do not establish an edge.
- **Evaluation:** initial 60% training (at least 126 bars), then chronological evaluation;
  rolling training on at most 425 earlier bars, retrained every 24 evaluation
  bars. Signals at a closed bar execute at the next bar's open. No future labels,
  random train/test shuffling, or retrospective selection of winning trades.
- **Simulation:** modeled bid/ask spread, adverse entry/exit slippage,
  round-trip commission, volume limits, margin, time exits, gap-aware stops,
  and stop-first treatment when both SL/TP touch inside one candle. The final
  position is closed at the end of the dataset. Limits use sampled equity;
  reported drawdown does not capture all intrabar excursions.
  Simulated fill timestamps identify their M5 candle, not an exact tick time.
- **Default limits:** 0.5% modeled cash risk per entry; 2% daily equity loss;
  10% drawdown from observed equity peak; one position; maximum 25% equity in
  required entry margin. Stops are 1.8 × ATR; take-profit is 1.8 × stop distance;
  maximum holding time is 24 M5 bars / 120 minutes. The default round-trip fee
  estimate is 7 account-currency units per lot and slippage is 2 ticks per fill.
  Adjust them using the dashboard or `--risk-config`.
- **Freshness:** broker quotes older than 20 seconds, bars more than 15 minutes
  past their expected close, future timestamps, invalid prices, and excessively
  wide spreads block entry. Market-closed errors halt execution until reviewed.
- **Broker protection:** account identity checks, broker-native cash/margin
  calculations, lot sizing rounded down, broker order preflight, SL/TP attached
  to every entry, post-fill protection/size/risk verification, broker-supported
  filling policies, ticket-specific exits, and no automatic order retries.

Limits reduce exposure; they cannot guarantee a maximum loss. Gaps, slippage,
market closure, connectivity, broker rejection, and partial fills can prevent
execution or exceed planned risk. No swaps, variable historical spreads, latency,
liquidity, taxes, or market impact are simulated. Historical data quality is
your responsibility; the bot does not independently verify quotes or use news
or economic-calendar feeds. Evaluate much longer, independent market histories
and multiple market regimes before considering actual funds.

## Restart, audit, and recovery

The ignored `data/journal.sqlite3` stores signal reservations, account risk
baselines, saved risk policies, persistent halts, and activity events. It stores no passwords.
Back up this directory; use the same directory across restarts. Paper balances,
positions, selected instrument, and equity curves are **in memory** and restart fresh.
Risk policies are restored by mode/account from the same data directory;
`--risk-config` explicitly overrides the saved policy.

MT5 daily equity is anchored at the first observation of the account on each
**UTC date**, not a reconstructed midnight balance. Peak equity is persisted
across restarts. These limits are account-wide, including outside trades and
deposits/withdrawals. Broker-account metrics deliberately leave win rate and
profit factor blank; use terminal history for actual closed-trade statistics.

Each MT5 signal is reserved in SQLite **before submission**, so a restart cannot
automatically repeat that account/symbol/candle's order. A missing response,
execution error, daily/drawdown limit, or emergency stop latches trading off.
Regular Stop Trading revokes order authorization without creating a persistent
halt, so Start Trading can resume if account checks pass.
Reservations and risk baselines remain even when a halt is cleared. Pending
orders and positions must be reconciled in MT5; a database cannot guarantee
exactly-once execution across every broker or machine failure.

After manually checking MT5 orders, deals, and positions, stop the running
process before clearing a halt:

```powershell
py -m fxpro_bot clear-halt --data-dir data --account 12345678 --server "YOUR_EXACT_FXPRO_SERVER" --confirm-reconciled I_CHECKED_MT5_ORDERS_AND_POSITIONS
```

Restart the server, inspect the account, and start it manually. Clearing a halt
does not erase risk limits or resend a reserved signal; account checks can still
block trading. Do not delete the journal to bypass a risk limit.

## Validate

```bash
python3 -m unittest discover -s tests -v
node --check fxpro_bot/static/app.js
```

Tests cover price ingestion, time-series leakage, realistic fill/cost accounting,
cash/margin/freshness limits, durable duplicate prevention, mocked MT5 orders and
failure modes, and the actual local HTTP controls with CSRF/origin/host checks.
HTTP tests require permission to bind loopback sockets. Broker tests use a fake
terminal; they do not establish real FxPro connectivity or fills.

Implementation: `model.py` (AI), `risk.py` (sizing/limits), `paper.py` and
`backtest.py` (simulation), `mt5.py` (native adapter), `engine.py` (automation),
`journal.py` (audit/deduplication), and `web.py` / `static/` (dashboard).
