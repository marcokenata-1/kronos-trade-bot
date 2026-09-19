import io
import os
import sys
import json
import time
import argparse
import subprocess
import threading
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest, GetOrdersRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, QueryOrderStatus

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "kronos"))

load_dotenv()

MARKETS = {
    "US": ["AAPL", "MSFT"],
    "ASX": ["BHP.AX", "CBA.AX"],
    "IDX": ["BBCA.JK", "TLKM.JK"],
    "CRYPTO": ["BTC-USD", "ETH-USD"],
}

LOOKBACK = 400
PRED_LEN = 10
SIGNAL_THRESHOLD = 0.01  # ponytail: flat threshold, tune per-market volatility if signals look noisy
SAMPLE_COUNT = 5  # averaged internally by the model — cuts single-path noise like the AMD -64% outlier
STOP_LOSS_PCT = 0.05  # sell a position down this much from entry, regardless of current signal
DRAWDOWN_HALT_PCT = 0.25  # stop opening new positions once equity is down this much from baseline

BASELINE_FILE = Path(__file__).parent / "baseline.json"
EVENTS_FILE = Path(__file__).parent / ".daily_events.log"  # notes from today's runs, drained by `report`
PAUSE_FILE = Path(__file__).parent / "PAUSED"  # present = no new buys; committed, so GitHub Actions sees it
DASHBOARD_FILE = Path(__file__).parent / "dashboard.html"

_predictor = None
_trading_client = None


def note(line):
    """Print `line` and remember it for the end-of-day Telegram report."""
    print(line)
    with EVENTS_FILE.open("a") as f:
        f.write(line + "\n")


def get_trading_client():
    global _trading_client
    if _trading_client is None:
        # ponytail: paper unless ALPACA_LIVE=1 is explicitly set — no accidental live orders,
        # and paper/live use separate key pairs so a stray var can't cross the two
        prefix = "ALPACA_LIVE" if is_live() else "ALPACA_PAPER"
        _trading_client = TradingClient(
            os.environ[f"{prefix}_API_KEY"],
            os.environ[f"{prefix}_SECRET_KEY"],
            paper=not is_live(),
        )
    return _trading_client


def is_live():
    return bool(os.environ.get("ALPACA_LIVE"))


def is_crypto(ticker):
    return ticker.endswith("-USD")


def crypto_tickers():
    """All tradable Alpaca USD-quoted crypto pairs, in yfinance's "BTC-USD" format."""
    assets = get_trading_client().get_all_assets(GetAssetsRequest(asset_class=AssetClass.CRYPTO))
    return sorted(a.symbol.replace("/", "-") for a in assets if a.tradable and a.symbol.endswith("/USD"))


def place_order(ticker, side, qty=None, notional=None):
    # ponytail: crypto needs Alpaca's "BTC/USD" symbol (yfinance uses "BTC-USD") and GTC —
    # Alpaca rejects DAY time-in-force on crypto orders since crypto trades 24/7
    order = MarketOrderRequest(
        symbol=ticker.replace("-", "/") if is_crypto(ticker) else ticker,
        qty=qty,
        notional=notional,
        side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
        time_in_force=TimeInForce.GTC if is_crypto(ticker) else TimeInForce.DAY,
    )
    return get_trading_client().submit_order(order)


def get_baseline():
    if BASELINE_FILE.exists():
        return json.loads(BASELINE_FILE.read_text())["equity"]
    return None


def check_stop_losses():
    """Sell any held position down more than STOP_LOSS_PCT from entry, regardless of signal."""
    client = get_trading_client()
    for position in client.get_all_positions():
        plpc = float(position.unrealized_plpc)
        if plpc <= -STOP_LOSS_PCT:
            try:
                client.close_position(position.symbol)
                note(f"SOLD {position.symbol}: stop-loss, down {plpc:+.2%} from entry")
            except Exception as e:
                print(f"stop-loss: failed to close {position.symbol} ({e})")


