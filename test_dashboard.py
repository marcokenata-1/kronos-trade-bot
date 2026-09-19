"""assert-based self-check: dashboard endpoints, the Close button, and the CSRF/host guard."""
import threading
from datetime import datetime, timezone
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import requests
from alpaca.trading.enums import OrderSide, OrderStatus

import bot


def check_server():
    closed = []
    order = SimpleNamespace(submitted_at=datetime(2026, 1, 2, tzinfo=timezone.utc), symbol="AAPL", side=OrderSide.BUY,
                            status=OrderStatus.FILLED, notional="2", qty=None, filled_avg_price="190.5", id="o1")
    position = SimpleNamespace(symbol="AAPL", asset_class=bot.AssetClass.US_EQUITY, qty="0.01", market_value="2.10", unrealized_pl="0.10", unrealized_plpc="0.05")
    client = SimpleNamespace(
        get_account=lambda: SimpleNamespace(equity="1000", last_equity="990", cash="900"),
        get_orders=lambda req: [order],
        get_all_positions=lambda: [position],
        close_position=closed.append,
        cancel_order_by_id=lambda order_id: None,
    )
    server = ThreadingHTTPServer(("127.0.0.1", 0), bot.Dashboard)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}"
    ok = {"X-Requested-With": "dashboard"}

    with patch.object(bot, "get_trading_client", return_value=client), \
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
    server.shutdown()


def demo():
    check_server()
    print("ok")


if __name__ == "__main__":
    demo()
