#!/usr/bin/env python3
"""Kalshi 15-minute BTC paper-trading & signal bot (KXBTC15M).

Public market data only. Simulated money. No API keys.
"""

from __future__ import annotations

import json
import math
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

PT = ZoneInfo("America/Los_Angeles")
UTC = timezone.utc

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
DOCS_DIR = ROOT / "docs"
STATE_PATH = DATA_DIR / "state.json"
BACKTEST_PATH = DATA_DIR / "backtest.json"
DASHBOARD_PATH = DOCS_DIR / "data.json"

SERIES = "KXBTC15M"
KALSHI_API = "https://api.elections.kalshi.com/trade-api/v2"
KALSHI_MARKET_URL = "https://kalshi.com/markets/kxbtc15m/{ticker}"

STARTING_BALANCE = 100.00
STAKE = 5.00
FEE_RATE = 0.07  # Kalshi quadratic taker fee
FEE_MULTIPLIER = 1.0

FAST_EMA = 9
SLOW_EMA = 21
TREND_EMA = 50
RSI_PERIOD = 14
MACD_FAST, MACD_SLOW, MACD_SIGNAL = 12, 26, 9

ASK_MIN = 0.35
ASK_MAX = 0.72
MIN_REMAINING_SEC = 120
MIN_STRIKE_DISTANCE = 20.0  # dollars
EMA_FLAT_DOLLARS = 8.0
RSI_YES_LO, RSI_YES_HI = 52.0, 75.0
RSI_NO_LO, RSI_NO_HI = 25.0, 48.0
TAKE_PROFIT_BID = 0.80
TAKE_PROFIT_GAIN = 0.15
STOP_LOSS_DROP = 0.12
EARLY_EXIT_CUTOFF_SEC = 30
DECISION_OFFSET_SEC = 60  # backtest snapshot: 1 minute after the window opens
BACKTEST_DAYS = 5
BACKTEST_CANDLE_PAD_HOURS = 6

USER_AGENT = "btc-paper-bot/1.1 (+https://github.com/nfm0984-png/btc-paper-bot)"
HTTP_TIMEOUT = 20
MAX_RETRIES = 3


def utc_now() -> datetime:
    return datetime.now(tz=UTC)


def parse_iso(ts: str | int | float | datetime) -> datetime:
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=UTC)
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(int(ts), tz=UTC)
    return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(UTC)


def to_ts(ts: str | int | float | datetime) -> int:
    return int(parse_iso(ts).timestamp())


def fmt_pt(ts: str | int | float | datetime) -> str:
    dt = parse_iso(ts).astimezone(PT)
    hour = dt.strftime("%I").lstrip("0") or "0"
    return f"{dt.strftime('%b')} {dt.day}, {dt.year}, {hour}:{dt.strftime('%M %p')} PT"


def iso_utc(ts: str | int | float | datetime) -> str:
    return parse_iso(ts).astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def round_money(x: float) -> float:
    return round(float(x) + 0.0, 2)


def round_px(x: float, n: int = 2) -> float:
    return round(float(x) + 0.0, n)


def round_cent(x: float) -> float:
    return round(float(x) + 0.0, 4)


def dollars(x: Any) -> Optional[float]:
    if x is None or x == "":
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def cents_label(price: Optional[float]) -> str:
    if price is None:
        return "—"
    return f"{round(price * 100):.0f}¢"


def http_get_json(url: str) -> Any:
    last_err: Optional[Exception] = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, json.JSONDecodeError) as exc:
            last_err = exc
            time.sleep(0.35 * attempt)
    raise RuntimeError(f"GET failed after {MAX_RETRIES} tries: {url} ({last_err})")


# ---------------------------------------------------------------------------
# Kalshi fee + market helpers
# ---------------------------------------------------------------------------

def kalshi_taker_fee(contracts: int, price: float, multiplier: float = FEE_MULTIPLIER) -> float:
    """Official-style quadratic taker fee, rounded up to the next cent.

    fee = ceil(0.07 * multiplier * C * P * (1-P) * 100) / 100
    """
    if contracts <= 0 or price <= 0 or price >= 1:
        return 0.0
    raw = FEE_RATE * multiplier * contracts * price * (1.0 - price)
    return math.ceil(raw * 100.0 - 1e-12) / 100.0


def contract_url(ticker: str) -> str:
    return KALSHI_MARKET_URL.format(ticker=ticker.lower())


# ---------------------------------------------------------------------------
# Market data: Kalshi + Coinbase/Kraken
# ---------------------------------------------------------------------------

def fetch_kalshi_markets(status: str, limit: int = 200, max_pages: int = 20) -> list[dict]:
    out: list[dict] = []
    cursor = None
    pages = 0
    while pages < max_pages:
        pages += 1
        params = {"series_ticker": SERIES, "status": status, "limit": str(limit)}
        if cursor:
            params["cursor"] = cursor
        url = f"{KALSHI_API}/markets?{urllib.parse.urlencode(params)}"
        data = http_get_json(url)
        batch = data.get("markets") or []
        out.extend(batch)
        cursor = data.get("cursor") or ""
        if not cursor or not batch:
            break
        time.sleep(0.12)
    return out


def fetch_kalshi_event(event_ticker: str) -> dict:
    return http_get_json(f"{KALSHI_API}/events/{urllib.parse.quote(event_ticker)}")


def fetch_candlesticks_for_markets(markets: list[dict], first_minutes: int = 3) -> dict[str, list[dict]]:
    """Map market ticker -> 1-minute yes bid/ask candlesticks near the open."""
    by_ticker: dict[str, list[dict]] = {}
    chunk = 15
    for i in range(0, len(markets), chunk):
        group = [m for m in markets[i : i + chunk] if m.get("open_time") and m.get("ticker")]
        if not group:
            continue
        starts = [to_ts(m["open_time"]) for m in group]
        ends = [to_ts(m.get("close_time") or m["open_time"]) for m in group]
        start = min(starts)
        # Keep the window tight around each group's opens so the payload stays small,
        # but always cover each market's first few minutes.
        end = max(s + first_minutes * 60 + 5 for s in starts)
        end = max(end, min(ends))
        tickers = ",".join(m["ticker"] for m in group)
        url = (
            f"{KALSHI_API}/markets/candlesticks?"
            + urllib.parse.urlencode(
                {
                    "market_tickers": tickers,
                    "start_ts": start,
                    "end_ts": end,
                    "period_interval": 1,
                }
            )
        )
        try:
            data = http_get_json(url)
        except Exception as exc:
            print(f"  candlestick batch failed ({exc}); retrying one-by-one", flush=True)
            for m in group:
                try:
                    s = to_ts(m["open_time"])
                    one = http_get_json(
                        f"{KALSHI_API}/markets/candlesticks?"
                        + urllib.parse.urlencode(
                            {
                                "market_tickers": m["ticker"],
                                "start_ts": s,
                                "end_ts": s + first_minutes * 60 + 5,
                                "period_interval": 1,
                            }
                        )
                    )
                    for row in one.get("markets") or []:
                        by_ticker[row.get("ticker") or row.get("market_ticker") or m["ticker"]] = (
                            row.get("candlesticks") or []
                        )
                except Exception as exc2:
                    print(f"  candlesticks missing for {m.get('ticker')}: {exc2}", flush=True)
                time.sleep(0.08)
            continue
        for row in data.get("markets") or []:
            t = row.get("ticker") or row.get("market_ticker")
            if t:
                by_ticker[t] = row.get("candlesticks") or []
        time.sleep(0.12)
    return by_ticker


