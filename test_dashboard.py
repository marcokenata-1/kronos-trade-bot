"""assert-based self-check: dashboard endpoints + CSRF/host guard, and set_paused's git round-trip against a throwaway remote."""
import os
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import requests
from alpaca.trading.enums import OrderSide, OrderStatus

import bot


def check_server():
    closed, paused_calls = [], []
    order = SimpleNamespace(submitted_at=datetime(2026, 1, 2, tzinfo=timezone.utc), symbol="AAPL", side=OrderSide.BUY,
                            status=OrderStatus.FILLED, notional="2", qty=None, filled_avg_price="190.5")
    position = SimpleNamespace(symbol="AAPL", asset_class=bot.AssetClass.US_EQUITY, qty="0.01", market_value="2.10", unrealized_pl="0.10", unrealized_plpc="0.05")
    client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(equity="1000", last_equity="990", cash="900"),
        get_orders=lambda req: [order],
        get_all_positions=lambda: [position],
        close_position=closed.append,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Dashboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    ok = {"X-Requested-With": "dashboard"}

    with patch.object(bot, "get_trading_client", return_value=client), \
         patch.object(bot, "is_paused", return_value=False), \
         patch.object(bot, "set_paused", side_effect=paused_calls.append), \
         patch.object(bot, "get_benchmark", return_value={"sleeves": {}, "since": None}):
        state = requests.get(url + "/api/state").json()
        assert state["equity"] == 1000 and state["paper"] and state["positions"][0]["symbol"] == "AAPL", state
        assert state["orders"][0]["side"] == "buy" and state["orders"][0]["status"] == "filled", state
        assert state["cash"] == 900, state
        assert requests.get(url + "/api/benchmark").json() == {"benchmark": {"sleeves": {}, "since": None}}
        assert "<title>Helm" in requests.get(url + "/").text

        assert requests.get(url + "/api/state", headers={"Host": "evil.example"}).status_code == 403
        assert requests.post(url + "/api/close/AAPL").status_code == 403, "POST without the custom header must be refused"
        assert closed == []
        assert requests.post(url + "/api/close/AAPL", headers=ok).status_code == 200
        assert closed == ["AAPL"], closed
        # the page's goodbye beacon can't set headers, so it is allowed by same-origin Origin instead
        assert requests.post(url + "/api/bye", headers={"Origin": "http://evil.example"}).status_code == 403
        assert bot.Dashboard.bye_at is None
        assert requests.post(url + "/api/bye", headers={"Origin": f"http://127.0.0.1:{server.server_port}"}).status_code == 200
        assert bot.Dashboard.bye_at is not None
        requests.get(url + "/api/state")
        assert bot.Dashboard.bye_at is None, "any later request must cancel the goodbye"
        assert requests.post(url + "/api/pause", headers=ok, json={"paused": True}).status_code == 200
        assert paused_calls == [True], paused_calls
    server.shutdown()


def check_set_paused():
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}

    def git(cwd, *args):
        return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout.strip()

    with tempfile.TemporaryDirectory() as d:
        remote, work = Path(d) / "remote.git", Path(d) / "work"
        git(d, "init", "--bare", "-b", "main", str(remote))
        git(d, "clone", str(remote), str(work))
        git(work, "checkout", "-b", "main")
        (work / "README").write_text("x")
        git(work, "add", "README")
        git(work, "commit", "-m", "init")
        git(work, "push", "-u", "origin", "main")

        env_patch = patch.dict(os.environ, env)
        with env_patch, patch.object(bot, "PAUSE_FILE", work / "PAUSED"):
            bot.set_paused(True)
            assert bot.is_paused() and "PAUSED" in git(remote, "ls-tree", "--name-only", "main"), "pause must reach the remote"
            bot.set_paused(True)  # already paused: no-op, no empty commit
            assert git(remote, "rev-list", "--count", "main") == "2"
            bot.set_paused(False)
            assert not bot.is_paused() and "PAUSED" not in git(remote, "ls-tree", "--name-only", "main")

            git(work, "checkout", "-b", "feature")
            try:
                bot.set_paused(True)
                raise AssertionError("must refuse to pause from a non-main branch")
            except RuntimeError:
                pass


def demo():
    check_server()
    check_set_paused()
    print("ok")


if __name__ == "__main__":
    demo()
