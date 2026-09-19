"""assert-based self-check: one failed close_position must not abort the exit loop, and a position's pending BUYs are
cancelled (before the close) so Alpaca doesn't reject the sell."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def demo():
    class FakeClient:
        def __init__(self):
            self.closed, self.events = [], []

        def get_orders(self, req):
            def order(id, symbol, side):
                return SimpleNamespace(id=id, symbol=symbol, side=side)
            return [order("buy-OK", "OK", bot.OrderSide.BUY), order("sell-OK", "OK", bot.OrderSide.SELL),
                    order("buy-OTHER", "OTHER", bot.OrderSide.BUY)]

        def cancel_order_by_id(self, order_id):
            self.events.append(("cancel", order_id))

        def get_all_positions(self):
            return [
                SimpleNamespace(symbol="FAIL", asset_class=bot.AssetClass.US_EQUITY, unrealized_plpc="-0.5"),
                SimpleNamespace(symbol="OK", asset_class=bot.AssetClass.US_EQUITY, unrealized_plpc="-0.5"),
            ]

        def close_position(self, symbol):
            if symbol == "FAIL":
                raise ValueError("insufficient qty available for order")
            self.closed.append(symbol)
            self.events.append(("close", symbol))

    client = FakeClient()
    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client):
        bot.check_stop_losses()  # must not raise despite FAIL's close_position blowing up

    assert client.closed == ["OK"], client.closed
    assert client.events == [("cancel", "buy-OK"), ("close", "OK")], client.events  # only OK's BUY, and before the close
    print("ok")


if __name__ == "__main__":
    demo()
