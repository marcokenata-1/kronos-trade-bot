# Kronos paper-trading bot

Trades on an [Alpaca](https://alpaca.markets) **paper** account using price forecasts from
[Kronos](https://github.com/shiyu-coder/Kronos), a time-series foundation model for candlesticks.
Runs daily on GitHub Actions, messages you a summary on Telegram, and has a local web dashboard
where you can watch trades, close positions and pause new buys.

![The Kronos dashboard: your trades vs buy-and-hold, equity, and open positions](docs/dashboard.png)

## How it decides

1. Pull ~2 years of daily OHLCV from Yahoo Finance (`yfinance`).
2. Feed the last 400 candles to Kronos-small and forecast the next 10 days (5 sampled paths, averaged).
3. Compare the forecast close on day 10 with the last close:
   - above **+1%** → BUY
   - below **-1%** → SELL
   - otherwise → HOLD

The bot never shorts. SELL only closes a position you already hold.

## The daily run (`python bot.py auto`)

1. **Stop-loss:** close any position down 10% or more from entry.
2. **Signal exit:** re-forecast every held position and close any that flipped to SELL.
3. **Drawdown halt:** if equity is more than 25% below `baseline.json`, skip new buys.
4. **Pause:** if the `PAUSED` file exists, skip new buys (see [Human override](#human-override)).
5. **Scan and buy:** forecast the whole universe and buy `--notional` dollars of each of the top `--top-n` BUY signals
   **you don't already hold**. With `--budget N`, it stops buying once N dollars are held across all positions.

The universe is the S&P 500 (`auto`) or every tradable Alpaca `/USD` crypto pair (`auto --crypto`).
A failure on one ticker (fetch, forecast or order, including a rejected order) is logged and skipped, it doesn't abort the run.

## Profit reinvestment (`python bot.py rebalance`)

The first run stores your equity in `baseline.json`. Once equity reaches **2x the baseline**, half the
profit is spread evenly across AAPL, MSFT, BTC-USD and ETH-USD (crypto orders need at least $10 each),
and the baseline resets to the current equity. Until then `rebalance` does nothing, so **every other buy
in your order history is a regular `auto` buy, funded from cash, not from profits.**

## Modes

| Command | What it does |
|---|---|
| `python bot.py signal [TICKER...]` | Print BUY/SELL/HOLD and the forecast % |
| `python bot.py backtest [TICKER...]` | Walk-forward direction accuracy (20 windows x 10 days), not P&L |
| `python bot.py trade [TICKER...]` | Place market orders from the signals (`--qty` shares each) |
| `python bot.py recommend [--crypto] [--top-n N] [--limit N]` | Show top BUY/SELL picks without trading |
| `python bot.py auto [--crypto] [--notional 2] [--top-n 5] [--budget N] [--limit N]` | The daily run above |
| `python bot.py rebalance [TICKER...] [--split 4]` | Profit reinvestment above |
| `python bot.py watch [--interval 60]` | Loop forever checking stop-losses (no model) |
| `python bot.py report` | Send the Telegram summary |
| `python bot.py web [--port 8000]` | Start the local dashboard |

Alpaca only trades US-listed stocks and crypto, so ASX (`.AX`) and IDX (`.JK`) tickers work for
`signal` and `backtest` only.

## Setup

```bash
git clone --recurse-submodules https://github.com/marcokenata-1/kronos-trade-bot.git
cd kronos-trade-bot
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Create `.env` (gitignored):

```
ALPACA_PAPER_API_KEY=...
ALPACA_PAPER_SECRET_KEY=...
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
```

Python 3.12 is required: `pandas==2.2.2` (pinned by Kronos) has no 3.13 wheel and needs `numpy<2`.
Forecasts run on CPU, since Kronos's attention dropout isn't supported on Apple's MPS backend.

**Paper only by default.** Live trading needs `ALPACA_LIVE=1` plus a separate `ALPACA_LIVE_API_KEY` /
`ALPACA_LIVE_SECRET_KEY` pair. The dashboard shows a red bar and "Live account, real money" when that is set.

Before going live: `baseline.json` holds the **paper** account's equity, so the 25% drawdown halt would block every
buy on a small live account. Give live its own baseline first. Use `--budget` so a small account can't be spent in a day
(crypto orders need at least $10 each).

## Scheduled run (GitHub Actions)

`.github/workflows/daily-trade.yml` runs at 22:00 UTC (08:00 AEST) and on manual dispatch:
`auto`, then `auto --crypto --notional 10`, then `rebalance`, commits `baseline.json`, then `report`
(which always runs, even if an earlier step failed).

Add these as **environment secrets** on the `Paper API Key` environment (Settings > Environments):
`ALPACA_PAPER_API_KEY`, `ALPACA_PAPER_SECRET_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`.
Don't add the live keys there.

`run_daily.sh` is a local cron alternative that runs the same steps and removes itself after 7 days.

## Telegram report

Sent by `python bot.py report` after the daily run:

```
Kronos daily report
Equity: $10,250.00  (yesterday $10,000.00, +250.00 / +2.50%)

Today's moves:
- BOUGHT $2.00 AAPL: Kronos forecasts +3.20% over the next 10 days (BUY above +1%, order ...)
- SOLD XYZ: Kronos forecast flipped to -2.10% over the next 10 days

Vs buy-and-hold (same dollars, same days):
- Stocks: bot -$2.61 vs SPY +$0.09 on $427.96 bought
- Crypto: bot +$50.25 vs BTC +$10.63 on $647.11 bought

Holdings (12, top 10 by value):
- BTCUSD: $55.50 (-2.00%)
```

Each step writes what it did and why to `.daily_events.log` as it goes; `report` reads it, sends it,
and clears it. "Yesterday" is Alpaca's `last_equity` (previous trading-day close). To get your chat ID,
message your bot, then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and look for `chat.id`.
If the variables aren't set, `report` just prints the message.

## Local dashboard

```bash
python bot.py web        # then open http://localhost:8000
```

It leads with one question: **is the bot beating buy-and-hold?** Below that: equity, cash and how much is actually
invested, every open position with its return, and the 50 most recent orders. It refreshes every 30 seconds and
follows your macOS light/dark setting.

**Want it in the Dock like an app?** In Safari, File > Add to Dock. It opens in its own window with its own icon, no
extra software needed.

**Start it once and forget it (macOS):**

```bash
./install_dashboard.sh              # starts at login, restarts if it crashes
./install_dashboard.sh --uninstall  # remove it
launchctl kickstart -k gui/$(id -u)/com.kronos.dashboard   # restart after editing bot.py
```

Edits to `dashboard.html` show up on the next page refresh with no restart. Logs go to `dashboard.log`.

It listens on `127.0.0.1` only, has no login, and refuses requests with a foreign `Host` header or
without its `X-Requested-With` header, so other web pages can't drive it. Don't expose it beyond localhost.

GitHub Pages isn't an option for this: it's static, so it can't hold your API keys or place orders.

### Buy-and-hold benchmark

For every filled order, the same dollars are put into SPY (for stock trades) or BTC (for crypto trades) on the same
day, and both sides are valued at today's price. That isolates whether the picks and timing helped, not just how much
money went in. It uses daily closing prices, ignores fees, and only sees the last 500 orders, so it's a rough guide.
Only a few days of trades says little about the strategy either way.

## Human override

The bot trades on its own; the dashboard lets you step in.

- **Close**: sells that whole position at market.
- **Pause new buys / Resume buying**: creates or deletes the `PAUSED` file, commits and pushes it to
  `main` so the GitHub Actions run sees it. While paused, `auto` and `rebalance` open nothing new, but
  stop-loss and signal exits still run. Run the dashboard from `main`, since the button refuses to push
  from another branch.
  You can do the same by hand (`touch PAUSED`, commit, push) or disable the workflow in GitHub.

## Settings

Constants at the top of `bot.py`:

| Name | Default | Meaning |
|---|---|---|
| `LOOKBACK` / `PRED_LEN` | 400 / 10 | Candles in, days forecast |
| `SIGNAL_THRESHOLD` | 1% | Forecast move needed for BUY/SELL |
| `SAMPLE_COUNT` | 5 | Forecast paths averaged per ticker |
| `STOP_LOSS_PCT` | 10% | Sell a position down this much |
| `DRAWDOWN_HALT_PCT` | 25% | Stop new buys this far below baseline |
| `MARKETS` | see file | Tickers for `signal`, `backtest`, `trade` and the `rebalance` pool |

## Tests

Plain assert scripts, no framework:

```bash
for t in test_*.py; do python $t; done
```

## Known limits

- Stop-loss only runs inside `auto`, so once a day, unless `watch` is running.
- The daily run scans ~500 stocks on a CPU runner, which is slow.
- Yahoo Finance prices are used for signals, Alpaca fills may differ.
- Everything is a market order.
- Nothing here is financial advice, and paper results don't predict live results.
