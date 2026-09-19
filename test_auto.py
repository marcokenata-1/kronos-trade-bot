"""assert-based self-check: auto_trade skips held tickers, respects --budget, and survives a rejected order."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def run(results, positions, budget=None, reject=(), notional=10.0, tickers=None, orders=(), crypto=False):
    bought = []

    def fake_order(ticker, side, notional):
        if ticker in reject:
            raise ValueError("insufficient buying power")
        bought.append(ticker)
        return SimpleNamespace(id=ticker)

    scanned = []
    cancelled = []
    client = SimpleNamespace(get_all_positions=lambda: positions, get_orders=lambda req: list(orders),
                             cancel_order_by_id=cancelled.append)
    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "check_stop_losses"), patch.object(bot, "check_signal_exits"), \
         patch.object(bot, "check_drawdown_halt", return_value=False), \
         patch.object(bot, "scan", side_effect=lambda ts: scanned.append(list(ts)) or [r for r in results if r[0] in ts]), \
         patch.object(bot, "place_order", side_effect=fake_order):
        bot.auto_trade(list(tickers or [r[0] for r in results]), top_n=5, notional=notional, budget=budget, crypto=crypto)
    run.scanned, run.cancelled = scanned, cancelled
    return bought


def check_exit_scope():
    """each daily run re-forecasts only its own asset class"""
    positions = [SimpleNamespace(symbol="AAPL", asset_class=bot.AssetClass.US_EQUITY),
                 SimpleNamespace(symbol="BTCUSD", asset_class=bot.AssetClass.CRYPTO)]
    for crypto, expected in ((False, ["AAPL"]), (True, ["BTC-USD"])):
        forecast = []
        with patch.object(bot, "get_trading_client", return_value=SimpleNamespace(get_all_positions=lambda: positions)), \
             patch.object(bot, "signal", side_effect=lambda t: forecast.append(t) or ("HOLD", 0.0)):
            bot.check_signal_exits(crypto)
        assert forecast == expected, (crypto, forecast)


def check_pending_orders():
    """queued buys count as held; unfilled crypto buys are cancelled after 24h and skipped this run"""
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)

    def order(id, symbol, hours_old, side=bot.OrderSide.BUY):
        return SimpleNamespace(id=id, symbol=symbol, side=side, notional="10", submitted_at=now - timedelta(hours=hours_old))

    results = [("AAA", "BUY", 0.09), ("BBB", "BUY", 0.08)]
    assert run(results, [], orders=[order("q1", "AAA", 60)]) == ["BBB"], "AAA is queued (e.g. over the weekend), don't buy it twice"
    assert run.cancelled == [], "stale STOCK buys are legitimately waiting for the open"

    coin = [("WIF-USD", "BUY", 0.5), ("ETH-USD", "BUY", 0.4), ("SOL-USD", "BUY", 0.3)]
    stuck, fresh = order("old", "WIF/USD", 30), order("new", "SOL/USD", 1)
    assert run(coin, [], orders=[stuck, fresh], crypto=True) == ["ETH-USD"], "stuck WIF cancelled and skipped, fresh SOL still pending"
    assert run.cancelled == ["old"], run.cancelled
    assert run(coin, [], orders=[stuck], crypto=False) == ["ETH-USD", "SOL-USD"], "the stock run still treats WIF as pending"
    assert run.cancelled == [], "the stock run must not cancel crypto orders"


def demo():
    buys = [("AAA", "BUY", 0.09), ("BBB", "BUY", 0.08), ("CCC", "BUY", 0.07), ("DDD", "HOLD", 0.0)]
    held_btc = SimpleNamespace(symbol="BTCUSD", asset_class=bot.AssetClass.CRYPTO, market_value="30")

    assert run(buys, []) == ["AAA", "BBB", "CCC"]
    held_aaa = SimpleNamespace(symbol="AAA", asset_class=bot.AssetClass.US_EQUITY, market_value="10")
    assert run(buys, [held_aaa]) == ["BBB", "CCC"], "already-held AAA must not be bought again"

    assert run(buys, [held_aaa], tickers=["AAA", "BBB", "CCC"]) == ["BBB", "CCC"]
    assert run.scanned == [["BBB", "CCC"]], f"held tickers must not be re-forecast: {run.scanned}"

    coin = [("BTC-USD", "BUY", 0.5), ("ETH-USD", "BUY", 0.4)]
    assert run(coin, [held_btc]) == ["ETH-USD"], "crypto held as BTCUSD must match BTC-USD"

    assert run(buys, [], budget=25) == ["AAA", "BBB"], "$10 each: a third buy would pass the $25 cap"
    assert run(buys, [held_btc], budget=45) == ["AAA"], "$30 already held + $10 = $40 ok, next $50 not"

    assert run(buys, [], reject={"AAA"}) == ["BBB", "CCC"], "one rejected order must not stop the rest"
    check_exit_scope()
    check_pending_orders()
    print("ok")


if __name__ == "__main__":
    demo()