def quote_at_decision(candles: list[dict], open_ts: int, offset: int = DECISION_OFFSET_SEC) -> Optional[dict]:
    if not candles:
        return None
    target = open_ts + offset
    # Prefer the 1-minute bar that closes at the decision time.
    chosen = None
    for c in candles:
        end = int(c.get("end_period_ts") or 0)
        if end <= target:
            chosen = c
        elif chosen is None and end > open_ts:
            chosen = c
            break
        else:
            break
    if chosen is None:
        chosen = candles[0]
    yes_ask = dollars((chosen.get("yes_ask") or {}).get("close_dollars"))
    yes_bid = dollars((chosen.get("yes_bid") or {}).get("close_dollars"))
    last = dollars((chosen.get("price") or {}).get("close_dollars"))
    if yes_ask is None or yes_bid is None:
        return None
    # Ignore empty-book prints of 0 or 1 at the very open.
    if yes_ask >= 0.99 or yes_ask <= 0.01:
        return None
    return {
        "end_ts": int(chosen.get("end_period_ts") or target),
        "yes_ask": yes_ask,
        "yes_bid": yes_bid,
        "no_bid": max(0.0, min(1.0, 1.0 - yes_ask)),
        "no_ask": max(0.0, min(1.0, 1.0 - yes_bid)),
        "last": last,
    }


def fetch_coinbase_ticker() -> float:
    data = http_get_json("https://api.exchange.coinbase.com/products/BTC-USD/ticker")
    return float(data["price"])


def fetch_kraken_ticker() -> float:
    data = http_get_json("https://api.kraken.com/0/public/Ticker?pair=XBTUSD")
    if data.get("error"):
        raise RuntimeError(f"Kraken ticker error: {data['error']}")
    result = data["result"]
    key = next(k for k in result)
    return float(result[key]["c"][0])


def fetch_live_price() -> tuple[float, str]:
    errors = []
    try:
        return fetch_coinbase_ticker(), "coinbase"
    except Exception as exc:
        errors.append(f"coinbase: {exc}")
    try:
        return fetch_kraken_ticker(), "kraken"
    except Exception as exc:
        errors.append(f"kraken: {exc}")
    raise RuntimeError("Could not fetch a live BTC/USD price. " + " | ".join(errors))


def _norm_cb(row: list) -> dict:
    # Coinbase: [time, low, high, open, close, volume]
    return {
        "time": int(row[0]),
        "open": float(row[3]),
        "high": float(row[2]),
        "low": float(row[1]),
        "close": float(row[4]),
        "volume": float(row[5]),
    }


