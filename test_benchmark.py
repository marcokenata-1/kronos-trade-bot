"""assert-based self-check: bot P/L vs the same dollars put into SPY/BTC on the same days."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pandas as pd

import bot


def order(symbol, side, dollars, day):
    return SimpleNamespace(symbol=symbol, side=side, filled_qty="1", filled_avg_price=str(dollars),
                           filled_at=datetime(2026, 1, day, 15, tzinfo=timezone.utc))


def demo():
    days = pd.date_range("2026-01-01", "2026-01-10")
    closes = {  # SPY doubles from 100 on the 2nd to 110 by the end; BTC flat
        "stocks": pd.Series([100.0] * 5 + [110.0] * 5, index=days),
        "crypto": pd.Series([50.0] * 10, index=days),
    }
    orders = [order("AAPL", bot.OrderSide.BUY, 100, 2), order("BTC/USD", bot.OrderSide.BUY, 10, 2),
              order("BTC/USD", bot.OrderSide.SELL, 4, 6),
              SimpleNamespace(symbol="MSFT", side=bot.OrderSide.BUY, filled_qty="0", filled_avg_price=None, filled_at=None)]  # unfilled: ignored
    positions = [SimpleNamespace(symbol="AAPL", asset_class=bot.AssetClass.US_EQUITY, market_value="105", unrealized_plpc="0.05"),
                 SimpleNamespace(symbol="BTCUSD", asset_class=bot.AssetClass.CRYPTO, market_value="7", unrealized_plpc="0.1")]

    b = bot.benchmark(orders, positions, closes)
    stocks, crypto = b["sleeves"]["stocks"], b["sleeves"]["crypto"]
    assert stocks["bought"] == 100 and abs(stocks["bot"] - 5) < 1e-9 and abs(stocks["bench"] - 10) < 1e-9, stocks
    # crypto: bought 10, sold 4 -> net 6 in; holds 7 -> bot +1; BTC flat -> bench 0
    assert crypto["bought"] == 10 and abs(crypto["bot"] - 1) < 1e-9 and abs(crypto["bench"]) < 1e-9, crypto
    assert str(b["since"]) == "2026-01-02", b["since"]

    text = bot.build_report(SimpleNamespace(equity="100", last_equity="100"), positions, [], bench=b)
    assert "- Stocks: bot +$5.00 vs SPY +$10.00 on $100.00 bought" in text, text
    assert "- Crypto: bot +$1.00 vs BTC +$0.00 on $10.00 bought" in text, text
    print("ok")


if __name__ == "__main__":
    demo()
