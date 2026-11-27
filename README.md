# Kalshi 15-Minute BTC Paper Bot

A live **Buy Yes / Buy No / Wait** signal bot for Kalshi’s 15-minute Bitcoin contract (`KXBTC15M` — “BTC price up in the next 15 mins?”). It runs on GitHub Actions (an hourly job loops `bot.py` every 5 minutes for ~50 minutes), learns from settled windows every hour, and publishes a dashboard on GitHub Pages.

**Live dashboard:** https://nfm0984-png.github.io/btc-paper-bot/

This bot uses only public market data and simulated money. There are no API keys, no secrets, and no real Kalshi orders.

## What it does

- Reads the live `KXBTC15M` contract (ticker, strike, time left, Yes/No bid and ask) from Kalshi’s public API.
- Pairs it with Coinbase 1-minute and 5-minute BTC/USD candles (Kraken fallback) for EMA + momentum indicators.
- Makes a plain-English call: **BUY YES**, **BUY NO**, **SELL / TAKE PROFIT**, **CUT LOSS**, or **WAIT**.
- Papers a **$100** book with a flat **$4 / $6 / $8** stake by signal strength (**no 2× press**). Every ticket’s total cost, including Kalshi’s real quadratic taker fee, stays between **$4 and $8**.
- Grades each ticket against Kalshi’s official Yes/No settlement.
- Can run a separate **1-hour $40 paper session** (the completed run is archived on the Trades tab) alongside the ongoing $100 book, with a live countdown and a check log of every 5-minute scan.
- Runs a live **Head-to-Head $100 Race** on the dashboard: **Version 1 · Active Multi-Trade** (up to 3 tickets in the same 15-minute window, one new entry per 5-minute check) vs **Version 2 · Strict Rules** (the learned `strategy_config.json`, at most one trade per window, only when every filter passes). Both books run a $100 bankroll (earlier $40 race books were lifted by adding cash so past trades and P&L stayed the same), with flat $4–$8 tickets (no press). Version 1 also requires a 35¢–75¢ ask, a $20 minimum strike gap, and will not fade price location (Yes only above the strike, No only below). The Trades tab shows the complete Version 1 and Version 2 trade log (no cap), plus the completed 1-hour $40 run and the $100 book.
- Backtests the same rules on recent settled windows using **real 1-minute Kalshi quotes** plus official settlements — no made-up fills.
- **Learns every hour** (`learn.py`, `.github/workflows/learn.yml`) on GitHub Actions ($0 Grok Bot usage). It keeps a rolling 7–14 day history of settled windows, walk-forward tests new parameter sets (train on the older ~70%, validate on the newest ~30%), and only promotes a new `data/strategy_config.json` version when unseen validation dollar P&L is strictly higher than the live config by at least $1.50, overall P&L is positive, drawdown is acceptable, and a 3-hour promotion cooldown has passed — never just because profit factor rose.
- The dashboard polls Coinbase every 20 seconds for a live BTC price and 1-minute indicators. Kalshi’s API blocks browser CORS, so contract quotes and paper-trade grading refresh with each Actions run.

## Strategy (plain English)

Starting rules (**v1**): **Buy Yes** when all of these are true: 1-minute EMA 9 > 21, close above EMA 50, MACD histogram positive, RSI 52–75, the 5-minute trend agrees, BTC is at least $20 above the strike, Yes is 35¢–72¢, and at least 2 minutes remain. **Buy No** is the bearish mirror. The live bot reads the current band and filters from `data/strategy_config.json`, so the hourly learner can tighten or loosen those numbers when unseen results actually improve.

**Wait** if the tape is choppy, the quotes are too expensive, or the window is almost over. The bot will not buy a very expensive Yes just to be “right.”

**Exit early** on a take-profit (bid ≥ 80¢ or +15¢ from entry), a 12¢ stop, or a momentum + strike flip. Otherwise hold to official settlement.

## How it runs

```text
python3 bot.py
```

No third-party Python packages are required (Python 3.11+). The paper-trade loop (`python3 bot.py` every 5 minutes inside the hourly Actions job) writes `data/state.json`, `data/backtest.json`, and `docs/data.json`. The hourly learner (`python3 learn.py`) writes `data/strategy_config.json`, `data/history_windows.json`, `data/learning_log.json`, and merges a `learning` payload into the dashboard — it never resets the paper books. `.github/workflows/trade.yml` and `learn.yml` use separate concurrency groups and rebase-retry on push so they don’t clobber each other. GitHub Pages serves `docs/` from `main`.

## Important

Paper trading only. Not financial advice. Kalshi settles these contracts on CF Benchmarks’ BRTI, which can differ slightly from Coinbase’s last price. A backtest is not a promise of future results.
