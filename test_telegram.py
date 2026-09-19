"""assert-based self-check: the /status message is a short phone-sized summary, and failures are reported."""
from datetime import date
from types import SimpleNamespace
from unittest.mock import patch

import bot


def pos(symbol, value, plpc, crypto=False):
    return SimpleNamespace(symbol=symbol, market_value=str(value), unrealized_plpc=str(plpc),
                           asset_class=bot.AssetClass.CRYPTO if crypto else bot.AssetClass.US_EQUITY)


def check_status_text():
    account = SimpleNamespace(equity="10250", last_equity="10300", cash="9000")
    positions = [pos("AAPL", 100, 0.05), pos("BTCUSD", 900, -0.02, crypto=True), pos("MSFT", 50, 0.0)]
    orders = [SimpleNamespace(side=bot.OrderSide.BUY, notional="2"), SimpleNamespace(side=bot.OrderSide.BUY, notional="10"),
              SimpleNamespace(side=bot.OrderSide.SELL, notional=None)]
    bench = {"since": date(2026, 8, 13), "sleeves": {"stocks": {"bought": 100, "bot": 5.0, "bench": 1.0},
                                                       "crypto": {"bought": 0, "bot": 0.0, "bench": 0.0}}}
    text = bot.build_status(account, positions, orders, bench, top=2)
    assert "Equity $10,250.00 (-$50.00 since last close)" in text, text
    assert "Ahead of buy-and-hold by $4.00 since Aug 13 (bot +$5.00, hold +$1.00)" in text, text
    assert "Invested $1,050.00 (10.2%), cash $9,000.00" in text, text
    assert text.index("BTC/USD $900.00 -2.00%") < text.index("AAPL $100.00 +5.00%"), "sorted by value, crypto shown as BTC/USD"
    assert "MSFT" not in text, "only the top 2"
    assert "Queued: 2 buys, $12.00" in text, text
    assert len(text.splitlines()) < 14, "must stay phone-sized"
    behind = dict(bench, sleeves={"stocks": {"bought": 100, "bot": 1.0, "bench": 5.0}})
    assert "Behind buy-and-hold by $4.00" in bot.build_status(account, [], [], behind), "behind case"
    assert "none" in bot.build_status(account, [], [], None), "no positions, no benchmark: still works"


def check_send_status():
    sent = []
    client = SimpleNamespace(get_account=lambda: SimpleNamespace(equity="100", last_equity="100", cash="50"),
                             get_all_positions=lambda: [])
    with patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "open_orders", return_value=[]), \
         patch.object(bot, "get_benchmark", return_value=None), \
         patch.object(bot, "send_telegram", side_effect=sent.append):
        bot.send_status()
        assert len(sent) == 1 and sent[0].startswith("Helm status"), sent
        with patch.object(bot, "open_orders", side_effect=RuntimeError("alpaca down")):
            bot.send_status()
    assert sent[1] == "Could not fetch the status: alpaca down", "a failure must reach the user, not vanish"


def demo():
    check_status_text()
    check_send_status()
    print("ok")


if __name__ == "__main__":
    demo()
