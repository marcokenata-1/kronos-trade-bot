"""assert-based self-check: auto_trade skips held tickers, respects --budget, and survives a rejected order."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def run(results, positions, budget=None, reject=(), notional=10.0):
    bought = []

    def fake_order(ticker, side, notional):
        if ticker in reject:
            raise ValueError("insufficient buying power")
        bought.append(ticker)
        return SimpleNamespace(id=ticker)

    client = SimpleNamespace(get_all_positions=lambda: positions)
    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "check_stop_losses"), patch.object(bot, "check_signal_exits"), \
         patch.object(bot, "check_drawdown_halt", return_value=False), \
         patch.object(bot, "is_paused", return_value=False), \
         patch.object(bot, "scan", return_value=results), \
         patch.object(bot, "place_order", side_effect=fake_order):
        bot.auto_trade(["x"], top_n=5, notional=notional, budget=budget)
    return bought


def demo():
    buys = [("AAA", "BUY", 0.09), ("BBB", "BUY", 0.08), ("CCC", "BUY", 0.07), ("DDD", "HOLD", 0.0)]
    held_btc = SimpleNamespace(symbol="BTCUSD", asset_class=bot.AssetClass.CRYPTO, market_value="30")

    assert run(buys, []) == ["AAA", "BBB", "CCC"]
    held_aaa = SimpleNamespace(symbol="AAA", asset_class=bot.AssetClass.US_EQUITY, market_value="10")
    assert run(buys, [held_aaa]) == ["BBB", "CCC"], "already-held AAA must not be bought again"

    coin = [("BTC-USD", "BUY", 0.5), ("ETH-USD", "BUY", 0.4)]
    assert run(coin, [held_btc]) == ["ETH-USD"], "crypto held as BTCUSD must match BTC-USD"

    assert run(buys, [], budget=25) == ["AAA", "BBB"], "$10 each: a third buy would pass the $25 cap"
    assert run(buys, [held_btc], budget=45) == ["AAA"], "$30 already held + $10 = $40 ok, next $50 not"

    assert run(buys, [], reject={"AAA"}) == ["BBB", "CCC"], "one rejected order must not stop the rest"
    print("ok")


if __name__ == "__main__":
    demo()