def watch_stop_losses(interval=60):
    """Loop forever, checking stop-losses on a short interval (no model, no cron)."""
    print(f"watching positions every {interval}s for stop-loss (Ctrl+C to stop)")
    while True:
        try:
            check_stop_losses()
        except Exception as e:
            print(f"stop-loss check failed: {e}")
        time.sleep(interval)


def position_ticker(position):
    """yfinance-style ticker for an Alpaca position (positions report crypto as "BTCUSD", yfinance wants "BTC-USD")."""
    return position.symbol[:-3] + "-USD" if position.asset_class == AssetClass.CRYPTO else position.symbol


def check_signal_exits(crypto):
    """Sell any held crypto (or, with crypto=False, stock) position whose freshly re-checked forecast has flipped
    to SELL. Each daily run only re-forecasts its own asset class, so no position is forecast twice."""
    for position in get_trading_client().get_all_positions():
        if (position.asset_class == AssetClass.CRYPTO) != crypto:
            continue
        ticker = position_ticker(position)
        try:
            direction, change = signal(ticker)
        except Exception as e:
            print(f"{position.symbol}: could not re-check signal ({e})")
            continue
        if direction == "SELL":
            try:
                get_trading_client().close_position(position.symbol)
                note(f"SOLD {position.symbol}: Kronos forecast flipped to {change:+.2%} over the next {PRED_LEN} days")
            except Exception as e:
                print(f"signal exit: failed to close {position.symbol} ({e})")


def is_paused():
    return PAUSE_FILE.exists()


def set_paused(paused):
    """Create/delete the PAUSED flag and push it, so the GitHub Actions run sees it too."""
    if is_paused() == paused:
        return

    def git(*args):
        return subprocess.run(["git", *args], cwd=PAUSE_FILE.parent, check=True, capture_output=True, text=True).stdout.strip()

    # ponytail: pushes to main only (what the cron checks out); no retry if CI pushed baseline.json mid-toggle
    if git("branch", "--show-current") != "main":
        raise RuntimeError("run the dashboard from the main branch so the pause reaches GitHub Actions")
    git("pull", "--ff-only")
    PAUSE_FILE.touch() if paused else PAUSE_FILE.unlink()
    git("add", PAUSE_FILE.name)
    git("commit", "-m", "Pause new buys" if paused else "Resume buying", "--", PAUSE_FILE.name)
    git("push")


def check_drawdown_halt():
    """True if equity has fallen more than DRAWDOWN_HALT_PCT below the stored baseline."""
    baseline = get_baseline()
    if baseline is None:
        return False
    equity = float(get_trading_client().get_account().equity)
    if equity < baseline * (1 - DRAWDOWN_HALT_PCT):
        note(f"HALT: equity ${equity:.2f} is down >{DRAWDOWN_HALT_PCT:.0%} from baseline ${baseline:.2f}, skipping new buys")
        return True
    return False


def rebalance(pool=None, split=4):
    """Once account equity has doubled off the stored baseline, put half the
    profit into notional buys spread across `pool` (default: US stocks + crypto),
    then reset the baseline."""
    client = get_trading_client()
    equity = float(client.get_account().equity)
    baseline = get_baseline()

    if baseline is None:
        BASELINE_FILE.write_text(json.dumps({"equity": equity}))
        print(f"baseline set to ${equity:.2f}")
        return

    print(f"equity ${equity:.2f} vs baseline ${baseline:.2f} ({equity / baseline:.2f}x)")

    if equity < 2 * baseline:
        return

    if is_paused():
        note("PAUSED: equity doubled but skipping the reinvestment buys")
        return

    pool = (pool or MARKETS["US"] + MARKETS["CRYPTO"])[:split]
    reinvest = (equity - baseline) / 2
    per_ticker = reinvest / len(pool)

    # Alpaca crypto orders need >= $10 notional
    if per_ticker < (10 if any(map(is_crypto, pool)) else 1):
        print(f"reinvest amount ${reinvest:.2f} too small to split across {len(pool)} tickers, skipping")
        return

    for ticker in pool:
        order = place_order(ticker, "BUY", notional=round(per_ticker, 2))
        note(f"BOUGHT ${per_ticker:.2f} {ticker}: profit reinvestment, equity hit 2x baseline (order {order.id})")

    BASELINE_FILE.write_text(json.dumps({"equity": equity}))
    print(f"baseline reset to ${equity:.2f}")


