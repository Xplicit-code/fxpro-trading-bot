"use strict";

const $ = (id) => document.getElementById(id);
let state = null, csrf = "", chartView = "price", report = null, busy = false, formDirty = false, toastTimer;
let pendingCommands = 0, serverReachable = false;
const text = (id, value) => { $(id).textContent = value; };
const number = (value, digits = 2) => value == null ? "—" : Number(value).toLocaleString("en-US", {minimumFractionDigits: digits, maximumFractionDigits: digits});
const money = (value, currency = state?.currency || "USD") => {
  if (value == null) return "—";
  try { return new Intl.NumberFormat("en-US", {style: "currency", currency, maximumFractionDigits: 2}).format(value); }
  catch { return `${number(value)} ${currency}`; }
};
const utc = (value) => new Date(value).toLocaleString("en-GB", {timeZone: "UTC", month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit"});

function toast(message, error = false) {
  clearTimeout(toastTimer);
  const box = $("toast"); box.textContent = message; box.classList.toggle("error", error); box.hidden = false;
  toastTimer = setTimeout(() => { box.hidden = true; }, error ? 10000 : 5000);
}

async function command(path, payload = {}) {
  if (!csrf) throw new Error("Wait for a connection to the local bot.");
  const response = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(payload)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Command failed");
  return result;
}

function cell(row, value, className = "") {
  const td = document.createElement("td"); td.textContent = value; td.className = className; row.append(td); return td;
}

function sideCell(row, side) {
  const td = document.createElement("td"), span = document.createElement("span");
  span.className = `pill-side ${side === "sell" ? "sell" : ""}`; span.textContent = side.toUpperCase(); td.append(span); row.append(td);
}

function chart(svg, points, {digits = 2, color = "#bbf36b", label = "Value"} = {}) {
  svg.replaceChildren();
  if (!points.length) return;
  const ns = "http://www.w3.org/2000/svg";
  const el = (tag, attrs = {}, content = null, parent = svg) => {
    const node = document.createElementNS(ns, tag);
    for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, value);
    if (content != null) node.textContent = content;
    parent.append(node); return node;
  };
  const bounds = {left: 14, right: 736, top: 15, bottom: 248};
  let low = Math.min(...points.map(p => p.value)), high = Math.max(...points.map(p => p.value));
  const padding = (high - low) * .16 || Math.abs(high) * .005 || 1;
  low -= padding; high += padding;
  const x = i => bounds.left + (bounds.right - bounds.left) * i / Math.max(1, points.length - 1);
  const y = v => bounds.bottom - (v - low) / (high - low) * (bounds.bottom - bounds.top);
  for (let i = 0; i <= 4; i++) {
    const value = low + (high - low) * i / 4, yy = y(value);
    el("line", {x1: bounds.left, x2: bounds.right, y1: yy, y2: yy, stroke: "#24313d", "stroke-dasharray": "3 5", "stroke-width": .8});
    el("text", {x: bounds.right + 12, y: yy + 3}, number(value, digits));
  }
  const steps = Math.min(5, points.length);
  for (let i = 0; i < steps; i++) {
    const idx = Math.round((points.length - 1) * i / Math.max(1, steps - 1));
    const date = new Date(points[idx].time);
    const value = date.toLocaleTimeString("en-GB", {timeZone: "UTC", hour: "2-digit", minute: "2-digit"});
    el("text", {x: x(idx), y: 272, "text-anchor": i === 0 ? "start" : "middle"}, value);
  }
  const defs = el("defs"), gradient = el("linearGradient", {id: `${svg.id}-fill`, x1: "0", y1: "0", x2: "0", y2: "1"}, null, defs);
  el("stop", {offset: "0%", "stop-color": color, "stop-opacity": .16}, null, gradient);
  el("stop", {offset: "100%", "stop-color": color, "stop-opacity": 0}, null, gradient);
  const path = points.map((p, i) => `${i ? "L" : "M"}${x(i).toFixed(2)},${y(p.value).toFixed(2)}`).join(" ");
  el("path", {d: `${path} L${x(points.length - 1)},${bounds.bottom} L${x(0)},${bounds.bottom} Z`, fill: `url(#${svg.id}-fill)`});
  el("path", {d: path, fill: "none", stroke: color, "stroke-width": 1.8, "stroke-linejoin": "round"});
  const last = points.at(-1);
  el("circle", {cx: x(points.length - 1), cy: y(last.value), r: 3.2, fill: color});
  const overlay = el("g", {visibility: "hidden"});
  const line = el("line", {y1: bounds.top, y2: bounds.bottom, stroke: "#9aadbb", "stroke-dasharray": "3 3"}, null, overlay);
  const point = el("circle", {r: 4, fill: color, stroke: "#101820", "stroke-width": 2}, null, overlay);
  el("rect", {x: 19, y: 10, width: 320, height: 25, fill: "#18242f", rx: 4}, null, overlay);
  const tooltip = el("text", {x: 28, y: 27, fill: "#dce7ef"}, null, overlay);
  svg.onmousemove = (event) => {
    const rect = svg.getBoundingClientRect(), localX = (event.clientX - rect.left) * 820 / rect.width;
    const index = Math.max(0, Math.min(points.length - 1, Math.round((localX - bounds.left) / (bounds.right - bounds.left) * (points.length - 1))));
    const p = points[index]; overlay.setAttribute("visibility", "visible");
    line.setAttribute("x1", x(index)); line.setAttribute("x2", x(index)); point.setAttribute("cx", x(index)); point.setAttribute("cy", y(p.value));
    tooltip.textContent = `${utc(p.time)} UTC · ${label} ${number(p.value, digits)}`;
  };
  svg.onmouseleave = () => overlay.setAttribute("visibility", "hidden");
}

function renderChart() {
  if (!state) return;
  const equity = chartView === "equity";
  const points = equity ? state.equity_curve.map(p => ({time: p.time, value: p.equity})) : state.candles.map(p => ({time: p.time, value: p.close}));
  chart($("market-chart"), points, {digits: equity ? 2 : state.spec.digits, color: equity ? "#81bdf8" : "#bbf36b", label: equity ? "Equity" : "Close"});
  text("chart-legend", equity ? `Account equity · ${state.currency}` : "Closed bid price");
  $("market-chart").setAttribute("aria-label", equity ? "Account equity curve" : `${state.symbol} closed bid price chart`);
  $("chart-empty").hidden = points.length > 1;
  text("chart-empty", equity ? "Start the replay to build an equity curve." : "Waiting for market observations.");
  $("price-view").classList.toggle("selected", !equity); $("equity-view").classList.toggle("selected", equity);
}

function render() {
  const s = state, paper = s.mode === "paper", live = s.mode === "mt5-live", synthetic = s.source === "synthetic";
  text("connection-label", s.connected ? paper ? "Local simulation" : "MT5 connected" : "MT5 disconnected");
  $("connection-dot").style.background = s.connected ? "#bbf36b" : "#f68e93";
  text("mode-pill", paper ? "PAPER ENVIRONMENT" : live ? "LIVE · REAL MONEY" : "MT5 DEMO ACCOUNT");
  $("mode-pill").classList.toggle("live", live);
  text("source-title", synthetic ? "Synthetic market simulation" : s.source === "csv" ? "Historical CSV replay" : live ? "Live execution enabled on this process" : "FxPro MT5 demo execution");
  text("source-description", synthetic ? "Demo prices are generated, not live market information. No real orders are placed." : s.source === "csv" ? "Imported historical prices. Simulated orders and estimated trading costs; no real orders are placed." : live ? "Orders can use real funds when started. Data comes from your local FxPro terminal; losses can exceed planned risk." : "Quotes, closed M5 candles, and orders come from your logged-in FxPro demo terminal.");
  text("source-tag", synthetic ? "DEMO DATA" : s.source === "csv" ? "HISTORICAL DATA" : "BROKER DATA");
  text("equity", money(s.account?.equity)); text("balance-label", s.account ? `Balance ${money(s.account.balance)}` : "Account data unavailable");
  text("pnl-label", paper ? "Replay net P&L" : "Observed UTC-day equity change");
  text("pnl", money(s.metrics.net_pnl)); $("pnl").className = `metric-value ${s.metrics.net_pnl > 0 ? "positive" : s.metrics.net_pnl < 0 ? "negative" : ""}`;
  text("return", paper ? `${number(s.metrics.return_pct)}% · including modeled costs` : "Since first observation today; account-wide");
  text("win-rate", s.metrics.win_rate == null ? "—" : `${number(s.metrics.win_rate, 1)}%`);
  text("trade-count", s.metrics.trade_count == null ? "See broker terminal for trade statistics" : `${s.metrics.trade_count} closed trades · not a target or guarantee`);
  text("drawdown", s.metrics.max_drawdown_pct == null ? "—" : `${number(s.metrics.max_drawdown_pct)}%`);
  text("drawdown-limit", `Limit: ${number(s.config.max_drawdown_fraction * 100, 1)}%${paper ? "" : " · current from observed peak"}`);
  const select = $("symbol");
  if (select.options.length !== s.symbols.length || ![...select.options].some(o => o.value === s.symbol)) {
    select.replaceChildren(...s.symbols.map(symbol => {const option = document.createElement("option"); option.value = symbol; option.textContent = symbol; return option;}));
  }
  select.value = s.symbol; select.disabled = s.running || busy;
  text("instrument-icon", ({EURUSD: "€", XAUUSD: "Au", BTCUSD: "₿", US500: "S"})[s.symbol] || "↗");
  text("instrument-description", `M5 · ${synthetic ? "synthetic" : s.source === "csv" ? "imported" : "broker"} closed bid prices`);
  const first = s.candles[0], last = s.candles.at(-1), change = last && first ? (last.close / first.close - 1) * 100 : null;
  text("last-price", last ? number(last.close, s.spec.digits) : "—"); text("price-change", change == null ? "—" : `${change > 0 ? "+" : ""}${number(change)}% in view`);
  $("price-change").className = change >= 0 ? "positive" : "negative";
  text("chart-source", paper ? "HISTORICAL REPLAY" : "LAST CLOSED M5 CANDLE"); text("chart-time", last ? `${utc(last.time)} UTC` : "UTC · M5");
  const score = s.score;
  text("signal-score", score == null ? "—" : number(score, 3));
  text("signal-state", score == null ? "Awaiting a signal" : score >= s.config.threshold ? "Buy signal" : score <= 1 - s.config.threshold ? "Sell signal" : "No entry signal");
  $("score-pointer").style.left = `${(score ?? .5) * 100}%`;
  text("training-count", s.model.samples || "Not trained yet"); text("threshold-label", `Buy ≥ ${number(s.config.threshold)} / Sell ≤ ${number(1 - s.config.threshold)}`);
  text("bot-status", s.error ? "Trading halted" : s.running ? paper ? "Trading active · Paper" : "Trading active · MT5" : "Trading stopped");
  text("bot-reason", s.error || s.reason); $("status-orb").classList.toggle("running", s.running);
  $("start").setAttribute("aria-pressed", String(s.running && s.trading_enabled));
  $("start").title = paper ? "Start the paper simulation; no MT5 orders" : live ? "Enable real-money MT5 bot orders" : "Enable MT5 demo bot orders";
  $("start").disabled = busy || !serverReachable || s.running || !s.connected || s.halted || s.progress?.finished;
  $("stop").disabled = !s.running && !busy;
  $("emergency-stop").disabled = busy || (!s.running && !s.positions.length);
  $("reset").hidden = !paper; $("reset").disabled = busy || s.running || s.positions.length > 0;
  text("progress", s.progress ? `${s.progress.completed} / ${s.progress.total} evaluation candles · 1 bar / replay step` : "Keep MT5 and this process running for automated management");
  text("risk-summary", `${number(s.config.risk_fraction * 100, 2)}% risk / trade · ${number(s.config.daily_loss_fraction * 100, 1)}% daily loss limit`);
  text("position-count", `${s.positions.length} / ${s.config.max_positions}`); $("no-positions").hidden = s.positions.length > 0;
  $("positions").replaceChildren(...s.positions.map(p => {const tr = document.createElement("tr"); cell(tr, p.symbol); cell(tr, `${p.side.toUpperCase()} / ${number(p.volume)}`); cell(tr, number(p.entry, s.spec.digits)); cell(tr, number(p.stop, s.spec.digits)); cell(tr, number(p.take, s.spec.digits)); return tr;}));
  $("events").replaceChildren(...s.events.map(e => {const div = document.createElement("div"); div.className = `event ${["error", "close"].includes(e.kind) ? e.kind : ""}`; const dot = document.createElement("span"); dot.className = "event-dot"; const body = document.createElement("div"), p = document.createElement("p"), time = document.createElement("time"); p.textContent = e.message; time.textContent = `${utc(e.time)} UTC`; time.dateTime = e.time; body.append(p, time); div.append(dot, body); return div;}));
  $("no-trades").hidden = s.trades.length > 0;
  $("trades").replaceChildren(...[...s.trades].reverse().map(t => {const tr = document.createElement("tr"); cell(tr, utc(t.closed_at)); cell(tr, t.symbol); sideCell(tr, t.side); cell(tr, number(t.volume)); cell(tr, `${number(t.entry, s.spec.digits)} → ${number(t.exit, s.spec.digits)}`); cell(tr, money(t.pnl), t.pnl >= 0 ? "positive" : "negative"); cell(tr, t.reason.replaceAll("_", " ")); return tr;}));
  const form = $("settings-form");
  for (const input of form.querySelectorAll("input")) {
    if (!formDirty) input.value = s.config[input.name] * (["risk_fraction", "daily_loss_fraction"].includes(input.name) ? 100 : 1);
    input.disabled = s.running || busy;
  }
  form.querySelector("button").disabled = s.running || busy;
  const connection = s.mt5_connection;
  const canConnect = connection.available && !connection.attached && !s.running && !busy && serverReachable;
  for (const input of $("mt5-form").querySelectorAll("input, select")) input.disabled = !canConnect;
  $("connect-mt5").disabled = !canConnect;
  $("connect-mt5").hidden = connection.attached;
  $("disconnect-mt5").hidden = !connection.attached;
  $("disconnect-mt5").disabled = busy || s.running;
  $("mt5-live-option").disabled = !connection.live_allowed;
  text("mt5-live-option", connection.live_allowed ? "Live account · real money" : "Live account · locked");
  text("mt5-connection-badge", connection.attached ? live ? "LIVE ACCOUNT CONNECTED" : "DEMO ACCOUNT CONNECTED" : connection.available ? "READY TO CONNECT" : "WINDOWS MT5 REQUIRED");
  text("mt5-support-message", connection.attached ? `Connected to ${connection.server} · account ${connection.login}. ${s.running ? "Automatic trading is active." : "Trading is stopped. Press Start Trading to enable orders."}` : connection.message);
  $("mt5-support-message").classList.toggle("connected", connection.attached);
  if (connection.attached) {
    $("mt5-login").value = connection.login;
    $("mt5-server").value = connection.server;
    $("mt5-account-type").value = s.mode;
    $("mt5-symbol").value = s.symbol;
    $("mt5-password").value = "";
  }
  renderChart();
}

async function refresh() {
  try {
    const response = await fetch("/api/status");
    const data = await response.json(); if (!response.ok) throw new Error(data.error);
    state = data; csrf = data.csrf_token; serverReachable = true; render();
  } catch {
    serverReachable = false;
    text("connection-label", "Connection lost — check process"); $("connection-dot").style.background = "#f68e93";
    $("start").disabled = true; $("reset").disabled = true;
    $("connect-mt5").disabled = true;
    $("mt5-password").value = "";
    if (state?.mode !== "paper") text("bot-reason", "Connection lost. Check MT5 directly; existing positions may still be open.");
  }
}

async function action(path, payload = {}) {
  pendingCommands += 1; busy = true; if (state) render();
  try { await command(path, payload); await refresh(); }
  catch (error) { toast(error.message, true); }
  finally { pendingCommands -= 1; busy = pendingCommands > 0; if (state) render(); }
}

for (const nav of document.querySelectorAll("[data-page]")) nav.addEventListener("click", () => {
  for (const page of document.querySelectorAll(".page")) page.hidden = page.id !== nav.dataset.page;
  for (const button of document.querySelectorAll("[data-page]")) button.classList.toggle("active", button === nav);
  const copy = {overview: ["Overview", "Trading overview", "A clear view of your strategy, signals, and exposure."], research: ["Strategy research", "Find an edge. Test it", "Honest measurements. Chronological evaluation. No promises."], settings: ["Risk & connection", "Build your guardrails", "Control the downside before you automate the upside."]}[nav.dataset.page];
  text("page-label", copy[0]); $("heading").replaceChildren(document.createTextNode(copy[1])); const period = document.createElement("span"); period.textContent = "."; $("heading").append(period); text("subheading", copy[2]);
  window.scrollTo(0, 0);
});
$("price-view").onclick = () => {chartView = "price"; renderChart();};
$("equity-view").onclick = () => {chartView = "equity"; renderChart();};
$("start").onclick = () => action("/api/start");
$("stop").onclick = () => action("/api/stop");
$("emergency-stop").onclick = () => action("/api/emergency-stop");
$("reset").onclick = () => action("/api/reset");
$("symbol").onchange = async (event) => {await action("/api/config", {symbol: event.target.value}); await refresh();};
$("mt5-form").onsubmit = async (event) => {
  event.preventDefault();
  if (!state?.mt5_connection.available || !serverReachable || busy) {
    $("mt5-password").value = "";
    toast(state?.mt5_connection.message || "Connect from the local Windows dashboard.", true);
    return;
  }
  const payload = {login: $("mt5-login").value.trim(), password: $("mt5-password").value,
    server: $("mt5-server").value.trim(), mode: $("mt5-account-type").value, symbol: $("mt5-symbol").value.trim()};
  $("mt5-password").value = "";
  pendingCommands += 1; busy = true; render();
  try {
    const request = command("/api/connect", payload);
    delete payload.password;
    await request;
    report = null; $("backtest-results").hidden = true; $("backtest-empty").hidden = false;
    toast("MT5 connected. Trading remains stopped until you press Start Trading.");
    await refresh();
  } catch (error) { toast(error.message, true); }
  finally {
    delete payload.password;
    $("mt5-password").value = "";
    pendingCommands -= 1; busy = pendingCommands > 0; if (state) render();
  }
};
$("disconnect-mt5").onclick = async () => {
  await action("/api/disconnect");
  if (!state?.mt5_connection.attached) $("mt5-form").reset();
};
window.addEventListener("pagehide", () => { $("mt5-password").value = ""; });
$("settings-form").oninput = () => {formDirty = true;};
$("settings-form").onsubmit = async (event) => {
  event.preventDefault(); const payload = {};
  for (const [name, value] of new FormData(event.currentTarget)) payload[name] = Number(value) / (["risk_fraction", "daily_loss_fraction"].includes(name) ? 100 : 1);
  try { await command("/api/config", payload); formDirty = false; toast("Risk policy updated."); await refresh(); }
  catch (error) {toast(error.message, true);}
};
$("backtest").onclick = async () => {
  const button = $("backtest"); button.disabled = true; button.textContent = "Evaluating past candles…";
  try {
    report = await command("/api/backtest"); $("backtest-empty").hidden = true; $("backtest-results").hidden = false;
    const m = report.metrics;
    const cards = [["Net return", `${number(m.return_pct)}%`, money(m.net_pnl, report.currency)], ["Closed-trade win rate", m.win_rate == null ? "—" : `${number(m.win_rate, 1)}%`, `${m.trade_count} closed trades`], ["Maximum drawdown", `${number(m.max_drawdown_pct)}%`, "Measured on close-to-close equity"], ["Profit factor", m.profit_factor == null ? "—" : number(m.profit_factor), m.profit_factor == null ? "Undefined when there are no losses" : "Net winning P&L / net losing P&L"]];
    $("backtest-metrics").replaceChildren(...cards.map(([label, value, foot]) => {const card = document.createElement("article"); card.className = "metric"; for (const [kind, str] of [["metric-label", label], ["metric-value", value], ["metric-foot", foot]]) {const div = document.createElement("div"); div.className = kind; div.textContent = str; card.append(div);} return card;}));
    chart($("backtest-chart"), report.equity_curve.map(p => ({time: p.time, value: p.equity})), {color: "#81bdf8", label: "Equity"});
    text("backtest-source", `${report.symbol} · ${report.source === "synthetic" ? "SYNTHETIC DATA" : "HISTORICAL DATA"}`);
    text("backtest-detail", `${report.training_bars} initial training bars · ${report.evaluation_bars} evaluation bars · ${m.wins} wins / ${m.losses} losses / ${m.breakeven} breakeven · ${money(report.config.commission_per_lot, report.currency)} round-trip commission per lot`);
    text("backtest-limits", report.limitations); await refresh();
  } catch (error) {toast(error.message, true);}
  finally {button.disabled = false; button.textContent = "↗ Run walk-forward test";}
};
$("download-report").onclick = () => {
  if (!report) return;
  const url = URL.createObjectURL(new Blob([JSON.stringify(report, null, 2)], {type: "application/json"}));
  const a = document.createElement("a"); a.href = url; a.download = `backtest-${report.symbol}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000);
};
(async function poll() {await refresh(); setTimeout(poll, 1500);})();
