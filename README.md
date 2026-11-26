# Kalshi 15-Minute BTC Paper Bot

A live **Buy Yes / Buy No / Wait** signal bot for Kalshi’s 15-minute Bitcoin contract (`KXBTC15M` — “BTC price up in the next 15 mins?”). It runs on GitHub Actions every 5 minutes and publishes a dashboard on GitHub Pages.

**Live dashboard:** https://nfm0984-png.github.io/btc-paper-bot/

This bot uses only public market data and simulated money. There are no API keys, no secrets, and no real Kalshi orders.

## What it does

- Reads the live `KXBTC15M` contract (ticker, strike, time left, Yes/No bid and ask) from Kalshi’s public API.
- Pairs it with Coinbase 1-minute and 5-minute BTC/USD candles (Kraken fallback) for EMA + momentum indicators.
- Makes a plain-English call: **BUY YES**, **BUY NO**, **SELL / TAKE PROFIT**, **CUT LOSS**, or **WAIT**.
- Papers a **$100** book with a flat **$5** stake per trade, using Kalshi’s real quadratic taker fee.
- Grades each ticket against Kalshi’s official Yes/No settlement.
- Backtests the same rules on recent settled windows using **real 1-minute Kalshi quotes** plus official settlements — no made-up fills.
- The dashboard polls Coinbase every 20 seconds for a live BTC price and 1-minute indicators. Kalshi’s API blocks browser CORS, so contract quotes and paper-trade grading refresh with each Actions run.

## Strategy (plain English)

**Buy Yes** when all of these are true: 1-minute EMA 9 > 21, close above EMA 50, MACD histogram positive, RSI 52–75, the 5-minute trend agrees, BTC is at least $20 above the strike, Yes is 35¢–72¢, and at least 2 minutes remain.

**Buy No** is the bearish mirror.

**Wait** if the tape is choppy, the quotes are too expensive, or the window is almost over. The bot will not buy an 85¢+ Yes just to be “right.”

**Exit early** on a take-profit (bid ≥ 80¢ or +15¢ from entry), a 12¢ stop, or a momentum + strike flip. Otherwise hold to official settlement.

## How it runs

```text
python3 bot.py
```

No third-party Python packages are required (Python 3.11+). The script writes `data/state.json`, `data/backtest.json`, and `docs/data.json`. `.github/workflows/trade.yml` runs it every 5 minutes and GitHub Pages serves `docs/` from `main`.

## Important

Paper trading only. Not financial advice. Kalshi settles these contracts on CF Benchmarks’ BRTI, which can differ slightly from Coinbase’s last price. A backtest is not a promise of future results.