def get_predictor():
    global _predictor
    if _predictor is None:
        # imported here, not at the top: torch is slow to import, and the stop-loss job never needs it
        from model import Kronos, KronosTokenizer, KronosPredictor
        tokenizer = KronosTokenizer.from_pretrained("NeoQuasar/Kronos-Tokenizer-base")
        model = Kronos.from_pretrained("NeoQuasar/Kronos-small")
        # ponytail: forced to CPU, torch's MPS scaled_dot_product_attention doesn't support
        # the dropout Kronos uses on Apple Silicon — revisit if this gets too slow to use
        _predictor = KronosPredictor(model, tokenizer, device="cpu", max_context=512)
    return _predictor


def fetch_ohlcv(ticker, period="2y", interval="1d"):
    df = yf.download(ticker, period=period, interval=interval, progress=False)
    if df.columns.nlevels > 1:
        df.columns = df.columns.get_level_values(0)
    df.index.name = "timestamp"
    df = df.reset_index()
    df.columns = [c.lower() for c in df.columns]
    return df[["timestamp", "open", "high", "low", "close", "volume"]]


def signal(ticker, df=None):
    if df is None:
        df = fetch_ohlcv(ticker)
    x_df = df.iloc[-LOOKBACK:][["open", "high", "low", "close", "volume"]]
    x_timestamp = df.iloc[-LOOKBACK:]["timestamp"]
    freq = pd.infer_freq(x_timestamp) or "B"
    y_timestamp = pd.Series(pd.date_range(x_timestamp.iloc[-1], periods=PRED_LEN + 1, freq=freq)[1:])

    pred = get_predictor().predict(
        df=x_df, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
        pred_len=PRED_LEN, T=1.0, top_p=0.9, sample_count=SAMPLE_COUNT, verbose=False,
    )

    last_close = x_df["close"].iloc[-1]
    forecast_close = pred["close"].iloc[-1]
    change = (forecast_close - last_close) / last_close

    if change > SIGNAL_THRESHOLD:
        direction = "BUY"
    elif change < -SIGNAL_THRESHOLD:
        direction = "SELL"
    else:
        direction = "HOLD"

    return direction, change


def backtest(ticker, window=LOOKBACK, pred_len=PRED_LEN, steps=20):
    df = fetch_ohlcv(ticker, period="5y")
    correct = total = 0

    for i in range(steps):
        end = len(df) - (steps - i) * pred_len
        if end - window < 0 or end + pred_len > len(df):
            continue

        x_df = df.iloc[end - window:end][["open", "high", "low", "close", "volume"]]
        x_timestamp = df.iloc[end - window:end]["timestamp"]
        y_timestamp = df.iloc[end:end + pred_len]["timestamp"]

        pred = get_predictor().predict(
            df=x_df, x_timestamp=x_timestamp, y_timestamp=y_timestamp,
            pred_len=pred_len, T=1.0, top_p=0.9, sample_count=SAMPLE_COUNT, verbose=False,
        )

        predicted_dir = pred["close"].iloc[-1] > x_df["close"].iloc[-1]
        actual_dir = df["close"].iloc[end + pred_len - 1] > x_df["close"].iloc[-1]
        correct += predicted_dir == actual_dir
        total += 1

    print(f"{ticker}: direction accuracy {correct}/{total}")


def sp500_tickers():
    # ponytail: routed through requests (bundles certifi CA certs) instead of pd.read_html's
    # urllib, which fails on this machine's python.org build with no system CA bundle wired up
    headers = {"User-Agent": "kronos-test-bot/1.0 (personal project)"}
    html = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=headers, timeout=10).text
    table = pd.read_html(io.StringIO(html), flavor="lxml")[0]
    return table["Symbol"].str.replace(".", "-", regex=False).tolist()


