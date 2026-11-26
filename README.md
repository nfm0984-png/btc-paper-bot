# BTC Paper Bot

A long-only Bitcoin (BTC/USD) **paper-trading** bot that follows an EMA + momentum trend strategy. It runs automatically on GitHub Actions every 30 minutes and publishes a live dashboard on GitHub Pages.

**Live dashboard:** https://nfm0984-png.github.io/btc-paper-bot/

This bot uses only public market data and simulated money. There are no API keys, no secrets, and no real orders.

## What it does

- Pulls live hourly BTC/USD candles from the Coinbase Exchange public API, with Kraken as a fallback.
- Enters a paper long when a confirmed uptrend lines up with healthy momentum.
- Exits on a trend roll-over, a −3% hard stop, or a trailing stop once the trade is up +2%.
- Charges a realistic **0.10% fee + 0.05% slippage (0.15% per side)** on every buy and sell.
- Starts with **$1,000** of paper cash and uses 95% of available cash per trade.
- Keeps a rolling 30–90 day backtest of the exact same rules, clearly separated from the live paper book.

## Strategy (plain English)

**Buy when flat** if all of these are true on a *closed* 1-hour candle:

- EMA 12 is above EMA 26
- Close is above EMA 50
- MACD histogram (12, 26, 9) is positive
- RSI 14 is between 52 and 72

**Sell back to cash** if any of these happen:

- EMA 12 crosses back below EMA 26 on a closed hour
- Price drops 3.0% below the entry fill
- After a +2.0% unrealized gain, price falls 2.5% off the high since entry

## How it runs

```text
python3 bot.py
```

No third-party Python packages are required (Python 3.11+). The script writes:

- `data/state.json` — live paper book (cash, open position, closed trades, equity)
- `data/backtest.json` — historical test of the same rules
- `docs/data.json` — dashboard payload
- `docs/index.html` — the GitHub Pages site

`.github/workflows/trade.yml` runs that script every 30 minutes, commits any book updates, and GitHub Pages serves `docs/` from `main`.

## Important

Paper trading only. Not financial advice. A backtest is not a promise of future results. Fees, slippage, and missed fills can look better or worse in live markets than they do in a historical replay.
