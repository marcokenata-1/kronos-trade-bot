import io
import os
import sys
import json
import time
import argparse
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest, GetAssetsRequest
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass

sys.path.insert(0, str(Path(__file__).parent / "vendor" / "kronos"))
from model import Kronos, KronosTokenizer, KronosPredictor

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
STOP_LOSS_PCT = 0.10  # sell a position down this much from entry, regardless of current signal
DRAWDOWN_HALT_PCT = 0.25  # stop opening new positions once equity is down this much from baseline

BASELINE_FILE = Path(__file__).parent / "baseline.json"

_predictor = None
_trading_client = None


def get_trading_client():
    global _trading_client
    if _trading_client is None:
        # ponytail: paper unless ALPACA_LIVE=1 is explicitly set — no accidental live orders,
        # and paper/live use separate key pairs so a stray var can't cross the two
        live = bool(os.environ.get("ALPACA_LIVE"))
        prefix = "ALPACA_LIVE" if live else "ALPACA_PAPER"
        _trading_client = TradingClient(
            os.environ[f"{prefix}_API_KEY"],
            os.environ[f"{prefix}_SECRET_KEY"],
            paper=not live,
        )
    return _trading_client


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
                print(f"stop-loss: sold {position.symbol} at {plpc:+.2%}")
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


def check_signal_exits():
    """Sell any held position whose freshly re-checked forecast has flipped to SELL."""
    for position in get_trading_client().get_all_positions():
        # positions report crypto as "BTCUSD" (no separator); yfinance wants "BTC-USD"
        ticker = position.symbol[:-3] + "-USD" if position.asset_class == AssetClass.CRYPTO else position.symbol
        try:
            direction, change = signal(ticker)
        except Exception as e:
            print(f"{position.symbol}: could not re-check signal ({e})")
            continue
        if direction == "SELL":
            try:
                get_trading_client().close_position(position.symbol)
                print(f"signal exit: sold {position.symbol} (forecast {change:+.2%})")
            except Exception as e:
                print(f"signal exit: failed to close {position.symbol} ({e})")


def check_drawdown_halt():
    """True if equity has fallen more than DRAWDOWN_HALT_PCT below the stored baseline."""
    baseline = get_baseline()
    if baseline is None:
        return False
    equity = float(get_trading_client().get_account().equity)
    if equity < baseline * (1 - DRAWDOWN_HALT_PCT):
        print(f"HALT: equity ${equity:.2f} is down >{DRAWDOWN_HALT_PCT:.0%} from baseline ${baseline:.2f}, skipping new buys")
        return True
    return False


def rebalance(pool=None, split=4):
    """Once account equity has doubled off the stored baseline, put half the
    profit into small notional buys spread across `pool`, then reset the baseline."""
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

    pool = (pool or MARKETS["US"])[:split]
    reinvest = (equity - baseline) / 2
    per_ticker = reinvest / len(pool)

    if per_ticker < 1:
        print(f"reinvest amount ${reinvest:.2f} too small to split across {len(pool)} tickers, skipping")
        return

    for ticker in pool:
        order = place_order(ticker, "BUY", notional=round(per_ticker, 2))
        print(f"bought ${per_ticker:.2f} of {ticker} -> order {order.id}")

    BASELINE_FILE.write_text(json.dumps({"equity": equity}))
    print(f"baseline reset to ${equity:.2f}")


def get_predictor():
    global _predictor
    if _predictor is None:
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


def auto_trade(tickers, top_n=5, notional=2.0):
    """Exit losers (stop-loss + signal flip), then, unless the drawdown circuit
    breaker is tripped, scan `tickers` and buy $notional of the top_n BUY signals."""
    check_stop_losses()
    check_signal_exits()

    if check_drawdown_halt():
        return

    results = scan(tickers)
    buys = sorted((r for r in results if r[1] == "BUY"), key=lambda r: r[2], reverse=True)[:top_n]

    if not buys:
        print("no BUY signals this run, nothing bought")
        return

    for ticker, direction, change in buys:
        order = place_order(ticker, "BUY", notional=notional)
        print(f"bought ${notional:.2f} of {ticker} (forecast {change:+.2%}) -> order {order.id}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=["signal", "backtest", "trade", "recommend", "rebalance", "auto", "watch"])
    parser.add_argument("tickers", nargs="*", help="e.g. AAPL BHP.AX BBCA.JK (default: all configured markets)")
    parser.add_argument("--qty", type=int, default=1, help="shares per order in trade mode")
    parser.add_argument("--top-n", type=int, help="recommend: how many BUY/SELL picks to show (default 10); auto: how many to buy (default 5)")
    parser.add_argument("--limit", type=int, help="recommend/auto: cap how many S&P 500 tickers to scan (default: all ~500)")
    parser.add_argument("--split", type=int, default=4, help="rebalance mode: how many tickers to spread reinvestment across")
    parser.add_argument("--notional", type=float, default=2.0, help="auto mode: dollars to spend per position (default 5x$2 = $10; crypto orders need >= $10)")
    parser.add_argument("--crypto", action="store_true", help="auto/recommend: scan the full tradable crypto marketplace instead of S&P 500 stocks")
    parser.add_argument("--interval", type=int, default=60, help="watch mode: seconds between stop-loss checks (default 60)")
    args = parser.parse_args()

    if args.mode == "watch":
        watch_stop_losses(interval=args.interval)
    elif args.mode == "recommend":
        tickers = args.tickers or (crypto_tickers() if args.crypto else sp500_tickers())[: args.limit]
        recommend(tickers, top_n=args.top_n or 10)
    elif args.mode == "rebalance":
        rebalance(pool=args.tickers or None, split=args.split)
    elif args.mode == "auto":
        tickers = args.tickers or (crypto_tickers() if args.crypto else sp500_tickers())[: args.limit]
        auto_trade(tickers, top_n=args.top_n or 5, notional=args.notional)
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