def _fetch_or_error(ticker):
    try:
        return fetch_ohlcv(ticker)
    except Exception as e:
        return e


def scan(tickers, fetch_workers=10):
    # ponytail: fixed worker count, not adaptive to Yahoo Finance rate-limiting —
    # lower it if runs start hitting 429s under this concurrency
    with ThreadPoolExecutor(max_workers=fetch_workers) as pool:
        dfs = list(pool.map(_fetch_or_error, tickers))

    results = []
    for i, (ticker, df) in enumerate(zip(tickers, dfs), 1):
        try:
            if isinstance(df, Exception):
                raise df
            direction, change = signal(ticker, df=df)
            results.append((ticker, direction, change))
        except Exception as e:
            print(f"[{i}/{len(tickers)}] {ticker}: skipped ({e})")
            continue
        print(f"[{i}/{len(tickers)}] {ticker}: {direction} ({change:+.2%})")
    return results


def recommend(tickers, top_n=10):
    results = scan(tickers)
    results.sort(key=lambda r: r[2], reverse=True)
    print(f"\nTop {top_n} BUY:")
    for ticker, direction, change in results[:top_n]:
        print(f"  {ticker}: {change:+.2%}")
    print(f"\nTop {top_n} SELL:")
    for ticker, direction, change in results[-top_n:][::-1]:
        print(f"  {ticker}: {change:+.2%}")


def auto_trade(tickers, top_n=5, notional=2.0, budget=None, crypto=False):
    """Exit losers (stop-loss + signal flip), then, unless the drawdown circuit breaker
    or the pause flag is on, scan `tickers` and buy $notional of the top_n BUY signals
    you don't already hold. `budget` caps total dollars held across all positions."""
    check_stop_losses()
    check_signal_exits(crypto)

    if check_drawdown_halt():
        return

    if is_paused():
        note("PAUSED: skipping new buys (delete the PAUSED file or press Resume in the dashboard)")
        return

    positions = get_trading_client().get_all_positions()
    held = {position_ticker(p) for p in positions}
    invested = sum(float(p.market_value) for p in positions)

    results = scan([t for t in tickers if t not in held])  # held ones were just re-forecast by the exit check
    buys = sorted((r for r in results if r[1] == "BUY"), key=lambda r: r[2], reverse=True)[:top_n]

    if not buys:
        note(f"no new BUY signals in {len(results)} scanned tickers ({len(held)} already held), nothing bought")
        return

    for ticker, direction, change in buys:
        if budget is not None and invested + notional > budget:
            note(f"BUDGET: ${invested:.2f} of ${budget:.2f} already invested, skipping the remaining buys")
            break
        try:
            order = place_order(ticker, "BUY", notional=notional)
        except Exception as e:
            note(f"FAILED to buy {ticker}: {e}")
            continue
        invested += notional
        note(f"BOUGHT ${notional:.2f} {ticker}: Kronos forecasts {change:+.2%} over the next {PRED_LEN} days (BUY above {SIGNAL_THRESHOLD:+.0%}, order {order.id})")


def usd_signed(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.2f}"


def _closes(ticker):
    df = fetch_ohlcv(ticker, period="2y")
    return df.set_index(pd.to_datetime(df["timestamp"]).dt.tz_localize(None))["close"]


def _close_on(closes, when):
    """Last close on or before `when`, else the first close we have."""
    before = closes[: pd.Timestamp(when.date())]
    return before.iloc[-1] if len(before) else closes.iloc[0]


