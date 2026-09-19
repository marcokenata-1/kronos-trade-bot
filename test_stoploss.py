"""assert-based self-check: importing bot doesn't load torch, and a stop-loss sale alerts Telegram immediately."""
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import bot

assert "torch" not in sys.modules, "stop-loss runs must not need torch installed"


def run(plpc):
    sent, closed = [], []
    client = SimpleNamespace(
        get_all_positions=lambda: [SimpleNamespace(symbol="ARBUSD", unrealized_plpc=str(plpc))],
        close_position=closed.append,
    )
    with tempfile.TemporaryDirectory() as d, \
         patch.object(bot, "EVENTS_FILE", Path(d) / "events.log"), \
         patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "send_telegram", side_effect=sent.append):
        bot.stoploss_alert()
        assert not bot.EVENTS_FILE.exists(), "events must be cleared after alerting"
    return sent, closed


def demo():
    sent, closed = run(-0.25)
    assert closed == ["ARBUSD"] and len(sent) == 1 and "Stop-loss triggered" in sent[0] and "ARBUSD" in sent[0], (closed, sent)
    sent, closed = run(-bot.STOP_LOSS_PCT / 2)
    assert closed == [] and sent == [], "no alert when nothing was sold"
    print("ok")


if __name__ == "__main__":
    demo()
