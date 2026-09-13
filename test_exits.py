"""assert-based self-check: one failed close_position must not abort the exit loop."""
from types import SimpleNamespace
from unittest.mock import patch

import bot


def demo():
    class FakeClient:
        def __init__(self):
            self.closed = []

        def get_all_positions(self):
            return [
                SimpleNamespace(symbol="FAIL", asset_class=bot.AssetClass.US_EQUITY, unrealized_plpc="-0.5"),
                SimpleNamespace(symbol="OK", asset_class=bot.AssetClass.US_EQUITY, unrealized_plpc="-0.5"),
            ]

        def close_position(self, symbol):
            if symbol == "FAIL":
                raise ValueError("insufficient qty available for order")
            self.closed.append(symbol)

    client = FakeClient()
    with patch.object(bot, "get_trading_client", return_value=client):
        bot.check_stop_losses()  # must not raise despite FAIL's close_position blowing up

    assert client.closed == ["OK"], client.closed
    print("ok")


if __name__ == "__main__":
    demo()