def benchmark(orders, positions, closes):
    """P/L of what the bot actually did vs putting the same dollars into SPY (its stock trades) or
    BTC (its crypto trades) on the same days. `closes` maps "stocks"/"crypto" to a close-price Series."""
    # ponytail: daily closes, not fill-time prices, and fees ignored; only as good as the last 500 orders
    filled = [o for o in orders if o.filled_avg_price and float(o.filled_qty or 0) > 0]
    result = {"since": min((o.filled_at.date() for o in filled), default=None), "sleeves": {}}
    for sleeve, name in (("stocks", "SPY"), ("crypto", "BTC")):
        crypto = sleeve == "crypto"
        flow = bought = units = 0.0
        for o in filled:
            if ("/" in o.symbol) != crypto:
                continue
            dollars = float(o.filled_qty) * float(o.filled_avg_price)
            bought += dollars if o.side == OrderSide.BUY else 0
            dollars = dollars if o.side == OrderSide.BUY else -dollars
            flow += dollars
            units += dollars / _close_on(closes[sleeve], o.filled_at)
        held = sum(float(p.market_value) for p in positions if (p.asset_class == AssetClass.CRYPTO) == crypto)
        result["sleeves"][sleeve] = {
            "benchmark": name, "bought": bought,
            "bot": held - flow, "bench": units * float(closes[sleeve].iloc[-1]) - flow,
        }
    return result


def get_benchmark():
    """benchmark() on live data, or None when orders or prices can't be fetched, so it never breaks a report."""
    try:
        client = get_trading_client()
        orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500))
        return benchmark(orders, client.get_all_positions(), {"stocks": _closes("SPY"), "crypto": _closes("BTC-USD")})
    except Exception as e:
        print(f"benchmark unavailable ({e})")
        return None


def build_report(account, positions, events, bench=None, top_holdings=10):
    equity, last = float(account.equity), float(account.last_equity)
    diff = equity - last
    lines = [
        "Helm daily report",
        f"Equity: ${equity:,.2f}  (yesterday ${last:,.2f}, {diff:+,.2f} / {diff / last:+.2%})",
        "",
        "Today's moves:",
        *(f"- {e}" for e in events or ["none"]),
        "",
        *_benchmark_lines(bench),
        f"Holdings ({len(positions)}, top {top_holdings} by value):",
    ]
    for p in sorted(positions, key=lambda p: float(p.market_value), reverse=True)[:top_holdings]:
        lines.append(f"- {p.symbol}: ${float(p.market_value):,.2f} ({float(p.unrealized_plpc):+.2%})")
    return "\n".join(lines)


def _benchmark_lines(bench):
    rows = [(k, v) for k, v in (bench or {}).get("sleeves", {}).items() if v["bought"] > 0]
    if not rows:
        return []
    lines = ["Vs buy-and-hold (same dollars, same days):"]
    for sleeve, v in rows:
        lines.append(f"- {sleeve.capitalize()}: bot {usd_signed(v['bot'])} vs {v['benchmark']} {usd_signed(v['bench'])} on ${v['bought']:,.2f} bought")
    return [*lines, ""]


def send_telegram(text):
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not (token and chat_id):
        print("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set, printing report instead:\n" + text)
        return
    # ponytail: truncated to Telegram's 4096-char cap, split into several messages if reports outgrow it
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat_id, "text": text[:4096]},
        timeout=10,
    )
    # not raise_for_status(): its error message embeds the URL, i.e. the bot token
    print("telegram report sent" if r.ok else f"telegram send failed: {r.status_code} {r.text}")


def drain_events():
    events = EVENTS_FILE.read_text().splitlines() if EVENTS_FILE.exists() else []
    EVENTS_FILE.unlink(missing_ok=True)
    return events


def report():
    """Send equity vs yesterday, today's trades with their Kronos readings, and holdings to Telegram."""
    client = get_trading_client()
    send_telegram(build_report(client.get_account(), client.get_all_positions(), drain_events(), bench=get_benchmark()))


def stoploss_alert():
    """One stop-loss pass for frequent scheduled runs: message Telegram right away if anything was sold
    (a separate job's notes never reach the daily report, so it has to speak for itself)."""
    check_stop_losses()
    if events := drain_events():
        send_telegram("Stop-loss triggered\n" + "\n".join(f"- {e}" for e in events))


