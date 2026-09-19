"""assert-based self-check: notes recorded by a run end up in the Telegram report, then get cleared."""
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot


def demo():
    account = SimpleNamespace(equity="10250", last_equity="10000")
    positions = [
        SimpleNamespace(symbol="AAPL", market_value="12.00", unrealized_plpc="0.05"),
        SimpleNamespace(symbol="BTCUSD", market_value="55.50", unrealized_plpc="-0.02"),
    ]
    client = SimpleNamespace(get_account=lambda: account, get_all_positions=lambda: positions)
    sent = []

    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "get_benchmark", return_value=None), \
         patch.object(bot, "send_telegram", side_effect=sent.append):
        bot.note("BOUGHT $2.00 AAPL: Kronos forecasts +3.20% over the next 10 days")
        bot.report()
        assert not bot.EVENTS_FILE.exists(), "events must be cleared after reporting"
        bot.report()  # a second, empty day

    text = sent[0]
    assert "$10,250.00  (yesterday $10,000.00, +250.00 / +2.50%)" in text, text
    assert "- BOUGHT $2.00 AAPL: Kronos forecasts +3.20%" in text, text
    assert text.index("BTCUSD") < text.index("- AAPL"), "holdings sorted by value"
    assert "- none" in sent[1], sent[1]
    print("ok")


if __name__ == "__main__":
    demo()
