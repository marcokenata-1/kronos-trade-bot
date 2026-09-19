"""assert-based self-check: rebalance's default pool includes crypto, and the $10 crypto minimum is enforced."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def run(equity, baseline, paused=False):
    bought = []
    client = SimpleNamespace(get_account=lambda: SimpleNamespace(equity=str(equity)))
    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "BASELINE_FILE", Path(d) / "baseline.json"), \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "get_baseline", return_value=baseline), \
         patch.object(bot, "is_paused", return_value=paused), \
         patch.object(bot, "place_order", side_effect=lambda t, s, notional: bought.append(t) or SimpleNamespace(id=t)):
        bot.rebalance()
    return bought


def demo():
    everything = ["AAPL", "MSFT", "BTC-USD", "ETH-USD"]
    assert run(200_000, 100_000) == everything
    assert run(200, 100) == everything, "$50 / 4 = $12.50 each, clears the crypto minimum"
    assert run(120, 60) == [], "$30 / 4 = $7.50 < $10 crypto minimum, must skip"
    assert run(200_000, 100_000, paused=True) == [], "PAUSED must block the reinvestment buys"
    print("ok")


if __name__ == "__main__":
    demo()