def dashboard_state():
    client = get_trading_client()
    account = client.get_account()
    orders = client.get_orders(GetOrdersRequest(status=QueryOrderStatus.ALL, limit=50))
    return {
        "paper": not is_live(),
        "paused": is_paused(),
        "equity": float(account.equity),
        "last_equity": float(account.last_equity),
        "cash": float(account.cash),
        "positions": [
            {"symbol": p.symbol, "crypto": p.asset_class == AssetClass.CRYPTO, "qty": float(p.qty), "market_value": float(p.market_value),
             "pl": float(p.unrealized_pl), "plpc": float(p.unrealized_plpc)}
            for p in client.get_all_positions()
        ],
        "orders": [
            {"time": o.submitted_at.isoformat(), "symbol": o.symbol, "side": o.side.value, "status": o.status.value,
             "notional": o.notional, "qty": o.qty, "filled_avg_price": o.filled_avg_price}
            for o in orders
        ],
    }


BYE_GRACE = 8    # seconds the server waits after the page says goodbye (a reload sends a request straight after)
IDLE_EXIT = 300  # ...or exits after this long with no requests at all, in case the goodbye never arrives


class Dashboard(BaseHTTPRequestHandler):
    last_seen = time.time()
    bye_at = None

    def _reply(self, code, body, ctype="application/json"):
        data = body.encode() if isinstance(body, str) else json.dumps(body, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.end_headers()
        self.wfile.write(data)

    def _guard(self):
        """Localhost-only Host header (blocks DNS rebinding). POSTs must carry our custom header (a cross-origin
        page can't send it without a CORS preflight we never answer) or, for the page's goodbye beacon which can't
        set headers, a same-origin Origin (browsers don't let other sites forge it)."""
        host = self.headers.get("Host", "")
        if host.split(":")[0] not in ("127.0.0.1", "localhost"):
            self._reply(403, {"error": "bad host"})
        elif self.command == "POST" and self.headers.get("X-Requested-With") != "dashboard" and self.headers.get("Origin") != f"http://{host}":
            self._reply(403, {"error": "cross-site request refused"})
        else:
            Dashboard.last_seen, Dashboard.bye_at = time.time(), None  # any request cancels a pending goodbye
            return True

    def do_GET(self):
        if not self._guard():
            return
        if self.path == "/":
            self._reply(200, DASHBOARD_FILE.read_text(), "text/html; charset=utf-8")
        elif self.path == "/api/state":
            self._api(dashboard_state)
        elif self.path == "/api/benchmark":
            self._api(lambda: {"benchmark": get_benchmark()})
        else:
            self._reply(404, {"error": "not found"})

    def do_POST(self):
        if not self._guard():
            return
        if self.path == "/api/bye":
            Dashboard.bye_at = time.time() + BYE_GRACE
            self._reply(200, {"ok": True})
        elif self.path == "/api/pause":
            paused = bool(json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0)) or 0) or "{}").get("paused"))
            self._api(lambda: set_paused(paused))
        elif self.path.startswith("/api/close/"):
            symbol = self.path[len("/api/close/"):]
            self._api(lambda: get_trading_client().close_position(symbol))  # closes only what's actually held
        else:
            self._reply(404, {"error": "not found"})

    def _api(self, fn):
        try:
            self._reply(200, fn() or {"ok": True})
        except Exception as e:
            self._reply(500, {"error": getattr(e, "stderr", None) or str(e)})


def _restart_when_changed(path, interval=1.0):
    """Re-exec this process when `path` changes, so edits to bot.py show up without restarting the dashboard by hand."""
    last = path.stat().st_mtime
    while True:
        time.sleep(interval)
        try:
            changed = path.stat().st_mtime != last
        except FileNotFoundError:  # editor mid-save
            continue
        if changed:
            print(f"{path.name} changed, restarting dashboard", flush=True)
            os.execv(sys.executable, [sys.executable, *sys.argv])


def _exit_when_closed():
    """Exit once the page has said goodbye (and not come back), or has been silent for IDLE_EXIT seconds."""
    while True:
        time.sleep(1)
        now = time.time()
        if (Dashboard.bye_at and now > Dashboard.bye_at) or now - Dashboard.last_seen > IDLE_EXIT:
            print("dashboard closed, shutting down", flush=True)
            os._exit(0)


