"""assert-based self-check for scan()'s parallel fetch + per-ticker error isolation."""
from unittest.mock import patch

import bot


def demo():
    def fake_fetch(ticker):
        if ticker == "BAD":
            raise ValueError("no data")
        return f"df-for-{ticker}"

    def fake_signal(ticker, df=None):
        assert df == f"df-for-{ticker}"
        return "BUY", 0.05

    with patch.object(bot, "fetch_ohlcv", side_effect=fake_fetch), \
         patch.object(bot, "signal", side_effect=fake_signal):
        results = bot.scan(["AAA", "BAD", "CCC"])

    assert [r[0] for r in results] == ["AAA", "CCC"], results
    assert all(r[1] == "BUY" for r in results)
    print("ok")


if __name__ == "__main__":
    demo()