def fetch_coinbase_candles(start_ts: int, end_ts: int, granularity: int) -> list[dict]:
    candles: dict[int, dict] = {}
    max_span = granularity * 300
    cursor = int(start_ts)
    end_ts = int(end_ts)
    while cursor < end_ts:
        chunk_end = min(cursor + max_span, end_ts)
        start_iso = datetime.fromtimestamp(cursor, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        end_iso = datetime.fromtimestamp(chunk_end, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (
            "https://api.exchange.coinbase.com/products/BTC-USD/candles"
            f"?granularity={granularity}&start={start_iso}&end={end_iso}"
        )
        raw = http_get_json(url)
        if not isinstance(raw, list):
            raise RuntimeError(f"Unexpected Coinbase candles payload: {raw}")
        for row in raw:
            candles[int(row[0])] = _norm_cb(row)
        cursor = chunk_end
        time.sleep(0.12)
    return [candles[k] for k in sorted(candles)]


def fetch_kraken_ohlc(interval_min: int, start_ts: int) -> list[dict]:
    url = f"https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval={interval_min}&since={int(start_ts)}"
    data = http_get_json(url)
    if data.get("error"):
        raise RuntimeError(f"Kraken OHLC error: {data['error']}")
    result = data["result"]
    key = next(k for k in result if k != "last")
    out = []
    for row in result[key]:
        ts = int(row[0])
        if ts < start_ts:
            continue
        out.append(
            {
                "time": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[6]),
            }
        )
    out.sort(key=lambda c: c["time"])
    return out


def fetch_btc_candles(start_ts: int, end_ts: int, granularity: int) -> tuple[list[dict], str]:
    errors = []
    try:
        candles = fetch_coinbase_candles(start_ts, end_ts, granularity)
        if len(candles) >= 80:
            return candles, "coinbase"
        errors.append(f"coinbase returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"coinbase: {exc}")
    try:
        interval = max(1, granularity // 60)
        candles = fetch_kraken_ohlc(interval, start_ts)
        candles = [c for c in candles if c["time"] <= end_ts]
        if len(candles) >= 80:
            return candles, "kraken"
        errors.append(f"kraken returned only {len(candles)} candles")
    except Exception as exc:
        errors.append(f"kraken: {exc}")
    raise RuntimeError(f"Could not fetch {granularity}s BTC/USD candles. " + " | ".join(errors))


# ---------------------------------------------------------------------------
# Indicators
# ---------------------------------------------------------------------------

def series_ema(values: list[Optional[float]], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    k = 2.0 / (period + 1)
    seed_vals: list[float] = []
    seed = None
    for i, v in enumerate(values):
        if v is None:
            continue
        if seed is None:
            seed_vals.append(float(v))
            if len(seed_vals) == period:
                seed = sum(seed_vals) / period
                out[i] = seed
            continue
        seed = (float(v) - seed) * k + seed
        out[i] = seed
    return out


def series_rsi(closes: list[float], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(closes)
    if len(closes) < period + 1:
        return out
    gains = losses = 0.0
    for i in range(1, period + 1):
        ch = closes[i] - closes[i - 1]
        if ch >= 0:
            gains += ch
        else:
            losses -= ch
    avg_gain = gains / period
    avg_loss = losses / period

    def rsi_val(ag: float, al: float) -> float:
        if al == 0:
            return 100.0
        if ag == 0:
            return 0.0
        return 100.0 - (100.0 / (1.0 + ag / al))

    out[period] = rsi_val(avg_gain, avg_loss)
    for i in range(period + 1, len(closes)):
        ch = closes[i] - closes[i - 1]
        g = ch if ch > 0 else 0.0
        l = -ch if ch < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period
        out[i] = rsi_val(avg_gain, avg_loss)
    return out


@dataclass
class Indicators:
    ema9: Optional[float]
    ema21: Optional[float]
    ema50: Optional[float]
    rsi14: Optional[float]
    macd_hist: Optional[float]
    close: Optional[float]
    ready: bool = False


def compute_indicators(candles: list[dict]) -> list[Indicators]:
    closes = [c["close"] for c in candles]
    opts: list[Optional[float]] = list(closes)
    ema9 = series_ema(opts, FAST_EMA)
    ema21 = series_ema(opts, SLOW_EMA)
    ema50 = series_ema(opts, TREND_EMA)
    rsi14 = series_rsi(closes, RSI_PERIOD)
    macd_line = [
        (a - b) if a is not None and b is not None else None for a, b in zip(series_ema(opts, MACD_FAST), series_ema(opts, MACD_SLOW))
    ]
    macd_signal = series_ema(macd_line, MACD_SIGNAL)
    macd_hist = [
        (m - s) if m is not None and s is not None else None for m, s in zip(macd_line, macd_signal)
    ]
    out = []
    for i, c in enumerate(candles):
        ind = Indicators(
            ema9=ema9[i],
            ema21=ema21[i],
            ema50=ema50[i],
            rsi14=rsi14[i],
            macd_hist=macd_hist[i],
            close=c["close"],
        )
        ind.ready = None not in (ind.ema9, ind.ema21, ind.ema50, ind.rsi14, ind.macd_hist)
        out.append(ind)
    return out


def indicators_at(candles: list[dict], series: list[Indicators], ts: int) -> Optional[tuple[dict, Indicators]]:
    """Last fully closed candle at or before ts, plus its indicators."""
    chosen = None
    for i, c in enumerate(candles):
        if int(c["time"]) <= ts:
            chosen = (c, series[i])
        else:
            break
    return chosen


def ind_payload(ind: Optional[Indicators]) -> dict:
    if not ind:
        return {"ema9": None, "ema21": None, "ema50": None, "rsi14": None, "macd_hist": None, "ready": False}
    return {
        "ema9": None if ind.ema9 is None else round_px(ind.ema9, 2),
        "ema21": None if ind.ema21 is None else round_px(ind.ema21, 2),
        "ema50": None if ind.ema50 is None else round_px(ind.ema50, 2),
        "rsi14": None if ind.rsi14 is None else round_px(ind.rsi14, 2),
        "macd_hist": None if ind.macd_hist is None else round_px(ind.macd_hist, 2),
        "ready": ind.ready,
    }


# ---------------------------------------------------------------------------
# Signal engine
# ---------------------------------------------------------------------------

@dataclass
class Signal:
    action: str  # BUY_YES, BUY_NO, WAIT, TAKE_PROFIT, CUT_LOSS
    label: str
    reason: str
    side: Optional[str] = None  # yes / no
    ask: Optional[float] = None
    bid: Optional[float] = None


def _bullish(ind: Indicators, trend: Optional[Indicators]) -> tuple[bool, list[str], list[str]]:
    ok, yes, no = True, [], []
    if not ind.ready:
        return False, [], ["indicators still warming up"]
    if ind.ema9 > ind.ema21:
        yes.append(f"EMA 9 > 21 (${ind.ema9:,.0f} > ${ind.ema21:,.0f})")
    else:
        ok = False
        no.append("EMA 9 is below EMA 21")
    if abs((ind.ema9 or 0) - (ind.ema21 or 0)) < EMA_FLAT_DOLLARS:
        ok = False
        no.append("EMAs are flat — looks choppy")
    if ind.close > ind.ema50:
        yes.append("price above the 50-period EMA")
    else:
        ok = False
        no.append("price is below the 50-period EMA")
    if ind.macd_hist > 0:
        yes.append("MACD histogram is positive")
    else:
        ok = False
        no.append("MACD histogram is negative")
    if RSI_YES_LO <= ind.rsi14 <= RSI_YES_HI:
        yes.append(f"RSI {ind.rsi14:.0f}")
    else:
        ok = False
        if ind.rsi14 > RSI_YES_HI:
            no.append(f"RSI {ind.rsi14:.0f} looks overheated")
        else:
            no.append(f"RSI {ind.rsi14:.0f} is short of the 52 momentum mark")
    if trend and trend.ready:
        if trend.ema9 >= trend.ema21:
            yes.append("5-minute trend agrees")
        else:
            ok = False
            no.append("5-minute trend is still down")
    return ok, yes, no


def _bearish(ind: Indicators, trend: Optional[Indicators]) -> tuple[bool, list[str], list[str]]:
    ok, yes, no = True, [], []
    if not ind.ready:
        return False, [], ["indicators still warming up"]
    if ind.ema9 < ind.ema21:
        yes.append(f"EMA 9 < 21 (${ind.ema9:,.0f} < ${ind.ema21:,.0f})")
    else:
        ok = False
        no.append("EMA 9 is still above EMA 21")
    if abs((ind.ema9 or 0) - (ind.ema21 or 0)) < EMA_FLAT_DOLLARS:
        ok = False
        no.append("EMAs are flat — looks choppy")
    if ind.close < ind.ema50:
        yes.append("price below the 50-period EMA")
    else:
        ok = False
        no.append("price is still above the 50-period EMA")
    if ind.macd_hist < 0:
        yes.append("MACD histogram is negative")
    else:
        ok = False
        no.append("MACD histogram is still positive")
    if RSI_NO_LO <= ind.rsi14 <= RSI_NO_HI:
        yes.append(f"RSI {ind.rsi14:.0f}")
    else:
        ok = False
        if ind.rsi14 < RSI_NO_LO:
            no.append(f"RSI {ind.rsi14:.0f} is oversold / stretched")
        else:
            no.append(f"RSI {ind.rsi14:.0f} is not in the bearish 25–48 band")
    if trend and trend.ready:
        if trend.ema9 <= trend.ema21:
            yes.append("5-minute trend agrees")
        else:
            ok = False
            no.append("5-minute trend is still up")
    return ok, yes, no


def make_signal(
    *,
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    yes_ask: Optional[float],
    yes_bid: Optional[float],
    no_ask: Optional[float],
    no_bid: Optional[float],
    remaining_sec: Optional[int],
    position: Optional[dict] = None,
) -> Signal:
    if remaining_sec is not None and remaining_sec <= 0:
        return Signal("WAIT", "WAIT — Window closed", "This 15-minute window has closed. Waiting for the next contract.")

    # Manage an open paper position first.
    if position:
        side = position["side"]
        entry = float(position["entry_price"])
        bid = yes_bid if side == "yes" else no_bid
        exit_sig = _exit_signal(position, bid, ind_1m, btc_price, strike, remaining_sec)
        if exit_sig:
            return exit_sig
        held = f"Holding {side.upper()} from {cents_label(entry)}"
        if bid is not None:
            held += f"; live bid {cents_label(bid)}"
        held += ". No exit trigger yet — still watching until settlement or a flip."
        return Signal("HOLD", f"HOLD {side.upper()}", held, side=side, ask=None, bid=bid)

    if remaining_sec is not None and remaining_sec < MIN_REMAINING_SEC:
        mm, ss = divmod(max(0, remaining_sec), 60)
        return Signal(
            "WAIT",
            "WAIT — Too close to expiry",
            f"Only {mm}:{ss:02d} left in this window. The bot stands aside under 2 minutes.",
        )
    if not ind_1m or not ind_1m.ready:
        return Signal("WAIT", "WAIT — No Trade", "1-minute indicators are still warming up.")
    if strike is None:
        return Signal("WAIT", "WAIT — No Trade", "The contract strike is not posted yet.")

    dist = btc_price - float(strike)
    dist_txt = f"BTC {('+' if dist >= 0 else '')}${dist:,.2f} vs the ${float(strike):,.2f} strike"

    if abs(dist) < MIN_STRIKE_DISTANCE:
        return Signal(
            "WAIT",
            "WAIT — Too close to strike",
            f"{dist_txt}. That’s inside a ${MIN_STRIKE_DISTANCE:.0f} band, so settlement could still flip either way.",
        )

    bull_ok, bull_yes, bull_no = _bullish(ind_1m, ind_5m)
    bear_ok, bear_yes, bear_no = _bearish(ind_1m, ind_5m)

    if bull_ok and dist > 0:
        if yes_ask is None:
            return Signal("WAIT", "WAIT — No Trade", f"{'; '.join(bull_yes)}. {dist_txt}. Yes ask is unavailable.")
        if not (ASK_MIN <= yes_ask <= ASK_MAX):
            if yes_ask > ASK_MAX:
                return Signal(
                    "WAIT",
                    "WAIT — No Edge",
                    f"{'; '.join(bull_yes)}. {dist_txt}, but Yes is already {cents_label(yes_ask)} — too expensive (bot only buys 35¢–72¢).",
                )
            return Signal(
                "WAIT",
                "WAIT — No Edge",
                f"{'; '.join(bull_yes)}. {dist_txt}, but Yes ask is only {cents_label(yes_ask)} (below the 35¢ floor — something looks off).",
            )
        reason = f"{'; '.join(bull_yes)}. {dist_txt}, Yes ask {cents_label(yes_ask)} — Buy Yes."
        return Signal("BUY_YES", f"BUY YES @ {cents_label(yes_ask)}", reason, side="yes", ask=yes_ask, bid=yes_bid)

    if bear_ok and dist < 0:
        if no_ask is None:
            return Signal("WAIT", "WAIT — No Trade", f"{'; '.join(bear_yes)}. {dist_txt}. No ask is unavailable.")
        if not (ASK_MIN <= no_ask <= ASK_MAX):
            if no_ask > ASK_MAX:
                return Signal(
                    "WAIT",
                    "WAIT — No Edge",
                    f"{'; '.join(bear_yes)}. {dist_txt}, but No is already {cents_label(no_ask)} — too expensive (bot only buys 35¢–72¢).",
                )
            return Signal(
                "WAIT",
                "WAIT — No Edge",
                f"{'; '.join(bear_yes)}. {dist_txt}, but No ask is only {cents_label(no_ask)} (below the 35¢ floor — something looks off).",
            )
        reason = f"{'; '.join(bear_yes)}. {dist_txt}, No ask {cents_label(no_ask)} — Buy No."
        return Signal("BUY_NO", f"BUY NO @ {cents_label(no_ask)}", reason, side="no", ask=no_ask, bid=no_bid)

    bits = []
    if dist > 0 and not bull_ok:
        bits.append("price is above the strike, but the 1-minute trend/momentum is not cleanly bullish (" + "; ".join(bull_no) + ")")
    elif dist < 0 and not bear_ok:
        bits.append("price is below the strike, but the 1-minute trend/momentum is not cleanly bearish (" + "; ".join(bear_no) + ")")
    elif bull_ok and dist < 0:
        bits.append("EMAs look bullish, but BTC is still below the strike — the contract would settle No if it expired now")
    elif bear_ok and dist > 0:
        bits.append("EMAs look bearish, but BTC is still above the strike — the contract would settle Yes if it expired now")
    else:
        bits.append("trend and momentum are mixed")
    if yes_ask is not None:
        bits.append(f"Yes ask {cents_label(yes_ask)}")
    return Signal("WAIT", "WAIT — No Trade", "Choppy / no clean edge: " + "; ".join(bits) + ".")


def _exit_signal(
    position: dict,
    bid: Optional[float],
    ind: Optional[Indicators],
    btc_price: float,
    strike: Optional[float],
    remaining_sec: Optional[int],
) -> Optional[Signal]:
    if remaining_sec is not None and remaining_sec < EARLY_EXIT_CUTOFF_SEC:
        return None  # hold to settlement
    side = position["side"]
    entry = float(position["entry_price"])
    if bid is None:
        return None
    if bid >= max(TAKE_PROFIT_BID, entry + TAKE_PROFIT_GAIN) or bid >= entry + TAKE_PROFIT_GAIN:
        return Signal(
            "TAKE_PROFIT",
            f"SELL NOW @ {cents_label(bid)}",
            f"Take profit: {side.upper()} bid is {cents_label(bid)} vs a {cents_label(entry)} entry. Sell now rather than holding to settlement.",
            side=side,
            bid=bid,
        )
    if bid <= entry - STOP_LOSS_DROP:
        return Signal(
            "CUT_LOSS",
            f"CUT LOSS @ {cents_label(bid)}",
            f"Cut loss: {side.upper()} bid fell to {cents_label(bid)} from a {cents_label(entry)} entry (−{(entry-bid)*100:.0f}¢). Sell now rather than hoping settlement saves it.",
            side=side,
            bid=bid,
        )
    # Momentum flip against the position, and the bid has at least stopped being a great hold.
    if ind and ind.ready and strike is not None:
        dist = btc_price - float(strike)
        if side == "yes" and ind.ema9 < ind.ema21 and dist < 0 and bid < entry:
            return Signal(
                "CUT_LOSS",
                f"CUT LOSS @ {cents_label(bid)}",
                f"Momentum flipped: EMA 9 crossed back below EMA 21 and BTC is now below the strike. Sell Yes at {cents_label(bid)} (entry {cents_label(entry)}).",
                side=side,
                bid=bid,
            )
        if side == "no" and ind.ema9 > ind.ema21 and dist > 0 and bid < entry:
            return Signal(
                "CUT_LOSS",
                f"CUT LOSS @ {cents_label(bid)}",
                f"Momentum flipped: EMA 9 crossed back above EMA 21 and BTC is now above the strike. Sell No at {cents_label(bid)} (entry {cents_label(entry)}).",
                side=side,
                bid=bid,
            )
    return None


# ---------------------------------------------------------------------------
# Paper book
# ---------------------------------------------------------------------------

def default_state() -> dict:
    return {
        "cash": STARTING_BALANCE,
        "starting_balance": STARTING_BALANCE,
        "stake": STAKE,
        "position": None,
        "trades": [],
        "equity_history": [],
        "created_at": iso_utc(utc_now()),
    }


def load_state() -> dict:
    if not STATE_PATH.exists():
        return default_state()
    with STATE_PATH.open() as f:
        state = json.load(f)
    state.setdefault("cash", STARTING_BALANCE)
    state.setdefault("starting_balance", STARTING_BALANCE)
    state.setdefault("stake", STAKE)
    state.setdefault("position", None)
    state.setdefault("trades", [])
    state.setdefault("equity_history", [])
    return state


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w") as f:
        json.dump(payload, f, indent=2)
        f.write("\n")
    tmp.replace(path)


def mark_equity(cash: float, position: Optional[dict], yes_bid: Optional[float], no_bid: Optional[float]) -> float:
    if not position:
        return round_money(cash)
    bid = yes_bid if position["side"] == "yes" else no_bid
    if bid is None:
        return round_money(cash + float(position.get("cost", 0)))
    return round_money(cash + float(position["contracts"]) * bid)


def open_trade(cash: float, market: dict, side: str, ask: float, reason: str, now: datetime) -> Optional[tuple[float, dict, dict]]:
    if ask <= 0 or ask >= 1:
        return None
    contracts = int(STAKE // ask)
    if contracts < 1:
        return None
    fee = kalshi_taker_fee(contracts, ask)
    cost = round_money(contracts * ask)
    total = round_money(cost + fee)
    while contracts >= 1 and total > cash + 1e-9:
        contracts -= 1
        fee = kalshi_taker_fee(contracts, ask)
        cost = round_money(contracts * ask)
        total = round_money(cost + fee)
    if contracts < 1 or total > cash + 1e-9:
        return None
    cash_after = round_money(cash - total)
    position = {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title") or "BTC price up in next 15 mins?",
        "side": side,
        "contracts": contracts,
        "entry_price": round_cent(ask),
        "entry_fee": fee,
        "cost": cost,
        "total_in": total,
        "entry_time": iso_utc(now),
        "entry_time_pt": fmt_pt(now),
        "close_time": market.get("close_time"),
        "close_time_pt": fmt_pt(market["close_time"]) if market.get("close_time") else None,
        "strike": dollars(market.get("floor_strike")),
        "reason": reason,
        "kalshi_url": contract_url(market["ticker"]),
    }
    log = {
        "status": "open",
        "ticker": position["ticker"],
        "side": side,
        "contracts": contracts,
        "entry_price": position["entry_price"],
        "entry_fee": fee,
        "entry_time": position["entry_time"],
        "entry_time_pt": position["entry_time_pt"],
        "reason": reason,
        "strike": position["strike"],
    }
    return cash_after, position, log


def close_trade(
    cash: float,
    position: dict,
    *,
    exit_price: Optional[float],
    exit_fee: float,
    result: Optional[str],
    outcome: str,
    reason: str,
    now: datetime,
    expiration_value: Optional[float] = None,
) -> tuple[float, dict]:
    contracts = int(position["contracts"])
    if outcome == "settlement":
        # Winning side pays $1 per contract, losing side pays $0. No extra settlement fee.
        won = (position["side"] == "yes" and result == "yes") or (position["side"] == "no" and result == "no")
        payout = contracts * (1.0 if won else 0.0)
        exit_price = 1.0 if won else 0.0
        exit_fee = 0.0
    else:
        payout = contracts * float(exit_price or 0.0)
    proceeds = round_money(payout - exit_fee)
    cash_after = round_money(cash + proceeds)
    pnl = round_money(proceeds - float(position["total_in"]))
    trade = {
        "status": "closed",
        "ticker": position["ticker"],
        "side": position["side"],
        "contracts": contracts,
        "entry_price": position["entry_price"],
        "entry_fee": position["entry_fee"],
        "entry_time": position["entry_time"],
        "entry_time_pt": position["entry_time_pt"],
        "exit_price": round_cent(exit_price or 0.0),
        "exit_fee": round_money(exit_fee),
        "exit_time": iso_utc(now),
        "exit_time_pt": fmt_pt(now),
        "outcome": outcome,
        "result": result,
        "expiration_value": expiration_value,
        "strike": position.get("strike"),
        "pnl": pnl,
        "reason_entry": position.get("reason"),
        "reason_exit": reason,
        "kalshi_url": position.get("kalshi_url") or contract_url(position["ticker"]),
    }
    return cash_after, trade


def trade_stats(trades: list[dict]) -> dict:
    closed = [t for t in trades if t.get("status") == "closed" and t.get("pnl") is not None]
    wins = [t for t in closed if float(t["pnl"]) > 0]
    losses = [t for t in closed if float(t["pnl"]) <= 0]
    gp = sum(float(t["pnl"]) for t in wins)
    gl = abs(sum(float(t["pnl"]) for t in losses))
    if closed and gl == 0 and gp > 0:
        pf, pf_note = None, "n/a (no losing trades)"
    elif not closed:
        pf, pf_note = None, "n/a (no closed trades)"
    else:
        pf, pf_note = (round(gp / gl, 2) if gl else 0.0), None
    return {
        "trade_count": len(closed),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / len(closed) * 100.0, 1) if closed else None,
        "profit_factor": pf,
        "profit_factor_note": pf_note,
        "gross_profit": round_money(gp),
        "gross_loss": round_money(gl),
        "net_pnl": round_money(sum(float(t["pnl"]) for t in closed)),
    }


# ---------------------------------------------------------------------------
# Backtest
# ---------------------------------------------------------------------------

def run_backtest(
    settled: list[dict],
    sticks: dict[str, list[dict]],
    candles_1m: list[dict],
    series_1m: list[Indicators],
    candles_5m: list[dict],
    series_5m: list[Indicators],
) -> dict:
    # Oldest first, only markets with a real yes/no result and a strike.
    markets = [
        m
        for m in settled
        if (m.get("result") in ("yes", "no")) and dollars(m.get("floor_strike")) is not None and m.get("open_time")
    ]
    markets.sort(key=lambda m: to_ts(m["open_time"]))

    cash = STARTING_BALANCE
    trades: list[dict] = []
    equity: list[dict] = []
    scanned = 0
    skipped = 0
    skip_reasons: dict[str, int] = {}

    for m in markets:
        open_ts = to_ts(m["open_time"])
        decision_ts = open_ts + DECISION_OFFSET_SEC
        scanned += 1
        q = quote_at_decision(sticks.get(m["ticker"]) or [], open_ts)
        snap_1 = indicators_at(candles_1m, series_1m, decision_ts - 1)
        snap_5 = indicators_at(candles_5m, series_5m, decision_ts - 1)
        if not q or not snap_1 or not snap_1[1].ready:
            skipped += 1
            key = "missing quotes or indicators"
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            continue
        btc_px = snap_1[0]["close"]
        strike = dollars(m["floor_strike"])
        sig = make_signal(
            ind_1m=snap_1[1],
            ind_5m=snap_5[1] if snap_5 else None,
            btc_price=btc_px,
            strike=strike,
            yes_ask=q["yes_ask"],
            yes_bid=q["yes_bid"],
            no_ask=q["no_ask"],
            no_bid=q["no_bid"],
            remaining_sec=to_ts(m["close_time"]) - decision_ts,
            position=None,
        )
        if sig.action not in ("BUY_YES", "BUY_NO"):
            skipped += 1
            key = sig.label
            skip_reasons[key] = skip_reasons.get(key, 0) + 1
            equity.append({"t": decision_ts, "t_pt": fmt_pt(decision_ts), "equity": round_money(cash)})
            continue
        opened = open_trade(cash, m, sig.side or "yes", float(sig.ask), sig.reason, parse_iso(decision_ts))
        if not opened:
            skipped += 1
            skip_reasons["not enough cash / size"] = skip_reasons.get("not enough cash / size", 0) + 1
            continue
        cash, position, _log = opened
        close_dt = parse_iso(m.get("settlement_ts") or m["close_time"])
        cash, trade = close_trade(
            cash,
            position,
            exit_price=None,
            exit_fee=0.0,
            result=m["result"],
            outcome="settlement",
            reason=f"Held to settlement: official result was {m['result'].upper()} (BTC {dollars(m.get('expiration_value')) or 'n/a'} vs strike {strike}).",
            now=close_dt,
            expiration_value=dollars(m.get("expiration_value")),
        )
        trades.append(trade)
        equity.append({"t": to_ts(close_dt), "t_pt": fmt_pt(close_dt), "equity": cash})

    stats = trade_stats(trades)
    start_eq = STARTING_BALANCE
    end_eq = cash
    # Max drawdown on the equity path.
    peak = start_eq
    max_dd = 0.0
    running = start_eq
    for pt in equity:
        running = float(pt["equity"])
        if running > peak:
            peak = running
        if peak > 0:
            max_dd = max(max_dd, (peak - running) / peak)

    first_t = to_ts(markets[0]["open_time"]) if markets else None
    last_t = to_ts(markets[-1]["close_time"]) if markets else None
    return {
        "start": iso_utc(first_t) if first_t else None,
        "end": iso_utc(last_t) if last_t else None,
        "start_pt": fmt_pt(first_t) if first_t else None,
        "end_pt": fmt_pt(last_t) if last_t else None,
        "days": round((last_t - first_t) / 86400, 1) if first_t and last_t else 0,
        "windows_scanned": scanned,
        "windows_traded": stats["trade_count"],
        "windows_skipped": skipped,
        "skip_reasons": skip_reasons,
        "starting_balance": STARTING_BALANCE,
        "stake": STAKE,
        "ending_equity": round_money(end_eq),
        "return_pct": round(((end_eq / start_eq) - 1.0) * 100.0, 2) if start_eq else 0.0,
        "max_drawdown_pct": round(max_dd * 100.0, 2),
        **stats,
        "equity_history": equity,
        "trades": list(reversed(trades[-80:])),
        "assumption_note": (
            "Each window is evaluated once, 1 minute after it opens, using the real Kalshi Yes/No "
            "quotes at that minute and Coinbase 1-minute / 5-minute BTC candles. Trades use a $5 stake, "
            "the official quadratic taker fee, and are held to Kalshi’s official Yes/No settlement. "
            "The live bot can also enter later in a window (on a 5-minute scan) or exit early if momentum flips; "
            "those live extras are not in this backtest."
        ),
        "fee_note": "Paper PnL includes Kalshi’s taker fee: ceil(0.07 × contracts × price × (1 − price)), rounded up to the next cent.",
    }


# ---------------------------------------------------------------------------
# Live paper step
# ---------------------------------------------------------------------------

def pick_live_market(open_markets: list[dict], settled: list[dict], now: datetime) -> Optional[dict]:
    now_ts = int(now.timestamp())
    candidates = []
    for m in open_markets + settled:
        if not m.get("open_time") or not m.get("close_time"):
            continue
        if m.get("ticker") and to_ts(m["open_time"]) <= now_ts < to_ts(m["close_time"]) + 1:
            candidates.append(m)
    if not candidates:
        # Prefer an open/active market even if clocks are slightly off.
        live = [m for m in open_markets if (m.get("status") in ("open", "active", "initialized") or True) and dollars(m.get("yes_ask_dollars")) is not None]
        live = [m for m in live if m.get("status") in ("open", "active")]
        return live[0] if live else None
    candidates.sort(key=lambda m: to_ts(m["close_time"]))
    return candidates[-1]


def find_market(ticker: str, *groups: list[dict]) -> Optional[dict]:
    for group in groups:
        for m in group:
            if m.get("ticker") == ticker:
                return m
    return None


def market_payload(market: Optional[dict], now: datetime) -> Optional[dict]:
    if not market:
        return None
    close_ts = to_ts(market["close_time"]) if market.get("close_time") else None
    remaining = (close_ts - int(now.timestamp())) if close_ts else None
    yes_ask = dollars(market.get("yes_ask_dollars"))
    yes_bid = dollars(market.get("yes_bid_dollars"))
    no_ask = dollars(market.get("no_ask_dollars"))
    no_bid = dollars(market.get("no_bid_dollars"))
    if no_ask is None and yes_bid is not None:
        no_ask = round_cent(max(0.0, 1.0 - yes_bid))
    if no_bid is None and yes_ask is not None:
        no_bid = round_cent(max(0.0, 1.0 - yes_ask))
    return {
        "ticker": market["ticker"],
        "event_ticker": market.get("event_ticker"),
        "title": market.get("title") or "BTC price up in next 15 mins?",
        "subtitle": market.get("yes_sub_title") or "",
        "status": market.get("status"),
        "strike": dollars(market.get("floor_strike")),
        "open_time": market.get("open_time"),
        "open_time_pt": fmt_pt(market["open_time"]) if market.get("open_time") else None,
        "close_time": market.get("close_time"),
        "close_time_pt": fmt_pt(market["close_time"]) if market.get("close_time") else None,
        "remaining_sec": remaining,
        "yes_bid": yes_bid,
        "yes_ask": yes_ask,
        "no_bid": no_bid,
        "no_ask": no_ask,
        "last": dollars(market.get("last_price_dollars")),
        "volume": dollars(market.get("volume_fp")),
        "result": market.get("result") or None,
        "expiration_value": dollars(market.get("expiration_value")),
        "kalshi_url": contract_url(market["ticker"]),
        "rules": market.get("rules_primary") or "",
    }


def run_live(
    state: dict,
    market: Optional[dict],
    settled: list[dict],
    ind_1m: Optional[Indicators],
    ind_5m: Optional[Indicators],
    btc_price: float,
    now: datetime,
) -> tuple[dict, Signal, list[str]]:
    events: list[str] = []
    cash = float(state["cash"])
    position = state.get("position")
    trades = list(state.get("trades") or [])
    equity_history = list(state.get("equity_history") or [])

    # Settle or early-close an open position if its market is done or the signal says sell.
    if position:
        pos_m = find_market(position["ticker"], settled, [market] if market else [])
        # Also try a dedicated fetch if we didn't see it in the lists.
        if pos_m is None:
            try:
                data = http_get_json(f"{KALSHI_API}/markets/{urllib.parse.quote(position['ticker'])}")
                pos_m = data.get("market") or data
            except Exception as exc:
                events.append(f"Could not refresh open market {position['ticker']}: {exc}")

        if pos_m and (pos_m.get("result") in ("yes", "no") or pos_m.get("status") in ("finalized", "settled", "determined")):
            if pos_m.get("result") in ("yes", "no"):
                cash, trade = close_trade(
                    cash,
                    position,
                    exit_price=None,
                    exit_fee=0.0,
                    result=pos_m["result"],
                    outcome="settlement",
                    reason=f"Settled {pos_m['result'].upper()} at BTC {dollars(pos_m.get('expiration_value')) or 'n/a'} vs strike {position.get('strike')}.",
                    now=parse_iso(pos_m.get("settlement_ts") or now),
                    expiration_value=dollars(pos_m.get("expiration_value")),
                )
                trades.append(trade)
                events.append(f"Settled {position['side'].upper()} on {position['ticker']}: {trade['reason_exit']} PnL {trade['pnl']:+.2f}")
                position = None

    live_mp = market_payload(market, now) if market else None
    yes_ask = live_mp["yes_ask"] if live_mp else None
    yes_bid = live_mp["yes_bid"] if live_mp else None
    no_ask = live_mp["no_ask"] if live_mp else None
    no_bid = live_mp["no_bid"] if live_mp else None
    strike = live_mp["strike"] if live_mp else (position or {}).get("strike")
    remaining = live_mp["remaining_sec"] if live_mp else None

    # If the open position is still in this (or its) window, maybe exit early.
    if position and market and market.get("ticker") == position["ticker"]:
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
        )
        if sig.action in ("TAKE_PROFIT", "CUT_LOSS") and sig.bid is not None:
            fee = kalshi_taker_fee(int(position["contracts"]), float(sig.bid))
            cash, trade = close_trade(
                cash,
                position,
                exit_price=float(sig.bid),
                exit_fee=fee,
                result=None,
                outcome="early_exit",
                reason=sig.reason,
                now=now,
            )
            trades.append(trade)
            events.append(f"{sig.action} {position['side'].upper()} on {position['ticker']} @ {cents_label(sig.bid)} PnL {trade['pnl']:+.2f}")
            position = None
        else:
            # Still holding; return this HOLD/WAIT signal later.
            pass
    else:
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
        )

    # New entry — only when flat, in a live window, and the call is a buy.
    if position is None and market and sig.action in ("BUY_YES", "BUY_NO") and sig.ask is not None:
        opened = open_trade(cash, market, sig.side or "yes", float(sig.ask), sig.reason, now)
        if opened:
            cash, position, log = opened
            trades.append(log)
            events.append(f"Opened {position['side'].upper()} {position['contracts']}x {position['ticker']} @ {cents_label(position['entry_price'])} (fee ${position['entry_fee']:.2f})")
            sig = Signal(
                "HOLD",
                f"HOLD {position['side'].upper()} @ {cents_label(position['entry_price'])}",
                f"Just bought {position['side'].upper()} — {sig.reason}",
                side=position["side"],
                ask=sig.ask,
                bid=yes_bid if position["side"] == "yes" else no_bid,
            )
        else:
            events.append("Signal fired but not enough paper cash to buy $5 of contracts after fees.")
            sig = Signal("WAIT", "WAIT — No Trade", sig.reason + " Not enough paper cash to size a $5 ticket after fees.")

    if position and sig.action not in ("TAKE_PROFIT", "CUT_LOSS", "HOLD"):
        # Recompute hold text now that we may have just opened, or are still in.
        sig = make_signal(
            ind_1m=ind_1m,
            ind_5m=ind_5m,
            btc_price=btc_price,
            strike=strike,
            yes_ask=yes_ask,
            yes_bid=yes_bid,
            no_ask=no_ask,
            no_bid=no_bid,
            remaining_sec=remaining,
            position=position,
        )

    equity = mark_equity(cash, position, yes_bid, no_bid)
    equity_history.append({"t": int(now.timestamp()), "t_pt": fmt_pt(now), "equity": equity})
    # Keep the live curve from growing forever.
    if len(equity_history) > 2000:
        equity_history = equity_history[-2000:]

    new_state = {
        "cash": round_money(cash),
        "starting_balance": float(state.get("starting_balance") or STARTING_BALANCE),
        "stake": STAKE,
        "position": position,
        "trades": trades,
        "equity_history": equity_history,
        "created_at": state.get("created_at") or iso_utc(now),
        "updated_at": iso_utc(now),
        "updated_at_pt": fmt_pt(now),
    }
    return new_state, sig, events


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    print(f"[{fmt_pt(now)}] Fetching Kalshi {SERIES} + BTC candles…", flush=True)

    open_markets = fetch_kalshi_markets("open", limit=50, max_pages=1)
    # Also grab recently closed / settled for grading + backtest.
    settled = fetch_kalshi_markets("settled", limit=200, max_pages=8)
    print(f"  Open/active markets: {len(open_markets)}; settled fetched: {len(settled)}", flush=True)

    # Recent window start for candle download (backtest days + warmup).
    if settled:
        oldest = min(to_ts(m["open_time"]) for m in settled if m.get("open_time"))
    else:
        oldest = int((now - timedelta(days=BACKTEST_DAYS)).timestamp())
    candle_start = oldest - BACKTEST_CANDLE_PAD_HOURS * 3600
    candle_end = int(now.timestamp()) + 60

    candles_1m, src_1m = fetch_btc_candles(candle_start, candle_end, 60)
    candles_5m, src_5m = fetch_btc_candles(candle_start, candle_end, 300)
    series_1m = compute_indicators(candles_1m)
    series_5m = compute_indicators(candles_5m)
    btc_price, price_src = fetch_live_price()
    print(
        f"  1m candles: {len(candles_1m)} ({src_1m}); 5m candles: {len(candles_5m)} ({src_5m}); "
        f"live BTC ${btc_price:,.2f} ({price_src})",
        flush=True,
    )

    # Historical Kalshi quotes at the 1-minute mark for an honest backtest.
    # Limit to the most recent BACKTEST_DAYS of settled windows to stay inside rate limits.
    cutoff = int((now - timedelta(days=BACKTEST_DAYS)).timestamp())
    settled_window = [m for m in settled if m.get("open_time") and to_ts(m["open_time"]) >= cutoff]
    print(f"  Backtest windows in last {BACKTEST_DAYS} days: {len(settled_window)}. Fetching 1-minute Kalshi quotes…", flush=True)
    sticks = fetch_candlesticks_for_markets(settled_window, first_minutes=3)
    print(f"  Candlesticks for {sum(1 for v in sticks.values() if v)} / {len(settled_window)} markets", flush=True)

    backtest = run_backtest(settled_window, sticks, candles_1m, series_1m, candles_5m, series_5m)
    backtest["generated_at"] = iso_utc(now)
    backtest["generated_at_pt"] = fmt_pt(now)
    backtest["candle_source_1m"] = src_1m
    backtest["candle_source_5m"] = src_5m
    save_json(BACKTEST_PATH, backtest)
    wr = backtest.get("win_rate")
    print(
        f"  Backtest {backtest.get('start_pt')} → {backtest.get('end_pt')}: "
        f"{backtest['windows_traded']} trades / {backtest['windows_scanned']} windows, "
        f"win rate {wr if wr is not None else '—'}%, "
        f"PnL ${backtest['net_pnl']:+.2f} ({backtest['return_pct']:+.2f}%), "
        f"max DD {backtest['max_drawdown_pct']:.2f}%",
        flush=True,
    )

    market = pick_live_market(open_markets, settled, now)
    last_closed_1 = None
    last_closed_5 = None
    now_ts = int(now.timestamp())
    # Use last fully closed 1-min / 5-min bars (not the in-progress one).
    snap1 = indicators_at(candles_1m, series_1m, now_ts - 60)
    snap5 = indicators_at(candles_5m, series_5m, now_ts - 300)
    ind_1m = snap1[1] if snap1 else None
    ind_5m = snap5[1] if snap5 else None
    if snap1:
        last_closed_1 = snap1[0]
    if snap5:
        last_closed_5 = snap5[0]

    state = load_state()
    # If an older hourly-spot state is still on disk, start a fresh $100 Kalshi book.
    if float(state.get("starting_balance") or 0) != STARTING_BALANCE or "stake" not in state:
        print("  Starting a fresh $100 / $5-per-trade Kalshi paper book (previous state was a different bot).", flush=True)
        state = default_state()

    new_state, sig, events = run_live(state, market, settled, ind_1m, ind_5m, btc_price, now)
    save_json(STATE_PATH, new_state)

    mp = market_payload(market, now)
    stats = trade_stats(new_state["trades"])
    start = float(new_state["starting_balance"])
    equity = mark_equity(
        new_state["cash"],
        new_state["position"],
        mp["yes_bid"] if mp else None,
        mp["no_bid"] if mp else None,
    )
    pnl = round_money(equity - start)
    strike = mp["strike"] if mp else None
    dist = (btc_price - strike) if strike is not None else None

    dashboard = {
        "generated_at": iso_utc(now),
        "generated_at_pt": fmt_pt(now),
        "series_ticker": SERIES,
        "price_source": price_src,
        "candle_source_1m": src_1m,
        "candle_source_5m": src_5m,
        "polling": {
            "coinbase_cors": True,
            "kalshi_cors": False,
            "note": (
                "The dashboard polls Coinbase’s public API every 20 seconds for a live BTC price and 1-minute "
                "indicators (CORS is open). Kalshi’s public API blocks browser CORS, so contract quotes, the "
                "official Buy/Sell/Wait call, and paper-trade grading refresh with each GitHub Actions run (every 5 minutes)."
            ),
        },
        "live": {
            "btc_price": round_px(btc_price, 2),
            "strike": strike,
            "distance": None if dist is None else round_px(dist, 2),
            "call": sig.action,
            "call_label": sig.label,
            "reason": sig.reason,
            "side": sig.side,
            "ask": sig.ask,
            "bid": sig.bid,
            "market": mp,
            "indicators_1m": ind_payload(ind_1m),
            "indicators_5m": ind_payload(ind_5m),
            "last_1m_close": last_closed_1["close"] if last_closed_1 else None,
            "last_1m_time_pt": fmt_pt(last_closed_1["time"]) if last_closed_1 else None,
            "cash": new_state["cash"],
            "equity": equity,
            "pnl": pnl,
            "pnl_pct": round((pnl / start) * 100.0, 2) if start else 0.0,
            "starting_balance": start,
            "stake": STAKE,
            "position": new_state["position"],
            "equity_history": new_state["equity_history"],
            "trades": list(reversed(new_state["trades"][-50:])),
            **stats,
            "events": events,
        },
        "backtest": backtest,
        "fee_note": "Every paper trade uses Kalshi’s taker fee, ceil(0.07 × contracts × price × (1 − price)), rounded up to the next cent. Stake is $5 per trade on a $100 starting book.",
        "rules_summary": {
            "buy_yes": "1m EMA 9 > 21, close > EMA 50, MACD hist > 0, RSI 52–75, 5m trend agrees, BTC at least $20 above the strike, Yes ask 35¢–72¢, and ≥ 2 minutes left.",
            "buy_no": "The bearish mirror (EMA 9 < 21, close < EMA 50, MACD hist < 0, RSI 25–48, 5m trend agrees, BTC at least $20 below the strike, No ask 35¢–72¢).",
            "exits": "Sell early if the bid reaches 80¢ or +15¢ from entry (take profit), drops 12¢ from entry, or momentum and BTC-vs-strike both flip against the position. Otherwise hold to official settlement.",
        },
    }
    save_json(DASHBOARD_PATH, dashboard)

    print(f"  Contract: {mp['ticker'] if mp else 'none'} | strike {strike} | BTC ${btc_price:,.2f}", flush=True)
    print(f"  CALL: {sig.label}", flush=True)
    print(f"  {sig.reason}", flush=True)
    print(f"  Paper equity ${equity:,.2f} (cash ${new_state['cash']:,.2f}) | {stats['wins']}W-{stats['losses']}L", flush=True)
    for ev in events:
        print(f"  EVENT {ev}", flush=True)
    print(f"  Wrote {STATE_PATH.relative_to(ROOT)}, {BACKTEST_PATH.relative_to(ROOT)}, {DASHBOARD_PATH.relative_to(ROOT)}", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