def web(port=8000, exit_when_closed=False):
    # ponytail: 127.0.0.1 only and no login — fine for a single-user machine, add auth before ever exposing it
    # ponytail: two tabs open = closing one stops the server for both (poll interval 30s > BYE_GRACE)
    print(f"dashboard at http://localhost:{port} ({'LIVE' if is_live() else 'paper'} account)", flush=True)
    threading.Thread(target=_restart_when_changed, args=(Path(__file__),), daemon=True).start()
    if exit_when_closed:
        threading.Thread(target=_exit_when_closed, daemon=True).start()
    ThreadingHTTPServer(("127.0.0.1", port), Dashboard).serve_forever()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["signal", "backtest", "trade", "recommend", "rebalance", "auto", "watch", "stoploss", "report", "web"])
    parser.add_argument("tickers", nargs="*", help="e.g. AAPL BHP.AX BBCA.JK (default: all configured markets)")
    parser.add_argument("--qty", type=int, default=1, help="shares per order in trade mode")
    parser.add_argument("--top-n", type=int, help="recommend: how many BUY/SELL picks to show (default 10); auto: how many to buy (default 5)")
    parser.add_argument("--limit", type=int, help="recommend/auto: cap how many S&P 500 tickers to scan (default: all ~500)")
    parser.add_argument("--split", type=int, default=4, help="rebalance mode: how many tickers to spread reinvestment across")
    parser.add_argument("--notional", type=float, default=2.0, help="auto mode: dollars to spend per position (default 5x$2 = $10; crypto orders need >= $10)")
    parser.add_argument("--budget", type=float, help="auto mode: stop buying once this many dollars are held across all positions (default: no cap)")
    parser.add_argument("--crypto", action="store_true", help="auto/recommend: scan the full tradable crypto marketplace instead of S&P 500 stocks")
    parser.add_argument("--port", type=int, default=8000, help="web mode: port for the local dashboard (default 8000)")
    parser.add_argument("--exit-when-closed", action="store_true", help="web mode: shut the server down when the page is closed (what Helm.app uses)")
    parser.add_argument("--interval", type=int, default=60, help="watch mode: seconds between stop-loss checks (default 60)")
    args = parser.parse_args()

    if args.mode == "watch":
        watch_stop_losses(interval=args.interval)
    elif args.mode == "stoploss":
        stoploss_alert()
    elif args.mode == "report":
        report()
    elif args.mode == "web":
        web(port=args.port, exit_when_closed=args.exit_when_closed)
    elif args.mode == "recommend":
        tickers = args.tickers or (crypto_tickers() if args.crypto else sp500_tickers())[: args.limit]
        recommend(tickers, top_n=args.top_n or 10)
    elif args.mode == "rebalance":
        rebalance(pool=args.tickers or None, split=args.split)
    elif args.mode == "auto":
        tickers = args.tickers or (crypto_tickers() if args.crypto else sp500_tickers())[: args.limit]
        auto_trade(tickers, top_n=args.top_n or 5, notional=args.notional, budget=args.budget, crypto=args.crypto)
    else:
        tickers = args.tickers or [t for ts in MARKETS.values() for t in ts]

        for ticker in tickers:
            if args.mode == "signal":
                direction, change = signal(ticker)
                print(f"{ticker}: {direction} (forecast {change:+.2%})")
            elif args.mode == "backtest":
                backtest(ticker)
            else:
                if ticker not in MARKETS["US"] and not is_crypto(ticker):
                    print(f"{ticker}: skipped, Alpaca only trades US-listed stocks or crypto")
                    continue
                direction, change = signal(ticker)
                if direction == "HOLD":
                    print(f"{ticker}: HOLD, no order placed")
                    continue
                order = place_order(ticker, direction, args.qty)
                print(f"{ticker}: {direction} (forecast {change:+.2%}) -> order {order.id}")
